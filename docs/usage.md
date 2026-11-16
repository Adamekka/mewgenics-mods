# Usage and recovery

Run these commands inside the SteamOS VM from `~/Coding/mewgenics-mods`. Close Mewgenics and Mewtator before commands that change the inventory or installed files.

## Back up the selection

```sh
./manage backup
```

This reads Mewtator's current enabled selection and load order, updates `mods.json`, commits that file, and pushes it to the public GitHub repo. Other staged or uncommitted files are excluded from the commit. New mod folders require a Nexus ID in the inventory before they can be backed up.

`./manage status` compares the JSON with the actual folders and selection. `python3 modctl.py sync` updates only the local JSON when you do not want a Git commit or push.

The inventory records Nexus mod IDs, exact file IDs, versions, enabled state, installation locations, archive checksums, and required filename changes. A Nexus ID alone cannot distinguish optional variants, such as the three Show Set Bonuses downloads. List order records Mewtator's enabled load order.

Always Show HP is installed through the SDK loader, outside Mewtator. Its inventory state records whether its owned DLL is installed. Use F10 in the game to change its runtime settings.

## Check or install updates

Store the Nexus API key in `~/.config/mewgenics-mods/nexus-api-key`, with file permissions `600`. This file is required by the `manage check` and `manage update` commands and is never added to Git. The standalone Python CLI instead requires the `NEXUS_API_KEY` environment variable for Nexus API requests.

```sh
./manage check
./manage update
./manage update --mod-id 225
```

`update` first records local choices, pulls this repo with rebase, follows each author's Nexus file-update chain, downloads and stages the new files, installs them, and commits and pushes the revised inventory. It preserves the current enabled selection and disabled mods. If the pull changes the inventory, it retains the incoming copy under `.state`, restores the actual local inventory, and stops for reconciliation. A failed conflicting rebase is aborted. Other repo updates can be pulled normally.

All selected mods are checked before errors are reported. Ambiguous update variants stop the batch before anything is installed. If an author uploads a file without linking it as an update of your selected file, review and select its exact file ID explicitly.

Nexus permits unattended API downloads only for Premium accounts. Free accounts can check versions, but a request to download a newer file can be rejected. No subscription is purchased by these scripts.

For a free account, download the selected archive through the normal Nexus website. Give the installation command the exact file ID shown by `check` or the website download URL:

```sh
./manage install --mod-id 225 --file-id 1330 --archive "$HOME/Downloads/selected-mod.zip"
```

This works with the free Nexus API. It identifies the archive's MD5 against both Nexus IDs, obtains the correct version, and records the new SHA-256 only after installation succeeds. The old metadata stays with the old payload in the rollback backup. Do not hand-edit installed version fields before updating.

When restoring the exact archive already recorded in `mods.json`, omit `--file-id` to verify its recorded SHA-256 offline, without an API key.

The scripts run when invoked. They do not install a scheduler or update mods while the game is running.

## Restore on another installation

Clone this repo, set up Mewtator and the pinned loader as described in [SteamOS setup](steamos-setup.md), and download the selected Nexus files listed in `mods.json`. Run the local install command for each downloaded archive. New enabled entries are inserted in their recorded order, even when archives are installed in a different order. Existing installed mods keep their live enabled/disabled state. Run `./manage backup` after restoring the selection.

For different game/mod paths, use the standalone CLI's explicit configuration option:

```sh
python3 modctl.py --config /absolute/path/to/config.json status
```

## Recover previous files

Every installation keeps a timestamped backup under the parent of Mewtator's mods folder, currently `~/.local/share/mewtator/backups/`. It saves the prior inventory, `modlist.txt`, changed mod folders, and explicitly owned game files. `changes.json` lists each installed target and its saved previous location. Loader installation uses the same transaction and journal for the prior `version.dll` and framework configuration.

If a normal installation step fails, the CLI restores the previous files, inventory, and selection. If rollback fails, it reports the backup directory and the remaining filesystem errors. Backups remain available for manual recovery. They are kept outside the mods folder so Mewtator cannot mistake them for installed mods.

To undo an incompatible update after it installed successfully, close the game and Mewtator, restore the targets listed in that backup's `changes.json`, and restore its `mods.json` and `modlist.txt`. A target marked `existed: false` should be removed rather than restored from a nonexistent previous file. Back up the recovered selection before the next update.

Game saves and downloaded archives are separate from this inventory backup.
