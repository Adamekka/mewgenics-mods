import copy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import zipfile

import modctl


class ProcessGuardTests(unittest.TestCase):
    def test_unreadable_command_line_refuses_own_process_but_skips_foreign_process(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            helper = proc_root / "1204"
            helper.mkdir()
            (helper / "comm").write_text("helper\n")
            original_read = Path.read_bytes
            path_type = Path
            def protected_read(path):
                if path == helper / "cmdline":
                    raise PermissionError("protected command line")
                return original_read(path)
            with mock.patch("modctl.Path", side_effect=lambda value: proc_root if value == "/proc" else path_type(value)), mock.patch.object(Path, "read_bytes", protected_read):
                with self.assertRaisesRegex(modctl.ModError, "Cannot inspect process"):
                    modctl.assert_stopped()
                with mock.patch("modctl.os.getuid", return_value=helper.stat().st_uid + 1):
                    modctl.assert_stopped()

    def test_protected_helper_executable_does_not_block_but_wine_game_does(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            helper = proc_root / "1204"
            helper.mkdir()
            (helper / "comm").write_text("systemd\n")
            (helper / "cmdline").write_bytes(b"/usr/lib/systemd/systemd\0--user\0")
            path_type = Path
            with mock.patch("modctl.Path", side_effect=lambda value: proc_root if value == "/proc" else path_type(value)):
                with mock.patch("modctl.os.readlink", side_effect=PermissionError("protected executable")):
                    modctl.assert_stopped()
                    game = proc_root / "42"
                    game.mkdir()
                    (game / "comm").write_text("wine-preloader\n")
                    (game / "cmdline").write_bytes(b"S:\\steamapps\\common\\Mewgenics\\Mewgenics.exe\0")
                    with self.assertRaisesRegex(modctl.ModError, "Close Mewgenics"):
                        modctl.assert_stopped()


def entry(mod_id, folder, enabled=True):
    return {"nexus_mod_id": mod_id, "name": folder, "folder": folder, "enabled": enabled, "file_id": mod_id * 10, "version": "1.0", "install_target": "mewtator"}


class UpdateChainTests(unittest.TestCase):
    def test_follows_explicit_chain_without_crossing_variants(self):
        files = {"files": [{"file_id": number, "category_id": 1, "version": str(number)} for number in (10, 20, 30, 99)], "file_updates": [{"old_file_id": 10, "new_file_id": 20}, {"old_file_id": 20, "new_file_id": 30}]}
        self.assertEqual(modctl.update_file(10, files)["file_id"], 30)
        self.assertIsNone(modctl.update_file(99, files))

    def test_ambiguous_chain_requires_explicit_selection(self):
        files = {"files": [], "file_updates": [{"old_file_id": 10, "new_file_id": 20}, {"old_file_id": 10, "new_file_id": 30}]}
        with self.assertRaisesRegex(modctl.ModError, "multiple update variants"):
            modctl.update_file(10, files)

    def test_deleted_target_and_cycle_are_reported(self):
        with self.assertRaisesRegex(modctl.ModError, "active"):
            modctl.update_file(10, {"files": [{"file_id": 20, "category_id": 7, "version": "2"}], "file_updates": [{"old_file_id": 10, "new_file_id": 20}]})
        with self.assertRaisesRegex(modctl.ModError, "cycle"):
            modctl.update_file(10, {"files": [], "file_updates": [{"old_file_id": 10, "new_file_id": 20}, {"old_file_id": 20, "new_file_id": 10}]})

    def test_null_file_id_never_chooses_latest_file(self):
        with self.assertRaisesRegex(modctl.ModError, "exact Nexus file_id"):
            modctl.update_file(None, {"files": [{"file_id": 99, "category_id": 1, "version": "9"}], "file_updates": []})


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def package(self, entries):
        archive = self.root / "mod.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for name, data in entries:
                package.writestr(name, data)
        return archive

    def test_wrapper_folders_and_metadata_are_normalized(self):
        archive = self.package([("wrapper/README.txt", "packaging"), ("wrapper/actual/description.json", '{"title":"Actual"}'), ("wrapper/actual/data/cats.patch", "payload")])
        root = modctl.extract_archive(archive, self.root / "extracted")
        self.assertEqual(root.relative_to(self.root).as_posix(), "extracted/wrapper/actual")
        self.assertEqual((root / "data/cats.patch").read_text(), "payload")

    def test_traversal_is_rejected_before_writing_outside_stage(self):
        for name in ("../escaped", "/escaped", "C:/escaped", "safe\\..\\escaped"):
            with self.subTest(name=name):
                archive = self.package([(name, "payload")])
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    with self.assertRaises(modctl.ModError):
                        modctl.extract_archive(archive, Path(directory) / "extracted")
        self.assertFalse((self.root / "escaped").exists())

    def test_symlinks_and_duplicate_paths_are_rejected(self):
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.package([(link, "../outside")])
        with self.assertRaisesRegex(modctl.ModError, "link"):
            modctl.extract_archive(archive, self.root / "links")
        duplicate = self.package([("payload", "one"), ("payload", "two")])
        with self.assertRaisesRegex(modctl.ModError, "duplicate"):
            modctl.extract_archive(duplicate, self.root / "duplicates")

    def test_required_dll_rename_is_applied_without_changing_bytes(self):
        archive = self.package([("Mod/version.dll", b"dll payload")])
        root, digest = modctl.stage_mod(archive, self.root / "extracted", dict(entry(186, "Mod"), file_renames={"version.dll": "ModCounter.dll"}))
        self.assertEqual((root / "ModCounter.dll").read_bytes(), b"dll payload")
        self.assertFalse((root / "version.dll").exists())
        self.assertEqual(len(digest), 64)


class InventoryAndTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mods = self.root / "mods"
        self.mods.mkdir()
        self.game = self.root / "game"
        self.game.mkdir()
        self.inventory_path = self.root / "mods.json"
        self.inventory = {"schema_version": 1, "game": "mewgenics", "mods": [entry(1, "A"), entry(2, "B", False)]}
        self.original = modctl.inventory_bytes(self.inventory)
        self.inventory_path.write_bytes(self.original)
        self.selection = b"A\n"
        (self.mods / "modlist.txt").write_bytes(self.selection)
        for name in ("A", "B"):
            (self.mods / name).mkdir()
            (self.mods / name / "data").write_text("old " + name)

    def test_sync_preserves_ids_and_records_actual_disabled_state_and_order(self):
        result = modctl.synced_inventory(self.inventory, self.mods, self.game, ["B"])
        self.assertEqual([mod["nexus_mod_id"] for mod in result["mods"]], [2, 1])
        self.assertEqual([mod["enabled"] for mod in result["mods"]], [True, False])
        self.assertEqual([mod["file_id"] for mod in result["mods"]], [20, 10])
        self.assertTrue(self.inventory["mods"][0]["enabled"])

    def test_update_preserves_selection_and_keeps_payload_backup_outside_mod_root(self):
        staged = self.root / "new-A"
        staged.mkdir()
        (staged / "data").write_text("new A")
        after = copy.deepcopy(self.inventory)
        after["mods"][0]["version"] = "2"
        changes = modctl.owned_changes(self.inventory["mods"][0], staged, self.mods, self.game)
        backup = modctl.commit_updates(self.inventory_path, self.original, self.mods, self.selection, self.selection, changes, after)
        self.assertEqual((self.mods / "modlist.txt").read_bytes(), self.selection)
        self.assertEqual((self.mods / "A/data").read_text(), "new A")
        self.assertEqual((backup / "mewtator/A/data").read_text(), "old A")
        self.assertFalse(backup.is_relative_to(self.mods))
        self.assertEqual((self.mods / "B/data").read_text(), "old B")

    def test_second_replacement_failure_rolls_back_files_selection_and_inventory(self):
        first = self.root / "new-A"
        first.mkdir()
        (first / "data").write_text("new A")
        missing = self.root / "missing-stage"
        changes = [(self.mods / "A", first, Path("mewtator/A")), (self.mods / "B", missing, Path("mewtator/B"))]
        with self.assertRaisesRegex(modctl.ModError, "previous mods and selection were restored"):
            modctl.commit_updates(self.inventory_path, self.original, self.mods, self.selection, b"B\n", changes, self.inventory)
        self.assertEqual((self.mods / "A/data").read_text(), "old A")
        self.assertEqual((self.mods / "B/data").read_text(), "old B")
        self.assertEqual((self.mods / "modlist.txt").read_bytes(), self.selection)
        self.assertEqual(self.inventory_path.read_bytes(), self.original)

    def test_inventory_restore_failure_does_not_skip_selection_restore(self):
        staged = self.root / "new-C"
        staged.mkdir()
        (staged / "data").write_text("new C")
        original_write = modctl.atomic_write
        def deny_inventory(path, contents):
            if path == self.inventory_path:
                raise PermissionError("inventory is read-only")
            original_write(path, contents)
        with mock.patch("modctl.atomic_write", side_effect=deny_inventory):
            with self.assertRaisesRegex(modctl.ModError, "rollback was incomplete"):
                modctl.commit_updates(self.inventory_path, self.original, self.mods, self.selection, b"A\nC\n", [(self.mods / "C", staged, Path("mewtator/C"))], self.inventory)
        self.assertEqual((self.mods / "modlist.txt").read_bytes(), self.selection)
        self.assertFalse((self.mods / "C").exists())

    def test_game_file_install_owns_only_explicit_mapped_files(self):
        staged = self.root / "framework-mod"
        staged.mkdir()
        (staged / "AlwaysShowHP.dll").write_bytes(b"HP DLL")
        (staged / "unrelated.txt").write_text("not installed")
        (self.game / "Mewgenics.exe").write_bytes(b"existing game")
        mod = dict(entry(301, "Always Show HP"), install_target="game_files", file_map={"AlwaysShowHP.dll": "Mods/AlwaysShowHP.dll"})
        after = copy.deepcopy(self.inventory)
        after["mods"].append(mod)
        backup = modctl.commit_updates(self.inventory_path, self.original, self.mods, self.selection, self.selection, modctl.owned_changes(mod, staged, self.mods, self.game), after)
        self.assertEqual((self.game / "Mods/AlwaysShowHP.dll").read_bytes(), b"HP DLL")
        self.assertEqual((self.game / "Mewgenics.exe").read_bytes(), b"existing game")
        self.assertFalse((self.game / "unrelated.txt").exists())
        self.assertTrue(modctl.game_files_enabled(mod, self.game))
        self.assertEqual(json.loads((backup / "changes.json").read_text())["changes"][0]["existed"], False)

    def test_changed_inventory_or_selection_prevents_any_replacement(self):
        self.inventory_path.write_text("changed")
        with self.assertRaisesRegex(modctl.ModError, "changed while preparing"):
            modctl.commit_updates(self.inventory_path, self.original, self.mods, self.selection, self.selection, [], self.inventory)
        self.assertEqual((self.mods / "A/data").read_text(), "old A")
        self.assertFalse((self.root / "backups").exists())

    def test_game_target_symlink_and_duplicate_ownership_are_rejected(self):
        (self.game / "Mods").symlink_to(self.mods, target_is_directory=True)
        with self.assertRaisesRegex(modctl.ModError, "symlink"):
            modctl.game_target(self.game, "Mods/Foo.dll")
        self.inventory["mods"] = [dict(entry(1, "A"), install_target="game_files", file_map={"a.dll": "Mods/a.dll"}), dict(entry(2, "B"), install_target="game_files", file_map={"b.dll": "mods/A.dll"})]
        self.inventory_path.write_bytes(modctl.inventory_bytes(self.inventory))
        with self.assertRaisesRegex(modctl.ModError, "same game file"):
            modctl.load_inventory(self.inventory_path)


if __name__ == "__main__":
    unittest.main()
