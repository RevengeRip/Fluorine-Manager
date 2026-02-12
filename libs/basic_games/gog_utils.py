# Code adapted from EzioTheDeadPoet / erri120:
#     https://github.com/ModOrganizer2/modorganizer-basic_games/pull/5

from __future__ import annotations

import json
import sys
import winreg
from pathlib import Path


def _find_heroic_gog_games() -> dict[str, Path]:
    """Detect GOG games installed via Heroic Launcher on Linux."""
    games: dict[str, Path] = {}

    for candidate in (
        Path.home() / ".config" / "heroic" / "gog_store" / "installed.json",
        Path.home()
        / ".var"
        / "app"
        / "com.heroicgameslauncher.hgl"
        / "config"
        / "heroic"
        / "gog_store"
        / "installed.json",
    ):
        if not candidate.is_file():
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            for game in data.get("installed", []):
                app_name = game.get("appName", "")
                install_path = game.get("install_path") or game.get("installPath", "")
                if app_name and install_path:
                    games[str(app_name)] = Path(install_path)
        except (json.JSONDecodeError, OSError) as e:
            print(
                f'Unable to parse Heroic GOG installed games from "{candidate}": {e}',
                file=sys.stderr,
            )

    return games


def find_games() -> dict[str, Path]:
    # List the game IDs from the registry (Windows):
    game_ids: list[str] = []
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"Software\Wow6432Node\GOG.com\Games"
        ) as key:
            nkeys = winreg.QueryInfoKey(key)[0]
            for ik in range(nkeys):
                game_key = winreg.EnumKey(key, ik)
                if game_key.isdigit():
                    game_ids.append(game_key)
    except FileNotFoundError:
        # Windows registry not available; try Heroic GOG on Linux.
        return _find_heroic_gog_games()

    # For each game, query the path:
    games: dict[str, Path] = {}
    for game_id in game_ids:
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                f"Software\\Wow6432Node\\GOG.com\\Games\\{game_id}",
            ) as key:
                games[game_id] = Path(winreg.QueryValueEx(key, "path")[0])
        except FileNotFoundError:
            pass

    return games
