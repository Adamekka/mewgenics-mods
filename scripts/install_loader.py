#!/usr/bin/env python3
"""Install the pinned SDK/community mod loader without replacing game data."""

import argparse
import hashlib
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

# Reuse the CLI's configuration, process guard, and filesystem checks without a package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import modctl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("~/.local/opt/mewtator/config.json"))
    args = parser.parse_args()
    try:
        repo = Path(__file__).resolve().parents[1]
        loader = modctl.load_json(repo / "runtime.json")["loader"]
        root, game, selection, _ = modctl.read_setup(args.config.expanduser())
        inventory_path = repo / "mods.json"
        inventory_before = inventory_path.read_bytes()
        inventory = modctl.load_inventory(inventory_path)
        url = f"https://github.com/{loader['github_repository']}/releases/download/v{loader['version']}/{loader['asset']}"
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read(modctl.MAX_ARCHIVE_BYTES + 1)
        if hashlib.sha256(payload).hexdigest() != loader["sha256"]:
            raise modctl.ModError("Loader download does not match runtime.json's SHA-256 checksum.")
        with modctl.mutation_lock(root):
            target = modctl.game_target(game, "version.dll")
            config = modctl.game_target(game, "Mods/config/core.cfg")
            existing = config.read_text() if config.exists() else ""
            # Disable the optional development console and duplicate resource-folder loading.
            settings = {"agent": "0", "resources": "0"}
            lines = existing.splitlines()
            lines = [line for line in lines if line.partition("=")[0].strip().lower() not in settings]
            lines.extend(f"{name}={value}" for name, value in settings.items())
            config_bytes = ("\n".join(lines) + "\n").encode("utf-8")
            with tempfile.TemporaryDirectory(prefix=".loader-stage-", dir=root.parent) as temporary:
                stage = Path(temporary)
                (stage / "version.dll").write_bytes(payload)
                (stage / "core.cfg").write_bytes(config_bytes)
                backup = modctl.commit_updates(inventory_path, inventory_before, root, selection, selection,
                    [(target, stage / "version.dll", Path("game/version.dll")),
                     (config, stage / "core.cfg", Path("game/Mods/config/core.cfg"))], inventory)
        print(f"Installed {loader['name']} {loader['version']}. Previous loader/config: {backup}")
        return 0
    except (modctl.ModError, OSError, ValueError, KeyError, urllib.error.URLError) as error:
        print(f"install-loader: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
