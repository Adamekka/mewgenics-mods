# Verification

The inventory, installer, updater, Git wrapper, and loader setup received two review rounds from Claude Opus 5.5 at high effort and GPT 6.1 Sol at xhigh effort. Final small fixes were checked with regression tests after round two.

| Finding | Review | Fix |
| --- | --- | --- |
| 1. Pulled inventory mistaken for installed payloads | Round 1, both | Retain the actual local baseline, abort conflicting rebases, and stop if the incoming inventory differs. |
| 2. Staged inventory omitted from backup commits | Round 1, both | Check all scoped Git states and commit only the inventory. |
| 3. Manual update metadata saved before installation | Round 1, both | Select an explicit Nexus file ID, identify the archive through the free MD5 API, and retain installed metadata until success. |
| 4. Disabled mods re-enabled or restore order lost | Round 1, both | Preserve live selection and retain pending entries' recorded positions. |
| 5. One failed check hides later mods; miscellaneous files rejected | Round 1, Claude | Collect check errors, stop the update batch before mutation, and accept active miscellaneous files. |
| 6. Nexus key inherited by Git or permissive keyfile accepted | Round 1, Claude | Restrict credentials to the API subprocess and enforce a regular mode-600 keyfile. |
| 7. Inconsistent loader recovery, required-field fallbacks, unclear messages | Round 1, Claude | Reuse the shared transaction, index validated required fields, and give concrete recovery commands. |
| 8. Repairing an enabled missing folder duplicates the selection | Round 2, Claude | Insert only an unselected folder and assert internal selection invariants. |
| 9. Incomplete rollback skips selection; source archive changes during verification | Round 2, Sol | Restore artifacts independently and verify/extract one private archive snapshot. |
| 10. Unknown archive or malformed API records produce unclear failures | Round 2, Claude | Model the 404 mismatch and share file-record validation. |

The process-guard regression test reproduces the observed SteamOS restriction on reading an unrelated systemd process's executable link. Additional tests cover actual command-line permission failures for same-user and foreign-user processes. No findings were deferred.

The suite contains 38 tests covering archive validation, manual archive identity, metadata and payload transactions, partial failures, restore order, selection preservation, process guards, loader recovery, real Git clones/rebases/pushes, Git-hook credential isolation, and CI exit statuses. The original 35 installation/update tests passed on macOS and ARM64 SteamOS. The CI tests check updates, no updates, and a missing API key without reading local game configuration. Python compilation, shell syntax, Git whitespace checks, and `actionlint` supplement the tests. Stored upstream patch context is exempt from whitespace normalization.

The scheduled CI checker received a separate scoped review from Claude Opus 5.5 and GPT 6.1 Sol. The resulting changes add exact free-account installation commands to update reports and document GitHub's public-repository schedule inactivity limit. The full test suite runs before the live check, so a test failure also makes the workflow fail.

Live checks confirmed Nexus metadata and MD5 queries, the free-account download restriction and its actionable message, refusal to modify a running game, and initialization of all requested DLL mods. The user confirmed the game is visible. Gameplay inside a battle was not exercised.

Unattended Nexus downloads require Premium. Free accounts download through the website and use the verified local installation command. Updates follow authors' explicit file-update chains; they do not guess variants or treat a remote Git inventory as proof of local installation.
