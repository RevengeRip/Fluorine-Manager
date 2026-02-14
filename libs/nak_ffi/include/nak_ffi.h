#ifndef NAK_FFI_H
#define NAK_FFI_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ========================================================================
 * Tier 1: Game Detection
 * ======================================================================== */

/** A detected game installation */
typedef struct {
    char *name;
    char *app_id;
    char *install_path;
    char *prefix_path;             /* NULL if no prefix */
    char *launcher;                /* display name string */
    char *my_games_folder;         /* NULL if not applicable */
    char *appdata_local_folder;    /* NULL if not applicable */
    char *appdata_roaming_folder;  /* NULL if not applicable */
    char *registry_path;           /* NULL if not applicable */
    char *registry_value;          /* NULL if not applicable */
} NakGame;

/** List of detected games */
typedef struct {
    NakGame *games;
    size_t count;
    size_t steam_count;
    size_t heroic_count;
    size_t bottles_count;
} NakGameList;

/** Detect all installed games across all launchers */
NakGameList nak_detect_all_games(void);

/** Free a NakGameList returned by nak_detect_all_games */
void nak_game_list_free(NakGameList list);

/** A known game definition (static data, do NOT free) */
typedef struct {
    const char *name;
    const char *steam_app_id;
    const char *gog_app_id;              /* NULL if none */
    const char *my_games_folder;         /* NULL if not applicable */
    const char *appdata_local_folder;    /* NULL if not applicable */
    const char *appdata_roaming_folder;  /* NULL if not applicable */
    const char *registry_path;
    const char *registry_value;
    const char *steam_folder;
} NakKnownGame;

/** Get the list of all known games (static data, do NOT free).
 *  Returns pointer to array; writes count to *out_count. */
const NakKnownGame *nak_get_known_games(size_t *out_count);

/* ========================================================================
 * Tier 2: Proton Detection
 * ======================================================================== */

/** An installed Proton version */
typedef struct {
    char *name;
    char *config_name;
    char *path;
    int is_steam_proton;
    int is_experimental;
} NakSteamProton;

/** List of detected Proton installations */
typedef struct {
    NakSteamProton *protons;
    size_t count;
} NakProtonList;

/** Find all installed Proton versions */
NakProtonList nak_find_steam_protons(void);

/** Free a NakProtonList */
void nak_proton_list_free(NakProtonList list);

/* ========================================================================
 * Tier 3: Steam Paths
 * ======================================================================== */

/** Find the Steam installation path.
 *  Returns newly allocated string (free with nak_string_free), or NULL. */
char *nak_find_steam_path(void);

/* ========================================================================
 * Tier 4: Dependency Installation (callback-based)
 * ======================================================================== */

/** Callback for status/log messages */
typedef void (*NakStatusCallback)(const char *message);
typedef void (*NakLogCallback)(const char *message);

/** Callback for progress updates (0.0 to 1.0) */
typedef void (*NakProgressCallback)(float progress);

/** Install all Wine prefix dependencies (blocking call).
 *  cancel_flag: pointer to int, set non-zero to cancel.
 *  Returns NULL on success, or error message (free with nak_string_free). */
char *nak_install_all_dependencies(
    const char *prefix_path,
    const char *proton_name,
    const char *proton_path,
    NakStatusCallback status_cb,
    NakLogCallback log_cb,
    NakProgressCallback progress_cb,
    const int *cancel_flag,
    uint32_t app_id
);

/** Apply Wine registry settings to a prefix.
 *  Returns NULL on success, or error message (free with nak_string_free). */
char *nak_apply_wine_registry_settings(
    const char *prefix_path,
    const char *proton_name,
    const char *proton_path,
    NakLogCallback log_cb,
    uint32_t app_id
);

/** Apply a game's registry entry with a custom install path.
 *  Looks up game_name in KNOWN_GAMES and writes registry pointing to install_path.
 *  Returns NULL on success, or an error message (free with nak_string_free). */
char *nak_apply_registry_for_game_path(
    const char *prefix_path,
    const char *proton_name,
    const char *proton_path,
    const char *game_name,
    const char *install_path,
    NakLogCallback log_cb
);

/* ========================================================================
 * Tier 5: Prefix Symlinks
 * ======================================================================== */

/** Ensure AppData/Local/Temp exists in the Wine prefix.
 *  Call during prefix creation. */
void nak_ensure_temp_directory(const char *prefix_path);

/** Detect games and create symlinks from the prefix to game prefixes.
 *  Call during prefix creation. */
void nak_create_game_symlinks_auto(const char *prefix_path);

/* ========================================================================
 * Tier 6: Logging
 * ======================================================================== */

/** Callback for NaK log messages: (level, message).
 *  Levels: "info", "warning", "error", "install", "action", "download" */
typedef void (*NakLogLevelCallback)(const char *level, const char *message);

/** Initialize NaK logging with a callback.
 *  Call once at startup before any other nak_* functions. */
void nak_init_logging(NakLogLevelCallback cb);

/* ========================================================================
 * Tier 7: DXVK Configuration
 * ======================================================================== */

/** Ensure the DXVK config file exists, downloading if necessary.
 *  Returns NULL on success, or error message (free with nak_string_free). */
char *nak_ensure_dxvk_conf(void);

/** Get the path to the DXVK config file.
 *  Returns newly allocated string (free with nak_string_free). */
char *nak_get_dxvk_conf_path(void);

/* ========================================================================
 * General
 * ======================================================================== */

/** Free a string returned by any nak_* function */
void nak_string_free(char *s);

#ifdef __cplusplus
}
#endif

#endif /* NAK_FFI_H */
