# SteamOS setup

## Mod loader

Always Show HP requires the [Mewgenics Mod Framework](https://github.com/pilout/mewgenics-mod-framework), pinned in `runtime.json`. Its current loader supports Mewjector's API v3, `chainloader.ini`, and Mewtator's DLL manifest, so it also loads the community DLL mods. Both loaders use the name `version.dll`; the framework replaces standalone Mewjector.

```sh
python3 scripts/install_loader.py
```

This verifies the published SHA-256, keeps the previous loader/configuration, and installs the pinned `version.dll`. The framework's development console and independent resource-folder scanning are disabled. Mewtator supplies the resource mod paths. Updating this dependency is an explicit edit of the pinned release and checksum in `runtime.json`, followed by this command.

The Windows game and its DLLs are x86-64 and run under Proton/FEX. The Mewtator desktop app is built natively for ARM64.

Always Show HP goes in `Mewgenics/Mods/AlwaysShowHP.dll`. Other requested mods are managed in Mewtator's mods folder. Mod Counter's DLL is renamed to `ModCounter.dll` because the loader already owns the basename `version.dll`; `mods.json` preserves this rename during updates.

## Native Mewtator

The current source checkout is `~/Coding/Mewtator`. Its upstream commit is pinned in `runtime.json`. The patch in `patches/mewtator-steam-launch.patch` adds the Steam entry point and regression tests.

For a fresh source checkout, check out that commit and apply the patch with `git apply`. Create a Python virtual environment, install upstream `requirements.txt`, and build using upstream `Mewtator.spec`. The build needs native ARM64 Tcl/Tk 8.6 and libXss. The existing VM keeps user-local copies under `build/native-deps/usr/lib`:

```sh
LD_LIBRARY_PATH="$PWD/build/native-deps/usr/lib" \
TCL_LIBRARY="$PWD/build/native-deps/usr/lib/tcl8.6" \
TK_LIBRARY="$PWD/build/native-deps/usr/lib/tk8.6" \
.venv/bin/python -m PyInstaller --noconfirm Mewtator.spec
```

Install `dist/Mewtator/` into `~/.local/opt/mewtator`, preserving the existing `config.json`. The source package and native build dependencies are separate from this repo. Mewtator has a desktop shortcut and application-menu entry in the VM.

## Steam launch

Mewgenics has Steam App ID `686060`. Its launch options are:

```text
"/home/steamos/.local/opt/mewtator/Mewtator" --steam %command%
```

Pressing Play reads the current Mewtator selection, prepares the DLL manifest, and executes Steam's original Proton command with the enabled mod paths. It launches the game immediately. Logs are in `~/.local/opt/mewtator/logs/`; framework logs are beside the game executable. Keep Steam's original command placeholder in these Steam options only.

Removing this launch option restores Steam's ordinary launch behavior. It does not uninstall the loader or SDK mods in the game directory.
