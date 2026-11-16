import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import zipfile

import modctl


class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mods = self.root / "mods"
        self.mods.mkdir()
        self.game = self.root / "game"
        self.game.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"mod_folder": str(self.mods), "game_install_dir": str(self.game)}))
        self.inventory_path = self.root / "mods.json"
        self.inventory = {"schema_version": 1, "game": "mewgenics", "mods": [
            {"nexus_mod_id": number, "folder": name, "name": name, "enabled": True, "install_target": "mewtator", "file_id": number * 10, "version": "1"}
            for number, name in [(1, "A"), (2, "B")]
        ]}
        self.inventory_path.write_bytes(modctl.inventory_bytes(self.inventory))
        (self.mods / "modlist.txt").write_bytes(b"A\nB\n")
        for name in ("A", "B"):
            (self.mods / name).mkdir()
            (self.mods / name / "data").write_text("old " + name)
        guard = mock.patch("modctl.assert_stopped")
        guard.start()
        self.addCleanup(guard.stop)
        self.args = argparse.Namespace(inventory=self.inventory_path, config=self.config, command="install", mod_id=1, file_id=None, archive=None)

    def archive(self, name):
        path = self.root / ("new-" + name + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(name + "/data", "new " + name)
        return path

    def test_manual_update_keeps_old_metadata_in_backup_and_disabled_state(self):
        archive = self.archive("A")
        self.inventory["mods"][0]["sha256"] = "0" * 64
        self.inventory_path.write_bytes(modctl.inventory_bytes(self.inventory))
        before = self.inventory_path.read_bytes()
        (self.mods / "modlist.txt").write_bytes(b"B\n")
        self.args.archive = archive
        self.args.file_id = 11
        with mock.patch("modctl.NexusClient") as client:
            client.return_value.files.return_value = {"files": [{"file_id": 11, "category_id": 1, "version": "2", "file_name": "official.zip"}]}
            client.return_value.get.return_value = [{"mod": {"mod_id": 1}, "file_details": {"file_id": 11}}]
            self.assertEqual(modctl.run(self.args), 0)
        updated = modctl.load_inventory(self.inventory_path)
        mod = next(item for item in updated["mods"] if item["nexus_mod_id"] == 1)
        self.assertEqual((mod["file_id"], mod["version"], mod["enabled"]), (11, "2", False))
        self.assertEqual(mod["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual((self.mods / "modlist.txt").read_bytes(), b"B\n")
        backup = next((self.root / "backups").iterdir())
        self.assertEqual((backup / "mods.json").read_bytes(), before)
        self.assertEqual((backup / "mewtator/A/data").read_text(), "old A")

    def test_manual_archive_must_match_both_nexus_ids(self):
        self.args.archive = self.archive("A")
        self.args.file_id = 11
        before = self.inventory_path.read_bytes()
        with mock.patch("modctl.NexusClient") as client:
            client.return_value.files.return_value = {"files": [{"file_id": 11, "category_id": 1, "version": "2", "file_name": "official.zip"}]}
            client.return_value.get.return_value = [{"mod": {"mod_id": 99}, "file_details": {"file_id": 11}}]
            with self.assertRaisesRegex(modctl.ModError, "does not match"):
                modctl.run(self.args)
        self.assertEqual(self.inventory_path.read_bytes(), before)
        self.assertEqual((self.mods / "A/data").read_text(), "old A")

    def test_reinstall_of_missing_enabled_folder_does_not_duplicate_selection(self):
        archive = self.archive("A")
        self.inventory["mods"][0]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.inventory_path.write_bytes(modctl.inventory_bytes(self.inventory))
        shutil.rmtree(self.mods / "A")
        self.args.archive = archive
        self.assertEqual(modctl.run(self.args), 0)
        self.assertEqual((self.mods / "modlist.txt").read_bytes(), b"A\nB\n")
        self.assertEqual([item["nexus_mod_id"] for item in modctl.load_inventory(self.inventory_path)["mods"]], [1, 2])

    def test_archive_replacement_during_api_lookup_cannot_change_installed_bytes(self):
        archive = self.archive("A")
        expected_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.args.archive = archive
        self.args.file_id = 11
        with mock.patch("modctl.NexusClient") as client:
            client.return_value.files.return_value = {"files": [{"file_id": 11, "category_id": 1, "version": "2", "file_name": "official.zip"}]}
            def replace_original(endpoint):
                with zipfile.ZipFile(archive, "w") as replacement:
                    replacement.writestr("A/data", "replacement must not be installed")
                return [{"mod": {"mod_id": 1}, "file_details": {"file_id": 11}}]
            client.return_value.get.side_effect = replace_original
            self.assertEqual(modctl.run(self.args), 0)
        self.assertEqual((self.mods / "A/data").read_text(), "new A")
        updated = next(item for item in modctl.load_inventory(self.inventory_path)["mods"] if item["nexus_mod_id"] == 1)
        self.assertEqual(updated["sha256"], expected_sha)

    def test_invalid_file_records_and_unknown_md5_have_modeled_errors(self):
        self.args.archive = self.archive("A")
        self.args.file_id = 11
        before = self.inventory_path.read_bytes()
        for malformed in (True, False):
            with self.subTest(malformed=malformed), mock.patch("modctl.NexusClient") as client:
                client.return_value.files.return_value = {"files": [None] if malformed else [{"file_id": 11, "category_id": 1, "version": "2", "file_name": "official.zip"}]}
                client.return_value.get.side_effect = modctl.NexusApiError("HTTP 404", 404)
                with self.assertRaisesRegex(modctl.ModError, "file record" if malformed else "does not match"):
                    modctl.run(self.args)
                self.assertEqual(self.inventory_path.read_bytes(), before)

    def test_restore_in_arbitrary_archive_order_keeps_recorded_order(self):
        for name in ("A", "B"):
            shutil.rmtree(self.mods / name)
        (self.mods / "modlist.txt").write_bytes(b"")
        third = dict(self.inventory["mods"][0], nexus_mod_id=3, folder="C", name="C", file_id=30)
        self.inventory["mods"].append(third)
        archives = {number: self.archive(name) for number, name in [(1, "A"), (2, "B"), (3, "C")]}
        for item in self.inventory["mods"]:
            item["sha256"] = hashlib.sha256(archives[item["nexus_mod_id"]].read_bytes()).hexdigest()
        self.inventory_path.write_bytes(modctl.inventory_bytes(self.inventory))
        for number in (3, 1, 2):
            self.args.mod_id = number
            self.args.archive = archives[number]
            self.assertEqual(modctl.run(self.args), 0)
            self.assertEqual([item["folder"] for item in modctl.load_inventory(self.inventory_path)["mods"]], ["A", "B", "C"])
        self.assertEqual((self.mods / "modlist.txt").read_bytes(), b"A\nB\nC\n")

    def test_update_records_metadata_and_preserves_actual_selection_order(self):
        archive = self.archive("A")
        (self.mods / "modlist.txt").write_bytes(b"B\nA\n")
        self.args.command = "update"
        self.args.mod_id = None
        with mock.patch("modctl.NexusClient") as client:
            client.return_value.files.side_effect = lambda number: {"files": [{"file_id": 11 if number == 1 else 20, "category_id": 1 if number == 1 else 5, "version": "2", "file_name": "new.zip"}], "file_updates": [{"old_file_id": 10, "new_file_id": 11}] if number == 1 else []}
            def download(number, file_id, destination):
                shutil.copyfile(archive, destination)
                return hashlib.sha256(destination.read_bytes()).hexdigest()
            client.return_value.download.side_effect = download
            self.assertEqual(modctl.run(self.args), 0)
        updated = modctl.load_inventory(self.inventory_path)
        self.assertEqual([item["folder"] for item in updated["mods"]], ["B", "A"])
        self.assertEqual(updated["mods"][1]["file_id"], 11)
        self.assertEqual(updated["mods"][1]["version"], "2")
        self.assertEqual((self.mods / "modlist.txt").read_bytes(), b"B\nA\n")
        self.assertEqual((self.mods / "A/data").read_text(), "new A")
        self.assertEqual((self.mods / "B/data").read_text(), "old B")

    def test_extraction_failure_leaves_installed_payload_and_inventory_unchanged(self):
        self.args.command = "update"
        self.args.mod_id = 1
        before = self.inventory_path.read_bytes()
        with mock.patch("modctl.NexusClient") as client:
            client.return_value.files.return_value = {"files": [{"file_id": 11, "category_id": 1, "version": "2"}], "file_updates": [{"old_file_id": 10, "new_file_id": 11}]}
            def corrupt_download(number, file_id, destination):
                destination.write_bytes(b"not an archive")
                return hashlib.sha256(destination.read_bytes()).hexdigest()
            client.return_value.download.side_effect = corrupt_download
            with self.assertRaisesRegex(modctl.ModError, "not a supported"):
                modctl.run(self.args)
        self.assertEqual(self.inventory_path.read_bytes(), before)
        self.assertEqual((self.mods / "A/data").read_text(), "old A")

    def test_all_mods_are_checked_before_reporting_unresolved_variants(self):
        self.args.command = "check"
        self.args.mod_id = None
        with mock.patch("modctl.NexusClient") as client:
            client.return_value.files.side_effect = [
                {"files": [], "file_updates": [{"old_file_id": 10, "new_file_id": 11}, {"old_file_id": 10, "new_file_id": 12}]},
                {"files": [{"file_id": 20, "category_id": 5, "version": "1"}], "file_updates": []},
            ]
            with self.assertRaisesRegex(modctl.ModError, "A:.*multiple update variants"):
                modctl.run(self.args)
            self.assertEqual(client.return_value.files.call_args_list, [mock.call(1), mock.call(2)])
            client.return_value.download.assert_not_called()

    def test_loader_uses_the_shared_transaction_and_retains_config_and_journal(self):
        script = Path(__file__).resolve().parents[1] / "scripts/install_loader.py"
        spec = importlib.util.spec_from_file_location("loader_test", script)
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)
        payload = b"pinned loader test payload"
        (self.root / "scripts").mkdir()
        (self.root / "runtime.json").write_text(json.dumps({"loader": {"github_repository": "example/loader", "version": "1", "asset": "version.dll", "sha256": hashlib.sha256(payload).hexdigest(), "name": "Loader"}}))
        (self.game / "version.dll").write_bytes(b"previous loader")
        config = self.game / "Mods/config/core.cfg"
        config.parent.mkdir(parents=True)
        config.write_text("agent=1\nresources=1\ndisabled=OtherMod\n")
        with mock.patch.object(loader, "__file__", str(self.root / "scripts/install_loader.py")), mock.patch("sys.argv", [str(script), "--config", str(self.config)]), mock.patch("urllib.request.urlopen", return_value=io.BytesIO(payload)):
            self.assertEqual(loader.main(), 0)
        self.assertEqual((self.game / "version.dll").read_bytes(), payload)
        self.assertIn("disabled=OtherMod", config.read_text())
        self.assertIn("agent=0", config.read_text())
        backup = next((self.root / "backups").iterdir())
        self.assertTrue((backup / "changes.json").is_file())
        self.assertEqual((backup / "game/version.dll").read_bytes(), b"previous loader")


if __name__ == "__main__":
    unittest.main()
