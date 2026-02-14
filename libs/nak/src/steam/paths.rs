//! Steam path detection utilities

use std::fs;
use std::path::PathBuf;

use crate::logging::{log_info, log_warning};

// ============================================================================
// Core Path Detection
// ============================================================================

/// Find the Steam installation path.
#[must_use]
pub fn find_steam_path() -> Option<PathBuf> {
    let home = std::env::var("HOME").ok()?;

    let steam_paths = [
        format!("{}/.steam/steam", home),
        format!("{}/.local/share/Steam", home),
        format!("{}/.var/app/com.valvesoftware.Steam/.steam/steam", home),
        format!("{}/snap/steam/common/.steam/steam", home),
    ];

    steam_paths
        .iter()
        .map(PathBuf::from)
        .find(|p| p.exists())
}

/// Find the Steam userdata directory.
#[must_use]
pub fn find_userdata_path() -> Option<PathBuf> {
    let config = crate::config::AppConfig::load();
    if !config.selected_steam_account.is_empty() {
        if let Some(path) = find_userdata_path_for_account(&config.selected_steam_account) {
            return Some(path);
        }
    }

    let steam_path = find_steam_path()?;
    let userdata = steam_path.join("userdata");

    if !userdata.exists() {
        return None;
    }

    let accounts = get_steam_accounts();

    if let Some(most_recent) = accounts.iter().find(|a| a.most_recent) {
        let path = userdata.join(&most_recent.account_id);
        if path.exists() {
            log_info(&format!(
                "Using Steam account from loginusers.vdf (MostRecent): {} ({})",
                most_recent.persona_name, most_recent.account_id
            ));
            return Some(path);
        }
    }

    if let Some(first_account) = accounts.first() {
        let path = userdata.join(&first_account.account_id);
        if path.exists() {
            log_info(&format!(
                "Using Steam account from loginusers.vdf (most recent timestamp): {} ({})",
                first_account.persona_name, first_account.account_id
            ));
            return Some(path);
        }
    }

    log_warning("Could not determine active Steam account from loginusers.vdf, falling back to directory modification time");

    let mut user_dirs: Vec<PathBuf> = Vec::new();

    if let Ok(entries) = fs::read_dir(&userdata) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                if let Some(name) = path.file_name() {
                    let name_str = name.to_string_lossy();
                    if name_str != "0" && name_str.chars().all(|c| c.is_ascii_digit()) {
                        user_dirs.push(path);
                    }
                }
            }
        }
    }

    user_dirs.sort_by(|a, b| {
        let a_time = fs::metadata(a).and_then(|m| m.modified()).ok();
        let b_time = fs::metadata(b).and_then(|m| m.modified()).ok();
        b_time.cmp(&a_time)
    });

    user_dirs.into_iter().next()
}

// ============================================================================
// Steam Account Detection (loginusers.vdf parsing)
// ============================================================================

/// Information about a Steam user account
#[derive(Debug, Clone)]
pub struct SteamAccount {
    pub account_id: String,
    pub persona_name: String,
    pub most_recent: bool,
    pub timestamp: u64,
}

/// Get all Steam accounts from loginusers.vdf with their display names
#[must_use]
pub fn get_steam_accounts() -> Vec<SteamAccount> {
    let Some(steam_path) = find_steam_path() else {
        return Vec::new();
    };

    let loginusers_path = steam_path.join("config/loginusers.vdf");
    let userdata_path = steam_path.join("userdata");

    let Ok(content) = fs::read_to_string(&loginusers_path) else {
        return Vec::new();
    };

    let mut accounts = Vec::new();

    let mut current_steam_id: Option<String> = None;
    let mut current_account: Option<SteamAccountBuilder> = None;

    for line in content.lines() {
        let trimmed = line.trim();

        if trimmed.starts_with('"') && trimmed.ends_with('"') {
            let id = trimmed.trim_matches('"');
            if id.len() == 17 && id.starts_with("7656") && id.chars().all(|c| c.is_ascii_digit()) {
                if let (Some(steam_id), Some(builder)) = (current_steam_id.take(), current_account.take()) {
                    if let Some(account) = builder.build(&steam_id, &userdata_path) {
                        accounts.push(account);
                    }
                }
                current_steam_id = Some(id.to_string());
                current_account = Some(SteamAccountBuilder::default());
            }
        }

        if let Some(ref mut builder) = current_account {
            if let Some((key, value)) = parse_vdf_kv(trimmed) {
                match key.to_lowercase().as_str() {
                    "accountname" => builder.account_name = Some(value),
                    "personaname" => builder.persona_name = Some(value),
                    "mostrecent" => builder.most_recent = value == "1",
                    "timestamp" => builder.timestamp = value.parse().unwrap_or(0),
                    _ => {}
                }
            }
        }
    }

    if let (Some(steam_id), Some(builder)) = (current_steam_id, current_account) {
        if let Some(account) = builder.build(&steam_id, &userdata_path) {
            accounts.push(account);
        }
    }

    accounts.sort_by(|a, b| b.timestamp.cmp(&a.timestamp));

    accounts
}

#[derive(Default)]
struct SteamAccountBuilder {
    account_name: Option<String>,
    persona_name: Option<String>,
    most_recent: bool,
    timestamp: u64,
}

impl SteamAccountBuilder {
    fn build(self, steam_id: &str, userdata_base: &std::path::Path) -> Option<SteamAccount> {
        let account_name = self.account_name?;
        let persona_name = self.persona_name.unwrap_or_else(|| account_name.clone());

        let steam64: u64 = steam_id.parse().ok()?;
        let account_id = (steam64 - 76561197960265728).to_string();

        let userdata_path = userdata_base.join(&account_id);

        if !userdata_path.exists() {
            return None;
        }

        Some(SteamAccount {
            account_id,
            persona_name,
            most_recent: self.most_recent,
            timestamp: self.timestamp,
        })
    }
}

/// Parse a VDF key-value pair like: "Key"    "Value"
fn parse_vdf_kv(line: &str) -> Option<(String, String)> {
    let mut parts = Vec::new();
    let mut current = String::new();
    let mut in_quotes = false;

    for c in line.chars() {
        match c {
            '"' => {
                if in_quotes {
                    parts.push(current.clone());
                    current.clear();
                }
                in_quotes = !in_quotes;
            }
            _ if in_quotes => current.push(c),
            _ => {}
        }
    }

    if parts.len() >= 2 {
        Some((parts[0].clone(), parts[1].clone()))
    } else {
        None
    }
}

/// Find the userdata path for a specific Steam account
#[must_use]
pub fn find_userdata_path_for_account(account_id: &str) -> Option<PathBuf> {
    let steam_path = find_steam_path()?;
    let userdata = steam_path.join("userdata").join(account_id);

    if userdata.exists() {
        Some(userdata)
    } else {
        None
    }
}

// ============================================================================
// Convenience Wrappers
// ============================================================================

/// Detect the Steam installation path with logging.
#[must_use]
pub fn detect_steam_path_checked() -> Option<String> {
    match find_steam_path() {
        Some(path) => {
            let path_str = path.to_string_lossy().to_string();
            log_info(&format!("Steam detected at: {}", path_str));
            Some(path_str)
        }
        None => {
            log_warning("Steam installation not detected! NaK requires Steam to be installed.");
            None
        }
    }
}
