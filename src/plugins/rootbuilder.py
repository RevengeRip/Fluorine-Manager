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

MANIFEST_NAME = ".rootbuilder_manifest.json"
BACKUP_DIR_NAME = ".rootbuilder_backup"


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


def _reflink_copy(src: str, dst: str):
    """Copy with reflink (CoW) if supported, fallback to regular copy."""
    try:
        subprocess.run(
            ["cp", "--reflink=auto", "--", src, dst],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        shutil.copy2(src, dst)


def _manifest_path(game_dir: str) -> str:
    return os.path.join(game_dir, MANIFEST_NAME)


def _load_manifest(game_dir: str) -> dict | None:
    path = _manifest_path(game_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_manifest(game_dir: str, manifest: dict):
    path = _manifest_path(game_dir)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def _remove_manifest(game_dir: str):
    path = _manifest_path(game_dir)
    if os.path.isfile(path):
        os.remove(path)


def _cleanup_empty_dirs(game_dir: str, deployed: list[str]):
    """Remove empty directories left behind after clearing deployed files."""
    dirs_to_check = set()
    for path in deployed:
        parent = os.path.dirname(path)
        while parent and parent != game_dir and not os.path.samefile(parent, game_dir):
            dirs_to_check.add(parent)
            parent = os.path.dirname(parent)

    for d in sorted(dirs_to_check, key=len, reverse=True):
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass


class RootBuilderDialog(QDialog):
    """Small settings/control dialog shown from the Tools menu."""

    def __init__(self, organizer: mobase.IOrganizer, build_fn, clear_fn, parent=None):
        super().__init__(parent)
        self._organizer = organizer
        self._build_fn = build_fn
        self._clear_fn = clear_fn
        self._plugin_name = "Root Builder"

        self.setWindowTitle("Root Builder")
        self.resize(350, 220)

        layout = QVBoxLayout(self)

        desc = QLabel("Deploys files from mod Root/ folders to the game directory.")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Enable checkbox
        self._enableCheck = QCheckBox("Auto-deploy on game launch")
        self._enableCheck.setChecked(
            bool(organizer.pluginSetting(self._plugin_name, "enabled"))
        )
        layout.addWidget(self._enableCheck)

        # Mode selector
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Deploy mode:"))
        self._modeCombo = QComboBox()
        self._modeCombo.addItems(["copy", "link"])
        current = organizer.pluginSetting(self._plugin_name, "mode")
        self._modeCombo.setCurrentText(current if current else "copy")
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

    def _save_settings(self):
        self._organizer.setPluginSetting(
            self._plugin_name, "enabled", self._enableCheck.isChecked()
        )
        self._organizer.setPluginSetting(
            self._plugin_name, "mode", self._modeCombo.currentText()
        )

    def _on_build(self):
        self._save_settings()
        count = self._build_fn()
        self._status.setText(f"Deployed {count} file(s).")

    def _on_clear(self):
        self._save_settings()
        count = self._clear_fn()
        self._status.setText(f"Cleared {count} file(s).")

    def accept(self):
        self._save_settings()
        super().accept()


class RootBuilder(mobase.IPluginTool):
    _organizer: mobase.IOrganizer

    def __init__(self):
        super().__init__()
        self.__parentWidget = None

    # --- IPlugin ---

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        self._check_third_party_rootbuilder()
        organizer.onAboutToRun(self._on_about_to_run)
        organizer.onFinishedRun(self._on_finished_run)
        return True

    def _check_third_party_rootbuilder(self):
        """Move any third-party Root Builder plugins into DisabledPlugins/."""
        plugins_dir = os.path.dirname(os.path.abspath(__file__))
        disabled_dir = os.path.join(os.path.dirname(plugins_dir), "DisabledPlugins")
        my_file = os.path.basename(__file__)

        # Collect conflicts first, then move (don't modify dir during iteration)
        conflicts = []
        for entry in os.scandir(plugins_dir):
            # Kezyma's standard install: plugins/rootbuilder/
            if entry.is_dir() and entry.name.lower() == "rootbuilder":
                conflicts.append((entry.name, entry.path))
            # Other rootbuilder*.py files that aren't us
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
                mobase.log(
                    mobase.LogLevel.INFO,
                    f"Root Builder: moved incompatible third-party plugin "
                    f"'{name}' to DisabledPlugins/. "
                    f"It uses Windows-only USVFS and cannot work on Linux.",
                )
            except OSError as e:
                mobase.log(
                    mobase.LogLevel.WARNING,
                    f"Root Builder: failed to move third-party plugin "
                    f"'{name}' to DisabledPlugins/: {e}",
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
        return False

    def settings(self) -> list[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("mode", "Deploy mode: copy or link", "copy"),
            mobase.PluginSetting(
                "enabled", "Auto-deploy root files on launch", True
            ),
        ]

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
        dialog = RootBuilderDialog(
            self._organizer, self._build, self._clear, self.__parentWidget
        )
        dialog.exec()

    # --- Hooks ---

    def _on_about_to_run(self, executable: str) -> bool:
        if self._organizer.pluginSetting(self.name(), "enabled"):
            self._build()
        return True

    def _on_finished_run(self, executable: str, exit_code: int):
        if self._organizer.pluginSetting(self.name(), "enabled"):
            self._clear()

    # --- Build / Clear ---

    def _build(self) -> int:
        """Deploy root files from all active mods. Returns number of files deployed."""
        game_dir = self._organizer.managedGame().gameDirectory().absolutePath()
        mod_list = self._organizer.modList()
        mods = mod_list.allModsByProfilePriority()
        mode = self._organizer.pluginSetting(self.name(), "mode") or "copy"

        # Clear any previous deployment first
        if _load_manifest(game_dir) is not None:
            self._clear()

        manifest = {"deployed": [], "backups": {}}
        backup_dir = os.path.join(game_dir, BACKUP_DIR_NAME)
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

                # Backup existing file if not already deployed by us
                if os.path.exists(dst) and dst not in deployed_set:
                    bak = os.path.join(backup_dir, rel)
                    os.makedirs(os.path.dirname(bak), exist_ok=True)
                    shutil.move(dst, bak)
                    manifest["backups"][dst] = bak

                os.makedirs(os.path.dirname(dst), exist_ok=True)

                if os.path.lexists(dst):
                    os.remove(dst)

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

        _save_manifest(game_dir, manifest)
        return len(manifest["deployed"])

    def _clear(self) -> int:
        """Remove deployed files and restore backups. Returns count of removed files."""
        game_dir = self._organizer.managedGame().gameDirectory().absolutePath()
        manifest = _load_manifest(game_dir)
        if manifest is None:
            return 0

        count = 0

        # Remove deployed files
        for path in manifest["deployed"]:
            if os.path.lexists(path):
                os.remove(path)
                count += 1

        # Restore backups
        for dst, bak in manifest["backups"].items():
            if os.path.exists(bak):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(bak, dst)

        # Clean up backup dir
        backup_dir = os.path.join(game_dir, BACKUP_DIR_NAME)
        if os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)

        _remove_manifest(game_dir)
        _cleanup_empty_dirs(game_dir, manifest["deployed"])
        return count


def createPlugin() -> mobase.IPlugin:
    return RootBuilder()
