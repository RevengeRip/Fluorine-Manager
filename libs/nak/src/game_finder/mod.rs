//! Game detection module
//!
//! Provides unified game detection across multiple launchers:
//! - Steam (native, Flatpak, Snap)
//! - Heroic (GOG, Epic)
//! - Bottles

// Allow unused items - this is a public API module
#![allow(dead_code)]
#![allow(unused_imports)]

mod bottles;
mod heroic;
pub mod known_games;
mod registry;
mod steam;
mod vdf;

use std::path::PathBuf;

pub use bottles::detect_bottles_games;
pub use heroic::detect_heroic_games;
pub use known_games::{find_by_gog_id, find_by_name, find_by_steam_id, KnownGame, KNOWN_GAMES};
pub use registry::{read_registry_value, wine_path_to_linux};
pub use steam::{detect_steam_games, find_game_install_path, find_game_prefix_path, get_known_game};

// ============================================================================
// Core Types
// ============================================================================

/// The launcher/store a game was installed from
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Launcher {
    Steam { is_flatpak: bool, is_snap: bool },
    Heroic { store: HeroicStore },
    Bottles,
}

impl Launcher {
    pub fn display_name(&self) -> &'static str {
        match self {
            Launcher::Steam { is_flatpak: true, .. } => "Steam (Flatpak)",
            Launcher::Steam { is_snap: true, .. } => "Steam (Snap)",
            Launcher::Steam { .. } => "Steam",
            Launcher::Heroic { store: HeroicStore::GOG } => "Heroic (GOG)",
            Launcher::Heroic { store: HeroicStore::Epic } => "Heroic (Epic)",
            Launcher::Bottles => "Bottles",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HeroicStore {
    GOG,
    Epic,
}

/// A detected game installation
#[derive(Debug, Clone)]
pub struct Game {
    pub name: String,
    pub app_id: String,
    pub install_path: PathBuf,
    pub prefix_path: Option<PathBuf>,
    pub launcher: Launcher,
    pub my_games_folder: Option<String>,
    pub appdata_local_folder: Option<String>,
    pub appdata_roaming_folder: Option<String>,
    pub registry_path: Option<String>,
    pub registry_value: Option<String>,
}

impl Game {
    pub fn has_prefix(&self) -> bool {
        self.prefix_path.is_some()
    }

    pub fn get_prefix_user_path(&self) -> Option<PathBuf> {
        let prefix = self.prefix_path.as_ref()?;
        let users_dir = prefix.join("drive_c/users");

        if let Ok(entries) = std::fs::read_dir(&users_dir) {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                if name != "Public" && name != "root" {
                    return Some(users_dir.join(name));
                }
            }
        }

        Some(users_dir.join("steamuser"))
    }

    pub fn get_prefix_documents_path(&self) -> Option<PathBuf> {
        self.get_prefix_user_path().map(|p| p.join("Documents"))
    }

    pub fn get_prefix_my_games_path(&self) -> Option<PathBuf> {
        let docs = self.get_prefix_documents_path()?;
        let folder = self.my_games_folder.as_ref()?;
        Some(docs.join("My Games").join(folder))
    }

    pub fn get_prefix_appdata_local_path(&self) -> Option<PathBuf> {
        let user = self.get_prefix_user_path()?;
        let folder = self.appdata_local_folder.as_ref()?;
        Some(user.join("AppData/Local").join(folder))
    }

    pub fn get_prefix_appdata_roaming_path(&self) -> Option<PathBuf> {
        let user = self.get_prefix_user_path()?;
        let folder = self.appdata_roaming_folder.as_ref()?;
        Some(user.join("AppData/Roaming").join(folder))
    }
}

// ============================================================================
// Scan Results
// ============================================================================

#[derive(Debug, Default)]
pub struct GameScanResult {
    pub games: Vec<Game>,
    pub steam_count: usize,
    pub heroic_count: usize,
    pub bottles_count: usize,
}

impl GameScanResult {
    pub fn games_with_prefixes(&self) -> impl Iterator<Item = &Game> {
        self.games.iter().filter(|g| g.has_prefix())
    }

    pub fn games_by_launcher(&self, launcher_type: &str) -> Vec<&Game> {
        self.games
            .iter()
            .filter(|g| {
                matches!(
                    (&g.launcher, launcher_type),
                    (Launcher::Steam { .. }, "steam")
                        | (Launcher::Heroic { .. }, "heroic")
                        | (Launcher::Bottles, "bottles")
                )
            })
            .collect()
    }

    pub fn find_by_name(&self, name: &str) -> Option<&Game> {
        let name_lower = name.to_lowercase();
        self.games
            .iter()
            .find(|g| g.name.to_lowercase() == name_lower)
    }

    pub fn find_by_app_id(&self, app_id: &str) -> Option<&Game> {
        self.games.iter().find(|g| g.app_id == app_id)
    }
}

// ============================================================================
// Public API
// ============================================================================

/// Detect all installed games from all supported launchers
pub fn detect_all_games() -> GameScanResult {
    let mut result = GameScanResult::default();

    let steam_games = detect_steam_games();
    result.steam_count = steam_games.len();
    result.games.extend(steam_games);

    let heroic_games = detect_heroic_games();
    result.heroic_count = heroic_games.len();
    result.games.extend(heroic_games);

    let bottles_games = detect_bottles_games();
    result.bottles_count = bottles_games.len();
    result.games.extend(bottles_games);

    result
}

/// Detect only Steam games
pub fn detect_steam_only() -> GameScanResult {
    let steam_games = detect_steam_games();
    GameScanResult {
        steam_count: steam_games.len(),
        games: steam_games,
        ..Default::default()
    }
}
