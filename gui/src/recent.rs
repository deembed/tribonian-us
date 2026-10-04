//! "Recent searches", kept between sessions in a small JSON file:
//! `$XDG_DATA_HOME/lawgui/recent-searches.json` (default `~/.local/share/lawgui/`).
//! A plain file rather than eframe's persistence, so nothing else about the app's state is saved.
//! Best effort: a missing or unreadable file is an empty list, a failed write is ignored.

use crate::search::Mode;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// Most searches kept.
pub const CAP: usize = 30;

#[derive(Serialize, Deserialize)]
struct Entry {
    query: String,
    /// "name" or "parenthetical"
    mode: String,
}

fn path() -> Option<PathBuf> {
    let base = match std::env::var_os("XDG_DATA_HOME").filter(|v| !v.is_empty()) {
        Some(d) => PathBuf::from(d),
        None => PathBuf::from(std::env::var_os("HOME")?).join(".local").join("share"),
    };
    Some(base.join("lawgui").join("recent-searches.json"))
}

pub fn load() -> Vec<(String, Mode)> {
    let Some(text) = path().and_then(|p| std::fs::read_to_string(p).ok()) else { return Vec::new() };
    let entries: Vec<Entry> = serde_json::from_str(&text).unwrap_or_default();
    entries
        .into_iter()
        .filter_map(|e| {
            let mode = match e.mode.as_str() {
                "name" => Mode::Name,
                "parenthetical" => Mode::Parenthetical,
                _ => return None,
            };
            Some((e.query, mode))
        })
        .take(CAP)
        .collect()
}

/// Write the list via a temp file and rename, so a crash mid-write never leaves half a file.
pub fn save(list: &[(String, Mode)]) {
    let Some(p) = path() else { return };
    let entries: Vec<Entry> = list
        .iter()
        .take(CAP)
        .map(|(q, m)| Entry {
            query: q.clone(),
            mode: match m {
                Mode::Name => "name",
                Mode::Parenthetical => "parenthetical",
            }
            .into(),
        })
        .collect();
    let Ok(text) = serde_json::to_string_pretty(&entries) else { return };
    let tmp = p.with_extension("json.tmp");
    let _ = p.parent().map(std::fs::create_dir_all);
    if std::fs::write(&tmp, text).is_ok() {
        let _ = std::fs::rename(&tmp, &p);
    }
}
