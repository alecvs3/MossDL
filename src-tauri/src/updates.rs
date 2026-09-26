//! In-app updates, when a release was built for them.
//!
//! The update signing key's public half and the feed URL are baked in at
//! release time (MOSSDL_UPDATER_PUBKEY, MOSSDL_UPDATE_URL; see
//! scripts/release.ps1). A build without them reports "not configured"
//! rather than checking an unsigned or missing feed.

use serde_json::{json, Value};
use tauri_plugin_updater::{Updater, UpdaterExt};

const PUBKEY: Option<&str> = option_env!("MOSSDL_UPDATER_PUBKEY");
const FEED: Option<&str> = option_env!("MOSSDL_UPDATE_URL");

fn updater(app: &tauri::AppHandle) -> Result<Option<Updater>, String> {
    let (Some(pubkey), Some(feed)) = (PUBKEY, FEED) else {
        return Ok(None);
    };
    let feed = feed.parse().map_err(|e| format!("update feed URL: {e}"))?;
    let updater = app
        .updater_builder()
        .pubkey(pubkey)
        .endpoints(vec![feed])
        .and_then(|builder| builder.build())
        .map_err(|e| e.to_string())?;
    Ok(Some(updater))
}

#[tauri::command]
pub async fn update_check(app: tauri::AppHandle) -> Result<Value, String> {
    let Some(updater) = updater(&app)? else {
        return Ok(json!({ "configured": false }));
    };
    Ok(match updater.check().await.map_err(|e| e.to_string())? {
        Some(update) => json!({
            "configured": true, "available": true, "version": update.version,
            "current": update.current_version, "notes": update.body,
        }),
        None => json!({ "configured": true, "available": false }),
    })
}

/// Downloads, verifies against the baked-in key, and installs. On Windows the
/// installer takes over and the app exits.
#[tauri::command]
pub async fn update_install(app: tauri::AppHandle) -> Result<(), String> {
    let updater = updater(&app)?.ok_or("updates are not configured in this build")?;
    let update = updater.check().await.map_err(|e| e.to_string())?.ok_or("already up to date")?;
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|e| e.to_string())
}
