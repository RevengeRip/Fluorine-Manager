use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

fn get_home() -> String {
    std::env::var("HOME").unwrap_or_default()
}

/// Normalize a path for compatibility with pressure-vessel/Steam container.
///
/// On Fedora Atomic/Bazzite/Silverblue, $HOME is `/var/home/user` but `/home`
/// is a symlink to `/var/home`. Pressure-vessel exposes `/home` but may not
/// properly handle paths that explicitly use `/var/home/`. This function
/// converts such paths to use `/home/` instead for maximum compatibility.
pub fn normalize_path_for_steam(path: &str) -> String {
    // Convert /var/home/user/... to /home/user/...
    if let Some(stripped) = path.strip_prefix("/var/home/") {
        format!("/home/{}", stripped)
    } else {
        path.to_string()
    }
}

fn default_data_path() -> String {
    format!("{}/NaK", get_home())
}

// ============================================================================
// Main App Config - stored in ~/.config/nak/config.json
// ============================================================================

#[derive(Serialize, Deserialize, Clone)]
pub struct AppConfig {
    pub selected_proton: Option<String>,
    /// Whether the first-run setup has been completed
    #[serde(default)]
    pub first_run_completed: bool,
    /// Path to NaK data folder (legacy ~/NaK - used for migration detection)
    #[serde(default = "default_data_path")]
    pub data_path: String,
    /// Whether the Steam-native migration popup has been shown
    #[serde(default)]
    pub steam_migration_shown: bool,
    /// Custom cache location (for downloads, tmp files during install)
    /// If empty/not set, uses ~/.cache/nak/
    #[serde(default)]
    pub cache_location: String,
    /// Selected Steam account ID (Steam3 format, e.g., "910757758")
    /// If empty/not set, uses the most recently active account
    #[serde(default)]
    pub selected_steam_account: String,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            selected_proton: None,
            first_run_completed: false,
            data_path: default_data_path(),
            steam_migration_shown: false,
            cache_location: String::new(),
            selected_steam_account: String::new(),
        }
    }
}

impl AppConfig {
    /// Config file path: ~/.config/nak/config.json
    fn get_config_path() -> PathBuf {
        PathBuf::from(format!("{}/.config/nak/config.json", get_home()))
    }

    /// Legacy config path for migration: ~/NaK/config.json
    fn get_legacy_path() -> PathBuf {
        PathBuf::from(format!("{}/NaK/config.json", get_home()))
    }

    pub fn load() -> Self {
        let config_path = Self::get_config_path();
        let legacy_path = Self::get_legacy_path();

        // Try new location first
        if config_path.exists() {
            if let Ok(content) = fs::read_to_string(&config_path) {
                if let Ok(config) = serde_json::from_str(&content) {
                    return config;
                }
            }
        }

        // Try legacy location and migrate if found
        if legacy_path.exists() {
            if let Ok(content) = fs::read_to_string(&legacy_path) {
                if let Ok(mut config) = serde_json::from_str::<AppConfig>(&content) {
                    // Ensure data_path is set (old configs won't have it)
                    if config.data_path.is_empty() {
                        config.data_path = default_data_path();
                    }
                    // Save to new location
                    config.save();
                    // Remove old config
                    let _ = fs::remove_file(&legacy_path);
                    return config;
                }
            }
        }

        Self::default()
    }

    pub fn save(&self) {
        let path = Self::get_config_path();
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(json) = serde_json::to_string_pretty(self) {
            let _ = fs::write(path, json);
        }
    }

    /// Get the NaK data directory path (legacy ~/NaK - used for migration detection)
    pub fn get_data_path(&self) -> PathBuf {
        PathBuf::from(&self.data_path)
    }

    /// Get the NaK config directory (~/.config/nak/)
    pub fn get_config_dir() -> PathBuf {
        PathBuf::from(format!("{}/.config/nak", get_home()))
    }

    /// Get the default cache directory (~/.cache/nak/)
    pub fn get_default_cache_dir() -> PathBuf {
        PathBuf::from(format!("{}/.cache/nak", get_home()))
    }

    /// Get the cache directory (custom location or default ~/.cache/nak/)
    pub fn get_cache_dir(&self) -> PathBuf {
        if self.cache_location.is_empty() {
            Self::get_default_cache_dir()
        } else {
            PathBuf::from(&self.cache_location)
        }
    }

    /// Get path to tmp directory (~/.cache/nak/tmp/)
    pub fn get_tmp_path() -> PathBuf {
        Self::get_default_cache_dir().join("tmp")
    }

    /// Get path to Prefixes directory (legacy - for migration detection only)
    pub fn get_prefixes_path(&self) -> PathBuf {
        self.get_data_path().join("Prefixes")
    }
}
