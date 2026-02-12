# -*- encoding: utf-8 -*-

"""
OMOD Installer plugin for Mod Organizer 2 (Linux port).

Handles .omod archives (Oblivion Mod Manager format) by parsing the binary
config, extracting compressed data/plugin streams, and re-packaging as a
standard zip for MO2's installation manager.
"""

import io
import lzma
import os
import shutil
import struct
import tempfile
import zipfile
import zlib
from pathlib import Path

import mobase


class OmodInstaller(mobase.IPluginInstallerCustom):
    _organizer: mobase.IOrganizer

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

    def name(self) -> str:
        return "OMOD Installer"

    def localizedName(self) -> str:
        return "OMOD Installer"

    def author(self) -> str:
        return "Fluorine Manager"

    def description(self) -> str:
        return "Installer for .omod archives (Oblivion Mod Manager format)"

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(1, 0, 0)

    def settings(self) -> list[mobase.PluginSetting]:
        return []

    def priority(self) -> int:
        return 500

    def isManualInstaller(self) -> bool:
        return False

    def isArchiveSupported(self, archive) -> bool:
        # Called with IFileTree (mod list refresh) or str (install check).
        if isinstance(archive, str):
            return archive.lower().endswith(".omod")
        # IFileTree: look for the "config" file that every OMOD contains.
        try:
            for entry in archive:
                if entry.isFile() and entry.name() == "config":
                    return True
        except TypeError:
            pass
        return False

    def supportedExtensions(self) -> set[str]:
        return {"omod"}

    def install(
        self,
        mod_name: mobase.GuessedString,
        game_name: str,
        archive_name: str,
        version: str,
        nexus_id: int,
    ) -> mobase.InstallResult:
        try:
            return self._do_install(mod_name, archive_name)
        except Exception as e:
            mobase.log(mobase.LogLevel.ERROR, f"OMOD install failed: {e}")
            return mobase.InstallResult.FAILED

    def _do_install(
        self,
        mod_name: mobase.GuessedString,
        archive_name: str,
    ) -> mobase.InstallResult:
        with zipfile.ZipFile(archive_name, "r") as zf:
            names = zf.namelist()

            if "config" not in names:
                mobase.log(mobase.LogLevel.WARNING, "OMOD: no config entry found")
                return mobase.InstallResult.NOT_ATTEMPTED

            config = self._parse_config(zf.read("config"))
            if config.get("mod_name"):
                mod_name.update(config["mod_name"])

            compression = config.get("compression_type", 0)

            tmpdir = tempfile.mkdtemp(prefix="omod_")
            try:
                self._extract_stream(
                    zf, names, "data", "data.crc", compression, tmpdir
                )
                self._extract_stream(
                    zf, names, "plugins", "plugins.crc", compression, tmpdir
                )

                # If the OMOD contains a readme, extract it too.
                for entry in names:
                    lower = entry.lower()
                    if lower == "readme" or lower.startswith("readme."):
                        data = zf.read(entry)
                        dest = Path(tmpdir) / entry
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)

                # Collect extracted files.
                file_list = []
                for root, _dirs, files in os.walk(tmpdir):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, tmpdir)
                        file_list.append(rel)

                if not file_list:
                    mobase.log(
                        mobase.LogLevel.WARNING, "OMOD: no files extracted"
                    )
                    return mobase.InstallResult.FAILED

                # Repackage as a standard zip for MO2's installer.
                repack_path = os.path.join(tmpdir, "_repack.zip")
                with zipfile.ZipFile(repack_path, "w", zipfile.ZIP_DEFLATED) as out_zip:
                    for rel in file_list:
                        out_zip.write(os.path.join(tmpdir, rel), rel)

                result, _, _ = self.manager().installArchive(mod_name, repack_path)
                return result
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _extract_stream(
        self,
        zf: zipfile.ZipFile,
        names: list[str],
        stream_name: str,
        crc_name: str,
        compression: int,
        out_dir: str,
    ) -> None:
        if stream_name not in names:
            return
        if crc_name not in names:
            mobase.log(
                mobase.LogLevel.WARNING,
                f"OMOD: {stream_name} present but {crc_name} missing",
            )
            return

        file_list = self._parse_crc_file(zf.read(crc_name))
        if not file_list:
            return

        raw = zf.read(stream_name)
        decompressed = self._decompress_stream(raw, compression)

        offset = 0
        for path, size in file_list:
            if offset + size > len(decompressed):
                mobase.log(
                    mobase.LogLevel.WARNING,
                    f"OMOD: truncated stream for {path} "
                    f"(need {size} bytes at offset {offset}, "
                    f"have {len(decompressed)})",
                )
                break

            file_data = decompressed[offset : offset + size]
            offset += size

            # Normalise path separators from Windows.
            path = path.replace("\\", "/")
            dest = Path(out_dir) / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(file_data)

    def _parse_config(self, data: bytes) -> dict:
        """Parse the OMOD binary config entry."""
        reader = io.BytesIO(data)
        config = {}

        config["file_version"] = struct.unpack("<B", reader.read(1))[0]
        config["mod_name"] = self._read_net_string(reader)

        # Major and minor version.
        config["major"] = struct.unpack("<i", reader.read(4))[0]
        config["minor"] = struct.unpack("<i", reader.read(4))[0]

        config["author"] = self._read_net_string(reader)
        config["email"] = self._read_net_string(reader)
        config["website"] = self._read_net_string(reader)
        config["description"] = self._read_net_string(reader)

        # Creation time (Windows FILETIME, 8 bytes) - skip.
        reader.read(8)

        # Compression type: 0 = deflate, 1 = lzma.
        config["compression_type"] = struct.unpack("<B", reader.read(1))[0]

        return config

    def _parse_crc_file(self, data: bytes) -> list[tuple[str, int]]:
        """Parse data.crc or plugins.crc.

        Returns [(relative_path, uncompressed_size), ...].
        """
        reader = io.BytesIO(data)
        count = self._read_7bit_encoded_int(reader)
        files = []
        for _ in range(count):
            path = self._read_net_string(reader)
            _crc = struct.unpack("<I", reader.read(4))[0]
            size = struct.unpack("<q", reader.read(8))[0]
            files.append((path, size))
        return files

    def _decompress_stream(self, data: bytes, compression_type: int) -> bytes:
        """Decompress an OMOD data or plugins stream."""
        if compression_type == 0:
            # Raw deflate (no zlib/gzip header).
            return zlib.decompress(data, -15)
        elif compression_type == 1:
            return lzma.decompress(data)
        else:
            raise ValueError(f"Unknown OMOD compression type: {compression_type}")

    @staticmethod
    def _read_net_string(reader: io.BytesIO) -> str:
        """Read a .NET BinaryWriter-style length-prefixed string.

        The length is encoded as a 7-bit encoded int, followed by that many
        bytes of UTF-8.
        """
        length = OmodInstaller._read_7bit_encoded_int(reader)
        if length == 0:
            return ""
        raw = reader.read(length)
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _read_7bit_encoded_int(reader: io.BytesIO) -> int:
        """Read a .NET 7-bit encoded integer."""
        result = 0
        shift = 0
        while True:
            byte_data = reader.read(1)
            if not byte_data:
                break
            b = byte_data[0]
            result |= (b & 0x7F) << shift
            shift += 7
            if (b & 0x80) == 0:
                break
        return result


def createPlugin() -> mobase.IPlugin:
    return OmodInstaller()
