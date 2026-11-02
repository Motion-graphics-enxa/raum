//! The one setting the shell keeps: the last box address, as `box.json` in the app config dir
//! (macOS `~/Library/Application Support/de.raum.desktop/`, Windows `%APPDATA%\de.raum.desktop\`,
//! Linux `~/.config/de.raum.desktop/`). Delete the file to forget the box.

use std::{fs, path::PathBuf};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, Runtime};
use url::Url;

#[derive(Debug, Default, Serialize, Deserialize)]
struct Saved {
    #[serde(rename = "box")]
    box_origin: Option<String>,
}

fn path<R: Runtime>(app: &AppHandle<R>) -> Option<PathBuf> {
    app.path().app_config_dir().ok().map(|d| d.join("box.json"))
}

pub fn load<R: Runtime>(app: &AppHandle<R>) -> Option<Url> {
    let raw = fs::read_to_string(path(app)?).ok()?;
    let saved: Saved = serde_json::from_str(&raw).ok()?;
    crate::target::normalize(&saved.box_origin?).ok()
}

pub fn save<R: Runtime>(app: &AppHandle<R>, origin: &Url) -> std::io::Result<()> {
    let file = path(app).ok_or_else(|| std::io::Error::other("no app config dir"))?;
    if let Some(dir) = file.parent() {
        fs::create_dir_all(dir)?;
    }
    let body = serde_json::to_string_pretty(&Saved { box_origin: Some(origin.origin().ascii_serialization()) })?;
    let tmp = file.with_extension("json.tmp");
    fs::write(&tmp, body)?;
    fs::rename(tmp, file)
}
