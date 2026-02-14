import json
import shutil
import traceback
import urllib.request
import zipfile
from functools import cached_property

from PyQt6.QtCore import qDebug, qWarning
from PyQt6.QtWidgets import QApplication, QMessageBox

from . import bg3_utils


class LSLibRetriever:
    def __init__(self, utils: bg3_utils.BG3Utils):
        self._utils = utils

    # Files that MUST be present for Divine to work.
    _REQUIRED_FILES = {
        "Divine.dll",
        "Divine.exe",
        "LSLib.dll",
    }

    # Additional files that may or may not be in a given LSLib release.
    _OPTIONAL_FILES = {
        "CommandLineArgumentsParser.dll",
        "Divine.dll.config",
        "Divine.runtimeconfig.json",
        "K4os.Compression.LZ4.dll",
        "K4os.Compression.LZ4.Streams.dll",
        "LSLibNative.dll",
        "LZ4.dll",
        "Newtonsoft.Json.dll",
        "System.IO.Hashing.dll",
        "ZstdSharp.dll",
    }

    @cached_property
    def _needed_lslib_files(self):
        return {
            self._utils.tools_dir / x
            for x in self._REQUIRED_FILES | self._OPTIONAL_FILES
        }

    def download_lslib_if_missing(self, force: bool = False) -> bool:
        # Only check required files to avoid re-downloading when optional
        # files were not present in the archive.
        required_paths = {
            self._utils.tools_dir / x for x in self._REQUIRED_FILES
        }
        if not force and all(x.exists() for x in required_paths):
            return True
        try:
            self._utils.tools_dir.mkdir(exist_ok=True, parents=True)
            downloaded = False

            def reporthook(block_num: int, block_size: int, total_size: int) -> None:
                if total_size > 0:
                    progress.setValue(
                        min(int(block_num * block_size * 100 / total_size), 100)
                    )
                    QApplication.processEvents()

            with urllib.request.urlopen(
                "https://api.github.com/repos/Norbyte/lslib/releases/latest"
            ) as response:
                assets = json.loads(response.read().decode("utf-8"))["assets"][0]
                zip_path = self._utils.tools_dir / assets["name"]
                if not zip_path.exists():
                    old_archives = list(self._utils.tools_dir.glob("*.zip"))
                    msg_box = QMessageBox(self._utils.main_window)
                    msg_box.setWindowTitle(
                        self._utils.tr("Baldur's Gate 3 Plugin - Missing dependencies")
                    )
                    if old_archives:
                        msg_box.setText(self._utils.tr("LSLib update available."))
                    else:
                        msg_box.setText(
                            self._utils.tr(
                                "LSLib tools are missing.\nThese are necessary for the plugin to create the load order file for BG3."
                            )
                        )
                    msg_box.addButton(
                        self._utils.tr("Download"),
                        QMessageBox.ButtonRole.DestructiveRole,
                    )
                    exit_btn = msg_box.addButton(
                        self._utils.tr("Exit"), QMessageBox.ButtonRole.ActionRole
                    )
                    msg_box.setIcon(QMessageBox.Icon.Warning)
                    msg_box.exec()

                    if msg_box.clickedButton() == exit_btn:
                        if not old_archives:
                            err = QMessageBox(self._utils.main_window)
                            err.setIcon(QMessageBox.Icon.Critical)
                            err.setText(
                                "LSLib tools are required for the proper generation of the modsettings.xml file, file will not be generated"
                            )
                            err.exec()
                            return False
                    else:
                        progress = self._utils.create_progress_window(
                            "Downloading LSLib", 100, cancelable=False
                        )
                        urllib.request.urlretrieve(
                            assets["browser_download_url"], str(zip_path), reporthook
                        )
                        progress.close()
                        downloaded = True
                        for archive in old_archives:
                            archive.unlink()
                        old_archives = []
                else:
                    old_archives = []
                    new_msg = QMessageBox(self._utils.main_window)
                    new_msg.setIcon(QMessageBox.Icon.Information)
                    new_msg.setText(
                        self._utils.tr("Latest version of LSLib already downloaded!")
                    )
                    new_msg.exec()

        except Exception as e:
            qDebug(f"Download failed: {e}")
            err = QMessageBox(self._utils.main_window)
            err.setIcon(QMessageBox.Icon.Critical)
            err.setText(
                self._utils.tr(
                    f"Failed to download LSLib tools:\n{traceback.format_exc()}"
                )
            )
            err.exec()
            return False
        try:
            if old_archives:
                zip_path = sorted(old_archives)[-1]
            if old_archives or not downloaded:
                dialog_message = "Ensuring all necessary LSLib files have been extracted from archive..."
                win_title = "Verifying LSLib files"
            else:
                dialog_message = "Extracting/Updating LSLib files..."
                win_title = "Extracting LSLib"
            x_progress = self._utils.create_progress_window(
                win_title, len(self._needed_lslib_files), msg=dialog_message
            )
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                # Build a lookup from basename → zip entry so we can handle
                # any zip directory layout (Packed/Tools/, Tools/, flat, etc.)
                zip_names = zip_ref.namelist()
                entry_map: dict[str, str] = {}
                for zn in zip_names:
                    base = zn.rsplit("/", 1)[-1] if "/" in zn else zn
                    if base:
                        entry_map[base] = zn

                for file in self._needed_lslib_files:
                    if downloaded or not file.exists():
                        zip_entry = entry_map.get(file.name)
                        if zip_entry is None:
                            if file.name in self._REQUIRED_FILES:
                                raise KeyError(
                                    f"Required file '{file.name}' not found in archive"
                                )
                            qDebug(f"LSLib: optional file '{file.name}' not in archive, skipping")
                            continue
                        extracted = zip_ref.extract(zip_entry, self._utils.tools_dir)
                        # Move from nested zip path to flat tools_dir
                        if extracted != str(file):
                            shutil.move(extracted, file)
                    x_progress.setValue(x_progress.value() + 1)
                    QApplication.processEvents()
                    if x_progress.wasCanceled():
                        qWarning("processing canceled by user")
                        return False
            x_progress.close()
            # Clean up any nested directories left from extraction
            for subdir in ("Packed", "Tools"):
                nested = self._utils.tools_dir / subdir
                if nested.is_dir():
                    shutil.rmtree(nested, ignore_errors=True)
        except Exception as e:
            qDebug(f"Extraction failed: {e}")
            err = QMessageBox(self._utils.main_window)
            err.setIcon(QMessageBox.Icon.Critical)
            err.setText(
                self._utils.tr(
                    f"Failed to extract LSLib tools:\n{traceback.format_exc()}"
                )
            )
            err.exec()
            return False
        return True
