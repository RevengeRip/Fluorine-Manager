//! Steam integration module
//!
//! Handles Proton detection, Steam path detection, and mount point discovery.
//! Shortcuts and config.vdf manipulation removed (handled by C++ side).

mod paths;
mod proton;

// Re-export path detection utilities
pub use paths::{
    detect_steam_path_checked, find_steam_path, find_userdata_path,
    get_steam_accounts,
};

// Re-export Proton detection
pub use proton::{find_steam_protons, SteamProton};

use std::fs;

/// Kill Steam process gracefully, then force if needed
pub fn kill_steam() -> Result<(), Box<dyn std::error::Error>> {
    use std::process::Command;

    // Try steam -shutdown first (graceful)
    let _ = Command::new("steam")
        .arg("-shutdown")
        .status();

    std::thread::sleep(std::time::Duration::from_secs(2));

    // Then force kill if still running
    let _ = Command::new("pkill")
        .arg("-9")
        .arg("steam")
        .status();

    // Brief wait for Steam to fully exit
    std::thread::sleep(std::time::Duration::from_secs(2));

    Ok(())
}

/// Start Steam in background
pub fn start_steam() -> Result<(), Box<dyn std::error::Error>> {
    use std::process::{Command, Stdio};

    Command::new("setsid")
        .arg("steam")
        .arg("-silent")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;

    Ok(())
}

/// Restart Steam (kill then start)
pub fn restart_steam() -> Result<(), Box<dyn std::error::Error>> {
    kill_steam()?;
    start_steam()?;
    Ok(())
}

// ============================================================================
// STEAM_COMPAT_MOUNTS Detection
// ============================================================================

/// Directories that pressure-vessel already exposes by default
const ALREADY_EXPOSED: &[&str] = &[
    "bin", "etc", "home", "lib", "lib32", "lib64",
    "overrides", "run", "sbin", "tmp", "usr", "var",
];

/// System directories that shouldn't be mounted
const SYSTEM_DIRS: &[&str] = &[
    "proc", "sys", "dev", "boot", "root", "lost+found", "snap",
];

/// Detect directories at root that need to be added to STEAM_COMPAT_MOUNTS
pub fn detect_extra_mounts() -> Vec<String> {
    let mut mounts = Vec::new();

    let Ok(entries) = fs::read_dir("/") else {
        return mounts;
    };

    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();

        if ALREADY_EXPOSED.contains(&name.as_str()) {
            continue;
        }

        if SYSTEM_DIRS.contains(&name.as_str()) {
            continue;
        }

        if name.starts_with('.') {
            continue;
        }

        if entry.path().is_dir() {
            mounts.push(format!("/{}", name));
        }
    }

    mounts.sort();
    mounts
}

/// Generate launch options string with DXVK config file and STEAM_COMPAT_MOUNTS
pub fn generate_launch_options(dxvk_conf_path: Option<&std::path::Path>, is_electron_app: bool) -> String {
    let mounts = detect_extra_mounts();

    let dxvk_part = match dxvk_conf_path {
        Some(path) => format!(
            "DXVK_CONFIG_FILE=\"{}\"",
            crate::config::normalize_path_for_steam(&path.to_string_lossy())
        ),
        None => String::new(),
    };

    let electron_flags = if is_electron_app {
        " --disable-gpu --no-sandbox"
    } else {
        ""
    };

    match (dxvk_part.is_empty(), mounts.is_empty()) {
        (true, true) => format!("%command%{}", electron_flags),
        (true, false) => format!("STEAM_COMPAT_MOUNTS={} %command%{}", mounts.join(":"), electron_flags),
        (false, true) => format!("{} %command%{}", dxvk_part, electron_flags),
        (false, false) => format!("{} STEAM_COMPAT_MOUNTS={} %command%{}", dxvk_part, mounts.join(":"), electron_flags),
    }
}
