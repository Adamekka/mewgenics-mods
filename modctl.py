#!/usr/bin/env python3
"""Back up Mewtator's selection and install Nexus file-chain updates."""

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 100000
IGNORED_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}
METADATA_NAMES = {"description.json", "info.json", "modinfo.json"}


class ModError(Exception):
    """An external input or runtime problem the user can resolve."""


class NexusApiError(ModError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ModError(f"Cannot read JSON from {path}: {error}") from error


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(character) < 32 for character in value):
        raise ModError(f"Invalid relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
        raise ModError(f"Unsafe relative path: {value!r}")
    if not path.parts:
        raise ModError(f"Invalid relative path: {value!r}")
    return path


def load_inventory(path):
    data = load_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("game") != "mewgenics":
        raise ModError("Inventory must use schema_version 1 and game 'mewgenics'.")
    if not isinstance(data.get("mods"), list):
        raise ModError("Inventory must contain a mods list.")
    ids, folders = set(), set()
    for mod in data["mods"]:
        if not isinstance(mod, dict):
            raise ModError("Each inventory mod must be an object.")
        mod_id = mod.get("nexus_mod_id")
        if type(mod_id) is not int or mod_id <= 0 or mod_id in ids:
            raise ModError("Every Nexus mod ID must be a unique positive integer.")
        ids.add(mod_id)
        if not isinstance(mod.get("name"), str) or not mod["name"].strip():
            raise ModError(f"Mod {mod_id} needs a name.")
        folder = safe_relative(mod.get("folder"))
        if len(folder.parts) != 1 or mod["folder"].casefold() in folders:
            raise ModError(f"Mod {mod_id} needs a unique immediate-child folder.")
        folders.add(mod["folder"].casefold())
        if type(mod.get("enabled")) is not bool:
            raise ModError(f"Mod {mod_id} needs an explicit enabled boolean.")
        if "file_id" not in mod or (mod["file_id"] is not None and (type(mod["file_id"]) is not int or mod["file_id"] <= 0)):
            raise ModError(f"Mod {mod_id} needs a positive file_id or null.")
        if "version" not in mod or (mod["version"] is not None and not isinstance(mod["version"], str)):
            raise ModError(f"Mod {mod_id} needs a version string or null.")
        if mod.get("install_target") not in {"mewtator", "game_files"}:
            raise ModError(f"Mod {mod_id} uses an unsupported install_target.")
        if mod["install_target"] == "game_files":
            mapping = mod.get("file_map")
            if not isinstance(mapping, dict) or not mapping:
                raise ModError(f"Mod {mod_id}: game_files requires an explicit file_map.")
            for source, destination in mapping.items():
                safe_relative(source)
                safe_relative(destination)
        renames = mod.get("file_renames", {})
        if not isinstance(renames, dict):
            raise ModError(f"Mod {mod_id}: file_renames must be an object.")
        for source, destination in renames.items():
            safe_relative(source)
            safe_relative(destination)
        if "sha256" in mod and (not isinstance(mod["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", mod["sha256"])):
            raise ModError(f"Mod {mod_id}: sha256 must be a 64-character hexadecimal digest.")
    owned_files = []
    for mod in data["mods"]:
        if mod["install_target"] == "game_files":
            owned_files.extend(value.casefold() for value in mod["file_map"].values())
    if len(owned_files) != len(set(owned_files)):
        raise ModError("Multiple mods claim ownership of the same game file.")
    if any(a != b and PurePosixPath(a) in PurePosixPath(b).parents for a in owned_files for b in owned_files):
        raise ModError("Owned game file paths overlap.")
    return data


def read_setup(config_path):
    config = load_json(config_path)
    if not isinstance(config, dict) or not isinstance(config.get("mod_folder"), str) or not config["mod_folder"]:
        raise ModError(f"Mewtator config has no mod_folder: {config_path}")
    root = Path(config["mod_folder"]).expanduser()
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ModError(f"Mewtator mod_folder must be an existing, real absolute directory: {root}")
    selection = root / "modlist.txt"
    try:
        selected = selection.read_bytes()
        names = [line.strip() for line in selected.decode("utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError) as error:
        raise ModError(f"Cannot read Mewtator selection: {error}") from error
    if len(names) != len(set(names)):
        raise ModError("Mewtator modlist.txt contains duplicate mod names. Fix it before continuing.")
    for name in names:
        if len(safe_relative(name).parts) != 1:
            raise ModError(f"Unsafe folder name in modlist.txt: {name!r}")
    game_value = config.get("game_install_dir")
    if not isinstance(game_value, str) or not game_value:
        raise ModError(f"Mewtator config has no game_install_dir: {config_path}")
    game_root = Path(game_value).expanduser()
    if not game_root.is_absolute() or not game_root.is_dir() or game_root.is_symlink():
        raise ModError(f"Game directory must be an existing, real absolute directory: {game_root}")
    return root, game_root, selected, names


def inventory_bytes(inventory):
    return (json.dumps(inventory, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def assert_stopped():
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        raise ModError("Run mutating commands inside the SteamOS VM, where /proc is available.")
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            comm = (proc / "comm").read_text().strip()
            # Sandboxed helpers can deny /proc/exe access; comm and Wine's argv[0] identify these apps without ptrace permission.
            argv0 = (proc / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            windows_name = argv0.replace("\\", "/").rsplit("/", 1)[-1]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as error:
            if proc.stat().st_uid == os.getuid():
                raise ModError(f"Cannot inspect process {proc.name}; close the game and Mewtator, then retry.") from error
            continue
        if any(name.casefold() in {"mewtator", "mewgenics.exe"} for name in (comm, windows_name)):
            raise ModError(f"Close Mewgenics and Mewtator before changing mods. Process {proc.name} is still running.")


@contextmanager
def mutation_lock(mod_root):
    with (mod_root.parent / ".modctl.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ModError("Another modctl command is changing this mod folder. Wait for it to finish.") from error
        assert_stopped()
        yield


def game_target(game_root, relative):
    path = game_root.joinpath(*safe_relative(relative).parts)
    current = path
    while current != game_root:
        if current.is_symlink():
            raise ModError(f"Game file path passes through a symlink: {current}")
        current = current.parent
    return path


def game_files_enabled(mod, game_root):
    paths = [game_target(game_root, relative) for relative in mod["file_map"].values()]
    present = [path.is_file() for path in paths]
    if any(present) and not all(present):
        raise ModError(f"{mod['name']}: only some owned game files are present. Restore or reinstall the complete mod.")
    return all(present)


def selection_status(inventory, root, game_root, names):
    tracked = {mod["folder"]: mod for mod in inventory["mods"] if mod["install_target"] == "mewtator"}
    actual = {path.name for path in root.iterdir() if path.is_dir()}
    differences = []
    for mod in inventory["mods"]:
        if mod["install_target"] == "game_files":
            enabled = game_files_enabled(mod, game_root)
            if enabled != mod["enabled"]:
                differences.append(f"Selection differs: {mod['name']} game files are {'present' if enabled else 'absent'}.")
            continue
        state = "enabled" if mod["folder"] in names else "disabled"
        expected = "enabled" if mod["enabled"] else "disabled"
        if mod["folder"] not in actual:
            differences.append(f"Missing folder: {mod['name']} ({mod['folder']})")
        elif state != expected:
            differences.append(f"Selection differs: {mod['name']} is {state}, inventory says {expected}")
    differences.extend(f"Untracked folder: {name}" for name in sorted(actual - tracked.keys()))
    differences.extend(f"Enabled folder is missing: {name}" for name in names if name not in actual)
    expected_order = [mod["folder"] for mod in inventory["mods"] if mod["enabled"] and mod["install_target"] == "mewtator"]
    if names != expected_order:
        differences.append("Enabled mod order differs from the inventory.")
    return differences


def synced_inventory(inventory, root, game_root, names):
    by_folder = {mod["folder"]: mod for mod in inventory["mods"] if mod["install_target"] == "mewtator"}
    actual = {path.name for path in root.iterdir() if path.is_dir()}
    unknown = sorted(actual - by_folder.keys())
    missing = sorted(set(names) - actual)
    if unknown:
        raise ModError("Add Nexus IDs for these folders to mods.json before syncing: " + ", ".join(unknown))
    if missing:
        raise ModError("Restore or disable these missing mods in Mewtator before syncing: " + ", ".join(missing))
    result = copy.deepcopy(inventory)
    selected = set(names)
    for mod in result["mods"]:
        mod["enabled"] = game_files_enabled(mod, game_root) if mod["install_target"] == "game_files" else mod["folder"] in selected
    result_by_folder = {mod["folder"]: mod for mod in result["mods"]}
    result["mods"] = [result_by_folder[name] for name in names]
    result["mods"].extend(mod for mod in result_by_folder.values() if mod["folder"] not in selected)
    return result


def preserve_pending_selection(inventory, names, folders_to_sync, game_file_states):
    assert len(names) == len(set(names)), "Selection contains duplicate folder names"
    result = copy.deepcopy(inventory)
    by_folder = {mod["folder"]: mod for mod in result["mods"] if mod["install_target"] == "mewtator"}
    unknown = set(names) - by_folder.keys()
    if unknown:
        raise ModError("Add Nexus IDs for these enabled folders before installing: " + ", ".join(sorted(unknown)))
    selected = set(names)
    for mod in result["mods"]:
        if mod["install_target"] == "mewtator" and mod["folder"] in folders_to_sync:
            mod["enabled"] = mod["folder"] in selected
        elif mod["nexus_mod_id"] in game_file_states:
            mod["enabled"] = game_file_states[mod["nexus_mod_id"]]
    # Keep uninstalled entries in their recorded slots so restoring archives in any order retains the intended order.
    ordered = iter(by_folder[name] for name in names)
    result["mods"] = [next(ordered) if mod["install_target"] == "mewtator" and mod["folder"] in selected else mod for mod in result["mods"]]
    assert not list(ordered), "Not all selected mods were recorded"
    return result


class NoApiRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        # Redirects must never forward a personal API key to a different origin.
        return None


class NexusClient:
    def __init__(self, key):
        if not key or not key.strip():
            raise ModError("Set NEXUS_API_KEY to your personal Nexus API key. Never add it to Git.")
        self.key = key

    def get(self, endpoint):
        url = "https://api.nexusmods.com/v1/" + endpoint
        request = urllib.request.Request(url, headers={"apikey": self.key, "User-Agent": "mewgenics-modctl/1.0", "Accept": "application/json"})
        try:
            with urllib.request.build_opener(NoApiRedirect()).open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            hint = "Check NEXUS_API_KEY." if error.code == 401 else ""
            if error.code == 403:
                hint = "Nexus denied access. Unattended downloads require Premium. Free accounts can download the archive on the website, then use install --mod-id ID --file-id FILE --archive PATH."
            if error.code == 429:
                hint = "Nexus rate limit reached. Retry later."
            raise NexusApiError(f"Nexus API returned HTTP {error.code}. {hint}".strip(), error.code) from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ModError("Nexus API request failed. Check your network and retry.") from error

    def files(self, mod_id):
        return self.get(f"games/mewgenics/mods/{mod_id}/files.json")

    def download(self, mod_id, file_id, destination):
        try:
            links = self.get(f"games/mewgenics/mods/{mod_id}/files/{file_id}/download_link.json")
        except NexusApiError as error:
            if error.status == 403:
                raise ModError(f"{error}\nAfter downloading mod {mod_id}, file {file_id} on Nexus, run: ./manage install --mod-id {mod_id} --file-id {file_id} --archive PATH") from error
            raise
        if not isinstance(links, list) or not links or not isinstance(links[0], dict):
            raise ModError(f"Nexus returned no download link for mod {mod_id}, file {file_id}.")
        url = links[0].get("URI")
        if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme != "https":
            raise ModError("Nexus returned an invalid HTTPS download link.")
        # The API key belongs only on the API request, never on a CDN request or in saved metadata.
        request = urllib.request.Request(url, headers={"User-Agent": "mewgenics-modctl/1.0"})
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as output:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ModError("Archive exceeds the 1 GiB download limit.")
                    output.write(chunk)
                    digest.update(chunk)
        except urllib.error.HTTPError as error:
            raise ModError(f"Mod download returned HTTP {error.code}; retry to obtain a fresh download link.") from error
        except (urllib.error.URLError, OSError) as error:
            raise ModError("Mod download failed. Check your connection and available disk space.") from error
        return digest.hexdigest()


def nexus_file_records(response):
    if not isinstance(response, dict) or not isinstance(response.get("files"), list):
        raise ModError("Nexus returned an invalid files response.")
    files = {}
    for file in response["files"]:
        if not isinstance(file, dict) or type(file.get("file_id")) is not int:
            raise ModError("Nexus returned an invalid file record.")
        if file["file_id"] in files:
            raise ModError("Nexus returned duplicate file IDs.")
        files[file["file_id"]] = file
    return files


def update_file(current_id, response):
    if type(current_id) is not int or current_id <= 0:
        raise ModError("Select an exact Nexus file_id in mods.json before checking or updating this mod.")
    files = nexus_file_records(response)
    if not isinstance(response.get("file_updates"), list):
        raise ModError("Nexus returned an invalid file-update response.")
    successors = {}
    for edge in response["file_updates"]:
        if not isinstance(edge, dict) or type(edge.get("old_file_id")) is not int or type(edge.get("new_file_id")) is not int:
            raise ModError("Nexus returned an invalid file-update record.")
        successors.setdefault(edge["old_file_id"], set()).add(edge["new_file_id"])
    candidate = current_id
    visited = set()
    while candidate in successors:
        if candidate in visited:
            raise ModError("Nexus file-update chain contains a cycle. Select a file explicitly.")
        visited.add(candidate)
        next_ids = successors[candidate]
        if len(next_ids) != 1:
            raise ModError(f"File {candidate} has multiple update variants. Select the intended file_id explicitly.")
        candidate = next(iter(next_ids))
    if candidate not in files:
        raise ModError(f"Nexus no longer lists file {candidate}. Select an available file_id explicitly.")
    target = files[candidate]
    if target.get("category_id") not in {1, 2, 3, 5}:
        raise ModError(f"Nexus file {candidate} is not an active main, patch, optional, or miscellaneous file. Select an active file_id explicitly.")
    if candidate == current_id:
        return None
    if not isinstance(target.get("version"), str) or not target["version"]:
        raise ModError(f"Nexus file {candidate} has no version.")
    return target


def extract_archive(archive, destination):
    destination.mkdir()
    with archive.open("rb") as source:
        header = source.read(8)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if len(entries) > MAX_ARCHIVE_FILES or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
                raise ModError("Archive exceeds the extraction size or file-count limit.")
            paths = set()
            for entry in entries:
                path = safe_relative(entry.filename.replace("\\", "/"))
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise ModError(f"Archive contains a link or special file: {entry.filename!r}")
                if path in paths:
                    raise ModError(f"Archive contains a duplicate path: {entry.filename!r}")
                paths.add(path)
                if any(part in IGNORED_NAMES for part in path.parts):
                    continue
                output = destination.joinpath(*path.parts)
                if entry.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, output.open("xb") as target:
                        shutil.copyfileobj(source, target)
    elif header.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01")):
        if shutil.which("bsdtar") is None:
            raise ModError("RAR extraction requires bsdtar. Install it or use a ZIP download of this mod.")
        listing = subprocess.run(["bsdtar", "-tf", str(archive)], capture_output=True, text=True, check=False)
        detailed = subprocess.run(["bsdtar", "-tvf", str(archive)], capture_output=True, text=True, check=False)
        if listing.returncode or detailed.returncode:
            raise ModError("Cannot read the RAR archive. It may be corrupt or password-protected.")
        names = listing.stdout.splitlines()
        lines = detailed.stdout.splitlines()
        if not names or len(names) > MAX_ARCHIVE_FILES or len(names) != len(lines):
            raise ModError("RAR archive has invalid entries or exceeds the file-count limit.")
        total_size = 0
        paths = set()
        for name, line in zip(names, lines):
            path = safe_relative(name.replace("\\", "/"))
            if path in paths:
                raise ModError("RAR archive contains duplicate paths.")
            paths.add(path)
            entry = re.match(r"^[-d][rwxStTs-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s", line)
            if not entry or " link to " in line or " -> " in line:
                raise ModError("RAR archive contains a link, special file, or unsupported entry name.")
            total_size += int(entry[1])
        if total_size > MAX_ARCHIVE_BYTES:
            raise ModError("RAR exceeds the extraction size limit.")
        # bsdtar additionally rejects extraction through symlinks and paths escaping the target directory.
        extraction = subprocess.run(["bsdtar", "-xf", str(archive), "--no-same-owner", "--no-same-permissions", "-C", str(destination)], capture_output=True, text=True, check=False)
        if extraction.returncode:
            raise ModError("RAR extraction failed. The archive may be corrupt or unsafe.")
        files = list(destination.rglob("*"))
        if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in files):
            raise ModError("RAR extracted a link or special file.")
        if sum(path.stat().st_size for path in files if path.is_file()) > MAX_ARCHIVE_BYTES:
            raise ModError("RAR exceeds the extraction size limit.")
    else:
        raise ModError("Download is not a supported ZIP or RAR archive.")
    metadata_roots = {path.parent for path in destination.rglob("*") if path.is_file() and path.name.lower() in METADATA_NAMES}
    if len(metadata_roots) > 1:
        raise ModError("Archive contains multiple mod metadata folders. Select a single-mod download.")
    root = next(iter(metadata_roots)) if metadata_roots else destination
    if not metadata_roots:
        while True:
            entries = [path for path in root.iterdir() if path.name not in IGNORED_NAMES]
            if len(entries) == 1 and entries[0].is_dir():
                root = entries[0]
            else:
                break
    if not any(path.is_file() for path in root.rglob("*")):
        raise ModError("Archive contains no mod files.")
    return root


def apply_renames(root, renames):
    for source_name, target_name in renames.items():
        source = root.joinpath(*safe_relative(source_name).parts)
        target = root.joinpath(*safe_relative(target_name).parts)
        if not source.is_file() or target.exists():
            raise ModError(f"Cannot apply required file rename {source_name!r} to {target_name!r}. Check the new mod layout.")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)


def commit_updates(inventory_path, inventory_before, mod_root, selection_before, selection_after, changes, inventory_after):
    """Replace only owned files/folders; retain old payloads for recovery."""
    if inventory_path.read_bytes() != inventory_before or (mod_root / "modlist.txt").read_bytes() != selection_before:
        raise ModError("Inventory or Mewtator selection changed while preparing updates. Retry with Mewtator closed.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    backup = mod_root.parent / "backups" / stamp
    backup.mkdir(parents=True)
    (backup / "mods.json").write_bytes(inventory_before)
    (backup / "modlist.txt").write_bytes(selection_before)
    targets = [target for target, _, _ in changes]
    if len(targets) != len(set(targets)) or any(a != b and a in b.parents for a in targets for b in targets):
        raise ModError("An installation transaction contains overlapping owned paths.")
    (backup / "changes.json").write_bytes(inventory_bytes({"changes": [{"target": str(target), "backup": str(relative), "existed": target.exists()} for target, _, relative in changes]}))
    moved = []
    try:
        for target, staged, relative_backup in changes:
            if target.is_symlink() or (target.exists() and target.is_dir() != staged.is_dir()):
                raise ModError(f"Installed path has an unexpected type: {target}")
            previous = backup / relative_backup
            previous.parent.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            if existed:
                target.rename(previous)
            moved.append((target, previous, existed))
            staged.rename(target)
        if selection_after != selection_before:
            atomic_write(mod_root / "modlist.txt", selection_after)
        atomic_write(inventory_path, inventory_bytes(inventory_after))
    except Exception as error:
        rollback_errors = []
        for target, previous, existed in reversed(moved):
            try:
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                if existed:
                    previous.rename(target)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        for path, contents in [(inventory_path, inventory_before), (mod_root / "modlist.txt", selection_before)]:
            try:
                atomic_write(path, contents)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise ModError(f"Update failed and automatic rollback was incomplete. Restore from {backup}. Details: {'; '.join(rollback_errors)}") from error
        raise ModError(f"Update failed; the previous mods and selection were restored. Backup: {backup}. Cause: {error}") from error
    return backup


def stage_mod(archive, stage_dir, mod):
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    published_sha = mod.get("sha256")
    if published_sha and published_sha.lower() != sha256:
        raise ModError(f"{mod['name']}: archive does not match the recorded SHA-256 checksum.")
    root = extract_archive(archive, stage_dir)
    apply_renames(root, mod.get("file_renames", {}))
    return root, sha256


def owned_changes(mod, staged_root, mod_root, game_root):
    if mod["install_target"] == "mewtator":
        return [(mod_root / mod["folder"], staged_root, Path("mewtator") / mod["folder"])]
    changes = []
    for source_name, target_name in mod["file_map"].items():
        source = staged_root.joinpath(*safe_relative(source_name).parts)
        if not source.is_file():
            raise ModError(f"{mod['name']}: archive does not contain the selected file {source_name!r}.")
        changes.append((game_target(game_root, target_name), source, Path("game") / target_name))
    return changes


def run(args):
    inventory_path = args.inventory.expanduser().resolve()
    inventory_before = inventory_path.read_bytes()
    inventory = load_inventory(inventory_path)
    if inventory_path.read_bytes() != inventory_before:
        raise ModError("Inventory changed while reading it. Retry.")
    # Version checks need only the tracked inventory and Nexus, so CI does not need a game installation.
    if args.command != "check":
        mod_root, game_root, selection_before, names = read_setup(args.config.expanduser())
    if args.command == "status":
        for mod in inventory["mods"]:
            enabled = game_files_enabled(mod, game_root) if mod["install_target"] == "game_files" else mod["folder"] in names
            state = "enabled" if enabled else "disabled"
            print(f"{mod['nexus_mod_id']:>4}  {state:8}  {mod['name']}  {mod['version'] or 'version unrecorded'}")
        differences = selection_status(inventory, mod_root, game_root, names)
        for difference in differences:
            print(difference)
        return 1 if differences else 0
    if args.command == "sync":
        with mutation_lock(mod_root):
            if inventory_path.read_bytes() != inventory_before or (mod_root / "modlist.txt").read_bytes() != selection_before:
                raise ModError("Inventory or Mewtator selection changed. Retry.")
            atomic_write(inventory_path, inventory_bytes(synced_inventory(inventory, mod_root, game_root, names)))
        print(f"Saved enabled state and load order to {inventory_path}.")
        return 0
    selected = [mod for mod in inventory["mods"] if args.mod_id is None or mod["nexus_mod_id"] == args.mod_id]
    if not selected:
        raise ModError(f"Nexus mod ID {args.mod_id} is not in the inventory.")
    for mod in selected:
        if mod["file_id"] is None and not (args.command == "install" and args.file_id is not None):
            raise ModError(f"{mod['name']}: record its exact installed Nexus file_id before checking updates.")
    if args.command == "install":
        mod = copy.deepcopy(selected[0])
        if not mod["version"] and args.file_id is None:
            raise ModError(f"{mod['name']}: record the selected archive's version before installing.")
        archive = args.archive.expanduser().resolve()
        if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ModError("Select an existing archive no larger than 1 GiB.")
        with mutation_lock(mod_root):
            with tempfile.TemporaryDirectory(prefix=".modctl-stage-", dir=mod_root.parent) as temporary:
                staged_archive = Path(temporary) / "source.archive"
                # Verify and extract one private snapshot; a browser can replace the original Downloads file during API requests.
                shutil.copyfile(archive, staged_archive)
                if staged_archive.stat().st_size > MAX_ARCHIVE_BYTES:
                    raise ModError("Archive exceeds the 1 GiB installation limit.")
                installed_filename = archive.name
                if args.file_id is not None:
                    client = NexusClient(os.environ.get("NEXUS_API_KEY", ""))
                    response = client.files(mod["nexus_mod_id"])
                    files = nexus_file_records(response)
                    if args.file_id not in files or files[args.file_id].get("category_id") not in {1, 2, 3, 5}:
                        raise ModError("Select one active Nexus file for this mod.")
                    target = files[args.file_id]
                    if not isinstance(target.get("version"), str) or not target["version"]:
                        raise ModError("The selected Nexus file has no version.")
                    if not isinstance(target.get("file_name"), str) or not target["file_name"]:
                        raise ModError("The selected Nexus file has no archive filename.")
                    md5 = hashlib.md5(staged_archive.read_bytes()).hexdigest()
                    try:
                        matches = client.get(f"games/mewgenics/mods/md5_search/{md5}.json")
                    except NexusApiError as error:
                        if error.status == 404:
                            raise ModError("This archive does not match the selected Nexus mod and file ID.") from error
                        raise
                    if not isinstance(matches, list) or not any(isinstance(item, dict) and isinstance(item.get("mod"), dict) and isinstance(item.get("file_details"), dict) and item["mod"].get("mod_id") == mod["nexus_mod_id"] and item["file_details"].get("file_id") == args.file_id for item in matches):
                        raise ModError("This archive does not match the selected Nexus mod and file ID.")
                    installed_filename = target["file_name"]
                    mod.update(file_id=target["file_id"], version=target["version"])
                    # The archive was identified by Nexus; the old installed checksum belongs to the rollback metadata.
                    mod.pop("sha256", None)
                elif not mod.get("sha256"):
                    raise ModError("Use --file-id to identify this archive through Nexus before its first installation.")
                root, sha256 = stage_mod(staged_archive, Path(temporary) / "extracted", mod)
                selected_names = names.copy()
                present_folders = {path.name for path in mod_root.iterdir() if path.is_dir()}
                if mod["install_target"] == "mewtator" and mod["folder"] not in present_folders and mod["folder"] not in selected_names and mod["enabled"]:
                    intended = [item["folder"] for item in inventory["mods"] if item["install_target"] == "mewtator" and item["enabled"]]
                    successors = [name for name in intended[intended.index(mod["folder"]) + 1:] if name in selected_names]
                    selected_names.insert(selected_names.index(successors[0]) if successors else len(selected_names), mod["folder"])
                selection_after = selection_before if selected_names == names else ("".join(name + "\n" for name in selected_names)).encode("utf-8")
                game_states = {item["nexus_mod_id"]: game_files_enabled(item, game_root) for item in inventory["mods"] if item["install_target"] == "game_files"}
                if mod["install_target"] == "game_files":
                    game_states[mod["nexus_mod_id"]] = True
                after = preserve_pending_selection(inventory, selected_names, present_folders | {mod["folder"]}, game_states)
                next(item for item in after["mods"] if item["nexus_mod_id"] == mod["nexus_mod_id"]).update(file_id=mod["file_id"], version=mod["version"], sha256=sha256, file_name=installed_filename)
                assert_stopped()
                backup = commit_updates(inventory_path, inventory_before, mod_root, selection_before, selection_after, owned_changes(mod, root, mod_root, game_root), after)
        print(f"Installed {mod['name']}. Previous files and inventory: {backup}")
        return 0
    client = NexusClient(os.environ.get("NEXUS_API_KEY", ""))
    pending = []
    errors = []
    for mod in selected:
        if args.command == "update" and mod["install_target"] == "mewtator" and not (mod_root / mod["folder"]).is_dir():
            print(f"{mod['name']}: not installed; use install to restore this mod explicitly.")
            continue
        if args.command == "update" and mod["install_target"] == "game_files" and not game_files_enabled(mod, game_root):
            print(f"{mod['name']}: game files are not installed; use install to enable this mod explicitly.")
            continue
        try:
            target = update_file(mod["file_id"], client.files(mod["nexus_mod_id"]))
        except ModError as error:
            errors.append(f"{mod['name']}: {error}")
            continue
        if target is None:
            print(f"{mod['name']}: no file-chain update.")
        else:
            print(f"{mod['name']}: {mod['version']} -> {target['version']} (file {target['file_id']})")
            if args.command == "check":
                print(f"  https://www.nexusmods.com/mewgenics/mods/{mod['nexus_mod_id']}?tab=files")
                print(f"  ./manage install --mod-id {mod['nexus_mod_id']} --file-id {target['file_id']} --archive PATH")
            pending.append((mod, target))
    if errors:
        raise ModError("No updates were installed. Resolve these checks first:\n" + "\n".join(errors))
    if args.command == "check":
        if pending and args.fail_on_update:
            print(f"{len(pending)} mod update(s) available. Download the listed files from Nexus, then run the install commands shown.")
            return 1
        return 0
    if not pending:
        return 0
    with mutation_lock(mod_root):
        # A single transaction covers all requested updates, including downloads and extraction.
        with tempfile.TemporaryDirectory(prefix=".modctl-stage-", dir=mod_root.parent) as temporary:
            stage = Path(temporary)
            updates = []
            after = preserve_pending_selection(inventory, names, {path.name for path in mod_root.iterdir() if path.is_dir()}, {mod["nexus_mod_id"]: game_files_enabled(mod, game_root) for mod in inventory["mods"] if mod["install_target"] == "game_files"})
            after_by_id = {mod["nexus_mod_id"]: mod for mod in after["mods"]}
            for mod, target in pending:
                folder = mod["folder"]
                archive = stage / f"{mod['nexus_mod_id']}.download"
                sha256 = client.download(mod["nexus_mod_id"], target["file_id"], archive)
                published_sha = target.get("sha256")
                if published_sha:
                    if not isinstance(published_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", published_sha):
                        raise ModError(f"{mod['name']}: Nexus returned an invalid SHA-256 checksum.")
                    if published_sha.lower() != sha256:
                        raise ModError(f"{mod['name']}: download does not match Nexus's SHA-256 checksum.")
                staged_mod = dict(mod, sha256=sha256)
                root, _ = stage_mod(archive, stage / f"extract-{mod['nexus_mod_id']}", staged_mod)
                updates.extend(owned_changes(mod, root, mod_root, game_root))
                updated = after_by_id[mod["nexus_mod_id"]]
                updated.update(file_id=target["file_id"], version=target["version"], file_name=target.get("file_name"), sha256=sha256)
            assert_stopped()
            backup = commit_updates(inventory_path, inventory_before, mod_root, selection_before, selection_before, updates, after)
    print(f"Installed {len(pending)} update(s). Previous files and inventory: {backup}")
    print("Updated mods.json. The manage wrapper commits and pushes it automatically.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=Path(__file__).resolve().with_name("mods.json"), help="Inventory JSON; default: mods.json next to this script")
    parser.add_argument("--config", type=Path, default=Path("~/.local/opt/mewtator/config.json"), help="Mewtator config; default: ~/.local/opt/mewtator/config.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Compare tracked mods with installed folders and current selection")
    commands.add_parser("sync", help="Record Mewtator enabled state and load order in mods.json")
    install = commands.add_parser("install", help="Install the selected file from a local Nexus archive, retaining backups")
    install.add_argument("--mod-id", type=int, required=True, help="Tracked Nexus mod ID; its file_id/version must match the selected archive")
    install.add_argument("--archive", type=Path, required=True, help="Archive downloaded from Nexus; recorded checksums allow offline reinstall, --file-id identifies new archives")
    install.add_argument("--file-id", type=int, help="Identify a newly downloaded file through Nexus before replacing installed metadata; requires an API key, not Premium")
    for name in ("check", "update"):
        command = commands.add_parser(name, help="Report Nexus file-chain updates" if name == "check" else "Download and install Nexus file-chain updates, retaining backups")
        command.add_argument("--mod-id", type=int, help="Only process this tracked Nexus mod ID")
        if name == "check":
            command.add_argument("--fail-on-update", action="store_true", help="Exit 1 when updates are available, for CI; API/configuration errors exit 2")
    args = parser.parse_args()
    try:
        return run(args)
    except (ModError, OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        print(f"modctl: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
