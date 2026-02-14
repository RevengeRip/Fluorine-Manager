# -*- encoding: utf-8 -*-
from __future__ import annotations

"""
Root Builder plugin for Mod Organizer 2 (Linux port).

Deploys files from mod Root/ subdirectories to the game's root directory.
Supports copy (with reflink/CoW) and symlink modes, with automatic
deploy/clear on game launch/close.
"""

import json
import os
import shutil
import subprocess

import mobase
from PyQt6.QtCore import qInfo, qWarning
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

# Storage lives under <instance_dir>/rootbuilder/ — NOT the game directory,
# which Steam/Wine may make read-only during gameplay.
_STORAGE_SUBDIR = "rootbuilder"
_MANIFEST_NAME = "manifest.json"
_SETTINGS_NAME = "settings.json"
_BACKUP_SUBDIR = "backup"

# Legacy names (stored in game dir by older versions)
_LEGACY_MANIFEST = ".rootbuilder_manifest.json"
_LEGACY_BACKUP = ".rootbuilder_backup"


def _find_root_dir(mod_path: str) -> str | None:
    """Find a 'Root' subdirectory (case-insensitive) inside a mod."""
    try:
        for entry in os.scandir(mod_path):
            if entry.is_dir() and entry.name.lower() == "root":
                return entry.path
    except OSError:
        pass
    return None


def _walk_files(root_dir: str):
    """Yield all file paths under root_dir recursively."""
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for name in filenames:
            yield os.path.join(dirpath, name)


def _host_cp(src: str, dst: str) -> bool:
    """Copy a file via the host OS (bypasses Flatpak sandbox restrictions)."""
    try:
        result = subprocess.run(
            ["flatpak-spawn", "--host", "cp", "-f", "--reflink=auto", "--", src, dst],
            capture_output=True, timeout=30,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _reflink_copy(src: str, dst: str):
    """Copy with reflink (CoW) if supported, fallback to regular copy."""
    try:
        subprocess.run(
            ["cp", "--reflink=auto", "-f", "--", src, dst],
            check=True,
            capture_output=True,
        )
        return
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    try:
        shutil.copy2(src, dst)
        return
    except OSError:
        pass
    if _IN_FLATPAK and _host_cp(src, dst):
        return
    raise OSError(f"Root Builder: failed to copy {src} -> {dst}")


def _ensure_writable(path: str):
    """Make a file or directory writable by the owner."""
    try:
        st = os.stat(path)
        os.chmod(path, st.st_mode | 0o200)
    except OSError:
        pass


_IN_FLATPAK = os.path.exists("/.flatpak-info")


def _host_rm(path: str) -> bool:
    """Remove a file via the host OS (bypasses Flatpak sandbox restrictions)."""
    try:
        result = subprocess.run(
            ["flatpak-spawn", "--host", "rm", "-f", "--", path],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _host_mv(src: str, dst: str) -> bool:
    """Move a file via the host OS (bypasses Flatpak sandbox restrictions)."""
    try:
        result = subprocess.run(
            ["flatpak-spawn", "--host", "mv", "-f", "--", src, dst],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _force_remove(path: str) -> bool:
    """Remove a file, fixing permissions if needed. Returns True on success."""
    try:
        os.remove(path)
        return True
    except PermissionError:
        pass
    try:
        _ensure_writable(os.path.dirname(path))
        _ensure_writable(path)
        os.remove(path)
        return True
    except OSError:
        pass
    if _IN_FLATPAK and _host_rm(path):
        return True
    qWarning(f"Root Builder: could not remove {path}")
    return False


def _rmtree_onerror(_func, path, _exc_info):
    """onerror handler for shutil.rmtree that fixes permissions and retries."""
    try:
        _ensure_writable(os.path.dirname(path))
        _ensure_writable(path)
        os.remove(path)
    except OSError:
        pass


def _cleanup_empty_dirs(base_dir: str, paths: list[str]):
    """Remove empty directories left behind after clearing deployed files."""
    dirs_to_check = set()
    for path in paths:
        parent = os.path.dirname(path)
        while parent and parent != base_dir:
            try:
                if os.path.samefile(parent, base_dir):
                    break
            except OSError:
                break
            dirs_to_check.add(parent)
            parent = os.path.dirname(parent)

    for d in sorted(dirs_to_check, key=len, reverse=True):
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass


# --- Manifest helpers (operate on storage_dir, NOT game dir) ---

def _load_manifest(storage_dir: str) -> dict | None:
    path = os.path.join(storage_dir, _MANIFEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_manifest(storage_dir: str, manifest: dict):
    os.makedirs(storage_dir, exist_ok=True)
    path = os.path.join(storage_dir, _MANIFEST_NAME)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def _remove_manifest(storage_dir: str):
    path = os.path.join(storage_dir, _MANIFEST_NAME)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


class RootBuilderDialog(QDialog):
    """Small settings/control dialog shown from the Tools menu."""

    def __init__(self, settings: dict, save_fn, build_fn, clear_fn, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._save_fn = save_fn
        self._build_fn = build_fn
        self._clear_fn = clear_fn

        self.setWindowTitle("Root Builder")
        self.resize(350, 220)

        layout = QVBoxLayout(self)

        desc = QLabel("Deploys files from mod Root/ folders to the game directory.")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Enable checkbox
        self._enableCheck = QCheckBox("Auto-deploy on game launch")
        self._enableCheck.setChecked(settings.get("enabled", False) is True)
        layout.addWidget(self._enableCheck)

        # Mode selector
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Deploy mode:"))
        self._modeCombo = QComboBox()
        self._modeCombo.addItems(["copy", "link"])
        self._modeCombo.setCurrentText(settings.get("mode", "copy"))
        mode_layout.addWidget(self._modeCombo)
        layout.addLayout(mode_layout)

        # Manual build/clear buttons
        btn_layout = QHBoxLayout()
        build_btn = QPushButton("Build Now")
        build_btn.clicked.connect(self._on_build)
        clear_btn = QPushButton("Clear Now")
        clear_btn.clicked.connect(self._on_clear)
        btn_layout.addWidget(build_btn)
        btn_layout.addWidget(clear_btn)
        layout.addLayout(btn_layout)

        # Status label
        self._status = QLabel("")
        layout.addWidget(self._status)

        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        # Save settings whenever the user changes them
        self._enableCheck.stateChanged.connect(lambda _: self._do_save())
        self._modeCombo.currentTextChanged.connect(lambda _: self._do_save())

    def _do_save(self):
        self._settings["enabled"] = self._enableCheck.isChecked()
        self._settings["mode"] = self._modeCombo.currentText()
        self._save_fn(self._settings)

    def _on_build(self):
        count = self._build_fn()
        self._status.setText(f"Deployed {count} file(s).")

    def _on_clear(self):
        count = self._clear_fn()
        self._status.setText(f"Cleared {count} file(s).")


class RootBuilder(mobase.IPluginTool):
    _organizer: mobase.IOrganizer

    def __init__(self):
        super().__init__()
        self.__parentWidget = None

    # --- IPlugin ---

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        self._migrate_legacy()
        self._check_third_party_rootbuilder()
        organizer.onAboutToRun(self._on_about_to_run)
        organizer.onFinishedRun(self._on_finished_run)
        return True

    # --- Storage paths (instance dir, always writable) ---

    def _storage_dir(self) -> str:
        d = os.path.join(self._organizer.basePath(), _STORAGE_SUBDIR)
        os.makedirs(d, exist_ok=True)
        return d

    # --- Settings (our own JSON, not pluginSetting) ---

    def _load_settings(self) -> dict:
        path = os.path.join(self._storage_dir(), _SETTINGS_NAME)
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return {"enabled": False, "mode": "copy"}

    def _save_settings(self, settings: dict):
        path = os.path.join(self._storage_dir(), _SETTINGS_NAME)
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)

    def _is_enabled(self) -> bool:
        return self._load_settings().get("enabled", False) is True

    # --- Legacy migration ---

    def _migrate_legacy(self):
        """Move legacy manifest/backup from game dir to our storage dir."""
        game = self._organizer.managedGame()
        if game is None:
            return
        game_dir = game.gameDirectory().absolutePath()
        storage = self._storage_dir()

        old_manifest = os.path.join(game_dir, _LEGACY_MANIFEST)
        if os.path.isfile(old_manifest):
            try:
                new_path = os.path.join(storage, _MANIFEST_NAME)
                if not os.path.isfile(new_path):
                    shutil.copy2(old_manifest, new_path)
                _force_remove(old_manifest)
            except OSError:
                pass

        old_backup = os.path.join(game_dir, _LEGACY_BACKUP)
        if os.path.isdir(old_backup):
            try:
                new_backup = os.path.join(storage, _BACKUP_SUBDIR)
                if not os.path.isdir(new_backup):
                    shutil.copytree(old_backup, new_backup)
                shutil.rmtree(old_backup, onerror=_rmtree_onerror)
            except OSError:
                pass

    def _check_third_party_rootbuilder(self):
        """Move any third-party Root Builder plugins into DisabledPlugins/."""
        plugins_dir = os.path.dirname(os.path.abspath(__file__))
        disabled_dir = os.path.join(os.path.dirname(plugins_dir), "DisabledPlugins")
        my_file = os.path.basename(__file__)

        conflicts = []
        for entry in os.scandir(plugins_dir):
            if entry.is_dir() and entry.name.lower() == "rootbuilder":
                conflicts.append((entry.name, entry.path))
            elif (
                entry.is_file()
                and entry.name.lower().startswith("rootbuilder")
                and entry.name.lower().endswith(".py")
                and entry.name != my_file
            ):
                conflicts.append((entry.name, entry.path))

        for name, path in conflicts:
            dst = os.path.join(disabled_dir, name)
            try:
                os.makedirs(disabled_dir, exist_ok=True)
                shutil.move(path, dst)
                qInfo(
                    f"Root Builder: moved incompatible third-party plugin "
                    f"'{name}' to DisabledPlugins/. "
                    f"It uses Windows-only USVFS and cannot work on Linux."
                )
            except OSError as e:
                qWarning(
                    f"Root Builder: failed to move third-party plugin "
                    f"'{name}' to DisabledPlugins/: {e}"
                )

    def name(self) -> str:
        return "Root Builder"

    def localizedName(self) -> str:
        return "Root Builder"

    def author(self) -> str:
        return "Fluorine Manager"

    def description(self) -> str:
        return (
            "Deploys mod files from Root/ subdirectories to the game's root directory. "
            "Supports copy and symlink modes with auto-deploy on launch."
        )

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(1, 0, 0)

    def enabledByDefault(self) -> bool:
        return True

    def settings(self) -> list[mobase.PluginSetting]:
        return []

    # --- IPluginTool ---

    def displayName(self) -> str:
        return "Root Builder"

    def tooltip(self) -> str:
        return "Deploy mod Root/ files to the game directory"

    def icon(self) -> QIcon:
        return QIcon()

    def setParentWidget(self, widget):
        self.__parentWidget = widget

    def display(self):
        settings = self._load_settings()
        dialog = RootBuilderDialog(
            settings, self._save_settings, self._build, self._clear,
            self.__parentWidget,
        )
        dialog.exec()

    # --- Hooks ---

    def _on_about_to_run(self, executable: str) -> bool:
        if self._is_enabled():
            self._build()
        return True

    def _on_finished_run(self, executable: str, exit_code: int):
        if self._is_enabled():
            self._clear()

    # --- Build / Clear ---

    def _build(self) -> int:
        """Deploy root files from all active mods. Returns number of files deployed."""
        game_dir = self._organizer.managedGame().gameDirectory().absolutePath()
        storage = self._storage_dir()
        mod_list = self._organizer.modList()
        mods = mod_list.allModsByProfilePriority()
        mode = self._load_settings().get("mode", "copy")

        # Clear any previous deployment first
        if _load_manifest(storage) is not None:
            self._clear()

        manifest = {"deployed": [], "backups": {}}
        backup_dir = os.path.join(storage, _BACKUP_SUBDIR)
        deployed_set = set()

        for mod_name in mods:
            if not (mod_list.state(mod_name) & mobase.ModState.ACTIVE):
                continue

            mod = mod_list.getMod(mod_name)
            if mod is None:
                continue
            if mod.isSeparator() or mod.isBackup() or mod.isForeign():
                continue

            mod_path = mod.absolutePath()
            root_dir = _find_root_dir(mod_path)
            if root_dir is None:
                continue

            for src_file in _walk_files(root_dir):
                rel = os.path.relpath(src_file, root_dir)
                dst = os.path.join(game_dir, rel)

                try:
                    # Backup existing file if not already deployed by us
                    if os.path.exists(dst) and dst not in deployed_set:
                        bak = os.path.join(backup_dir, rel)
                        os.makedirs(os.path.dirname(bak), exist_ok=True)
                        try:
                            shutil.copy2(dst, bak)
                        except PermissionError:
                            _ensure_writable(dst)
                            shutil.copy2(dst, bak)
                        manifest["backups"][dst] = bak

                    os.makedirs(os.path.dirname(dst), exist_ok=True)

                    if os.path.lexists(dst):
                        _force_remove(dst)

                    # In link mode, .exe and .dll must be copied — Wine/Proton
                    # resolves a symlinked exe's path to the target, so the
                    # process can't find sibling files in the game directory.
                    ext = os.path.splitext(src_file)[1].lower()
                    if mode == "link" and ext not in (".exe", ".dll"):
                        os.symlink(src_file, dst)
                    else:
                        _reflink_copy(src_file, dst)

                    if dst not in deployed_set:
                        manifest["deployed"].append(dst)
                        deployed_set.add(dst)
                except OSError as e:
                    qWarning(f"Root Builder: could not deploy {rel}: {e}")

        _save_manifest(storage, manifest)
        return len(manifest["deployed"])

    def _clear(self) -> int:
        """Remove deployed files and restore backups. Returns count of removed files."""
        game_dir = self._organizer.managedGame().gameDirectory().absolutePath()
        storage = self._storage_dir()
        manifest = _load_manifest(storage)
        if manifest is None:
            return 0

        count = 0
        failed = []

        # Remove deployed files
        for path in manifest["deployed"]:
            if os.path.lexists(path):
                if _force_remove(path):
                    count += 1
                else:
                    failed.append(path)

        # Restore backups
        for dst, bak in manifest["backups"].items():
            if os.path.exists(bak):
                try:
                    parent = os.path.dirname(dst)
                    os.makedirs(parent, exist_ok=True)
                    _ensure_writable(parent)
                    if os.path.lexists(dst):
                        _force_remove(dst)
                    shutil.move(bak, dst)
                except OSError:
                    if not (_IN_FLATPAK and _host_mv(bak, dst)):
                        qWarning(
                            f"Root Builder: could not restore backup "
                            f"{bak} -> {dst}"
                        )

        # Clean up backup dir
        backup_dir = os.path.join(storage, _BACKUP_SUBDIR)
        if os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)

        if failed:
            # Update manifest to only contain files we couldn't remove,
            # so the next clear attempt can retry them.
            manifest["deployed"] = failed
            manifest["backups"] = {}
            _save_manifest(storage, manifest)
            qWarning(
                f"Root Builder: {len(failed)} file(s) could not be removed. "
                f"They will be retried on next clear."
            )
        else:
            _remove_manifest(storage)

        _cleanup_empty_dirs(game_dir, [p for p in manifest["deployed"] if p not in failed])
        return count


def createPlugin() -> mobase.IPlugin:
    return RootBuilder()
