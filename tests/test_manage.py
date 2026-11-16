import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


MANAGE = Path(__file__).resolve().parents[1] / "manage"


class ManageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.work = self.root / "work"
        self.test_home = self.root / "test-home"
        self.test_home.mkdir()
        self.env = os.environ.copy()
        self.env.update(HOME=str(self.test_home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", NEXUS_API_KEY="inherited-fixture-key")
        self.git(self.root, "init", "--bare", str(self.origin))
        self.git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "main")
        self.configure(self.seed)
        self.inventory = {"schema_version": 1, "game": "mewgenics", "mods": [{"nexus_mod_id": 225, "name": "Test mod", "folder": "TestMod", "enabled": True, "file_id": 10, "version": "1.0", "install_target": "mewtator"}]}
        (self.seed / "mods.json").write_text(json.dumps(self.inventory, indent=2) + "\n")
        shutil.copy2(MANAGE, self.seed / "manage")
        (self.seed / "modctl.py").write_text("# The test wrapper supplies the external modctl command.\n")
        (self.seed / "README.md").write_text("original code\n")
        (self.seed / ".gitignore").write_text(".state/\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Initial test repository")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.git(self.seed, "push", "-u", "origin", "main")
        self.git(self.root, "clone", str(self.origin), str(self.work))
        self.configure(self.work)
        self.actual = self.root / "installed.json"
        self.actual.write_text((self.work / "mods.json").read_text())
        self.env["TEST_INSTALLED_INVENTORY"] = str(self.actual)
        self.hook_log = self.root / "git-hook-environment.log"
        self.env["TEST_GIT_HOOK_LOG"] = str(self.hook_log)
        for name in ("pre-commit", "pre-push", "post-merge", "pre-rebase"):
            hook = self.work / ".git/hooks" / name
            hook.write_text('#!/bin/sh\nif [ "${NEXUS_API_KEY+x}" = x ]; then echo leaked >> "$TEST_GIT_HOOK_LOG"; else echo clean >> "$TEST_GIT_HOOK_LOG"; fi\n')
            hook.chmod(0o755)
        key = self.test_home / ".config/mewgenics-mods/nexus-api-key"
        key.parent.mkdir(parents=True)
        key.write_text("file-fixture-key\n")
        key.chmod(0o600)
        self.key_file = key
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "python3"
        # Simulate only modctl; real Python still executes manage's locking and key permission checks.
        wrapper.write_text(f"#!{sys.executable}\n" + '''import json
import os
from pathlib import Path
import sys

if len(sys.argv) < 3 or sys.argv[1] != "modctl.py":
    os.execv(''' + repr(sys.executable) + ''', [''' + repr(sys.executable) + ''', *sys.argv[1:]])
command = sys.argv[2]
state = Path(".state")
state.mkdir(exist_ok=True)
record = {"command": command, "has_key": "NEXUS_API_KEY" in os.environ, "file_key": os.environ.get("NEXUS_API_KEY") == "file-fixture-key"}
with (state / "modctl-calls.jsonl").open("a") as output:
    output.write(json.dumps(record) + "\\n")
if command == "sync":
    if "TEST_SYNC_FIFO" in os.environ:
        (state / "sync-started").touch()
        with open(os.environ["TEST_SYNC_FIFO"]) as pause:
            pause.read()
    Path("mods.json").write_bytes(Path(os.environ["TEST_INSTALLED_INVENTORY"]).read_bytes())
if command == "update" and "TEST_UPDATE_VERSION" in os.environ:
    data = json.loads(Path("mods.json").read_text())
    data["mods"][0]["version"] = os.environ["TEST_UPDATE_VERSION"]
    Path("mods.json").write_text(json.dumps(data, indent=2) + "\\n")
''')
        wrapper.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]

    def configure(self, repo):
        self.git(repo, "config", "user.name", "Mod management test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")

    def git(self, directory, *arguments):
        return subprocess.run(["git", "-C", str(directory), *arguments], env=self.env, capture_output=True, text=True, check=True).stdout

    def manage(self, *arguments):
        return subprocess.run(["/bin/sh", str(self.work / "manage"), *arguments], cwd=self.work, env=self.env, capture_output=True, text=True, timeout=20)

    def calls(self):
        path = self.work / ".state/modctl-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def push_remote_change(self, filename, content):
        (self.seed / filename).write_text(content)
        self.git(self.seed, "add", "--", filename)
        self.git(self.seed, "commit", "-m", "Change remote " + filename)
        self.git(self.seed, "push", "origin", "HEAD")

    def assert_no_key_in_git_hooks(self):
        self.assertTrue(self.hook_log.is_file())
        self.assertEqual(set(self.hook_log.read_text().splitlines()), {"clean"})

    def test_staged_inventory_is_committed_and_pushed_without_other_staged_files(self):
        inventory = json.loads(self.actual.read_text())
        inventory["mods"][0]["enabled"] = False
        changed = json.dumps(inventory, indent=2) + "\n"
        self.actual.write_text(changed)
        (self.work / "mods.json").write_text(changed)
        (self.work / "README.md").write_text("unrelated staged work\n")
        self.git(self.work, "add", "mods.json", "README.md")
        result = self.manage("backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(self.git(self.origin, "show", "main:mods.json"))["mods"][0]["enabled"])
        self.assertEqual(self.git(self.work, "show", "HEAD:README.md"), "original code\n")
        self.assertIn("unrelated staged work", self.git(self.work, "diff", "--cached", "--", "README.md"))
        self.assertEqual(self.calls(), [{"command": "sync", "has_key": False, "file_key": False}])
        self.assert_no_key_in_git_hooks()

    def test_incoming_inventory_is_saved_but_local_installed_baseline_is_restored(self):
        remote = json.loads(self.actual.read_text())
        remote["mods"][0].update(version="2.0", enabled=False)
        self.push_remote_change("mods.json", json.dumps(remote, indent=2) + "\n")
        baseline = self.actual.read_bytes()
        result = self.manage("update")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual((self.work / "mods.json").read_bytes(), baseline)
        self.assertEqual((self.work / ".state/update-local-mods.json").read_bytes(), baseline)
        self.assertEqual(json.loads((self.work / ".state/update-incoming-mods.json").read_text()), remote)
        self.assertEqual([call["command"] for call in self.calls()], ["sync"])
        self.assertIn("Reconcile", result.stderr)
        self.assert_no_key_in_git_hooks()

    def test_sync_can_undo_a_stale_staged_inventory_without_an_empty_commit_failure(self):
        stale = json.loads(self.actual.read_text())
        stale["mods"][0]["version"] = "incorrect-staged-version"
        (self.work / "mods.json").write_text(json.dumps(stale, indent=2) + "\n")
        self.git(self.work, "add", "mods.json")
        result = self.manage("backup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "mods.json").read_bytes(), self.actual.read_bytes())
        self.assertEqual(self.git(self.work, "status", "--porcelain", "--", "mods.json"), "")
        self.assert_no_key_in_git_hooks()

    def test_non_inventory_pull_and_update_succeed_without_exposing_key_to_git(self):
        self.push_remote_change("README.md", "new management code\n")
        self.env["TEST_UPDATE_VERSION"] = "1.1"
        result = self.manage("update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / "README.md").read_text(), "new management code\n")
        self.assertEqual(json.loads(self.git(self.origin, "show", "main:mods.json"))["mods"][0]["version"], "1.1")
        self.assertEqual(self.calls(), [{"command": "sync", "has_key": False, "file_key": False}, {"command": "update", "has_key": True, "file_key": True}])
        self.assertNotIn("file-fixture-key", result.stdout + result.stderr)
        self.assert_no_key_in_git_hooks()

    def test_conflicting_inventory_pull_aborts_rebase_and_restores_actual_inventory(self):
        installed = json.loads(self.actual.read_text())
        installed["mods"][0]["version"] = "1.1"
        self.actual.write_text(json.dumps(installed, indent=2) + "\n")
        remote = json.loads((self.seed / "mods.json").read_text())
        remote["mods"][0]["version"] = "2.0"
        self.push_remote_change("mods.json", json.dumps(remote, indent=2) + "\n")
        result = self.manage("update")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.work / "mods.json").read_bytes(), self.actual.read_bytes())
        self.assertFalse((self.work / ".git/rebase-merge").exists())
        self.assertFalse((self.work / ".git/rebase-apply").exists())
        self.assertNotIn("<<<<<<<", (self.work / "mods.json").read_text())
        self.assertEqual([call["command"] for call in self.calls()], ["sync"])
        self.assert_no_key_in_git_hooks()

    def test_key_file_permissions_are_enforced_by_real_python(self):
        self.key_file.chmod(0o644)
        result = self.manage("check")
        self.assertEqual(result.returncode, 2)
        self.assertIn("permissions 600", result.stderr)
        self.assertNotIn("file-fixture-key", result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_only_file_id_install_receives_a_key(self):
        result = self.manage("install", "--mod-id", "225", "--archive", "download.zip")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.manage("install", "--mod-id", "225", "--archive", "download.zip", "--file-id", "20")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [{"command": "install", "has_key": False, "file_key": False}, {"command": "install", "has_key": True, "file_key": True}])
        self.assert_no_key_in_git_hooks()

    def test_mutating_manage_commands_are_serialized(self):
        fifo = self.root / "sync-pause"
        os.mkfifo(fifo)
        self.env["TEST_SYNC_FIFO"] = str(fifo)
        first = subprocess.Popen(["/bin/sh", str(self.work / "manage"), "backup"], cwd=self.work, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.work / ".state/sync-started").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.work / ".state/sync-started").exists())
            second = self.manage("backup")
            self.assertEqual(second.returncode, 2)
            self.assertIn("already changing", second.stderr)
            with fifo.open("w") as release:
                release.write("continue\n")
            stdout, stderr = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, stderr)
            self.assertEqual([call["command"] for call in self.calls()], ["sync"])
        finally:
            if first.poll() is None:
                os.killpg(os.getpgid(first.pid), signal.SIGTERM)
                first.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
