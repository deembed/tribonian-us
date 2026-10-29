//! Search Opinions, in every data mode but offline: cases newer than the local database. Runs the repo's
//! `case2md.py api-search --json` (one API request per search, cached a day under cases/api/search/;
//! the token and the rate limits stay in the Python, as for every other script) on its own thread.
//! Default date filter: filed after the bulk data's date less a month, so it fills the gap only.

use crate::model::Page;
use crate::search::{looks_like_cite, Mode};
use serde::Deserialize;
use crate::config::Py;
use std::sync::mpsc::Sender;

#[derive(Clone, Debug, Deserialize)]
pub struct ApiHit {
    pub cluster: i64,
    pub name: String,
    pub cite: String,
    pub date: String,
    pub court: String,
    pub cited: i64,
    pub snippet: String,
    /// The local database already has it (the local results show it).
    pub local: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ApiResults {
    /// "YYYY-MM-DD", or "" for any date.
    pub after: String,
    pub count: Option<i64>,
    pub hits: Vec<ApiHit>,
    pub cached: bool,
    pub fetched: String,
}

pub struct Resp {
    pub page: Page,
    pub result: Result<ApiResults, String>,
}

/// The script's mode for a search page: a citation, a case name, or full text (Parentheticals).
pub fn api_mode(query: &str, mode: Mode) -> &'static str {
    match mode {
        Mode::Name if looks_like_cite(query) => "cite",
        Mode::Name => "name",
        Mode::Parenthetical => "text",
    }
}

pub fn spawn(py: Py, page: Page, tx: Sender<Resp>, wake: impl Fn() + Send + 'static) {
    let Page::Search { query, mode } = page.clone() else { return };
    std::thread::spawn(move || {
        let out = py
            .cmd()
            .args(["case", "api-search", "--json", "--mode", api_mode(&query, mode), "--"])
            .arg(&query)
            .output();
        let result = match out {
            Err(e) => Err(format!("{} -m tribonian: {e}", py.python)),
            Ok(o) => {
                let stdout = String::from_utf8_lossy(&o.stdout);
                match stdout.lines().rev().find(|l| l.starts_with('{')) {
                    Some(line) if o.status.success() => serde_json::from_str::<ApiResults>(line).map_err(|e| e.to_string()),
                    _ => {
                        let err = String::from_utf8_lossy(&o.stderr);
                        let tail: Vec<&str> = err.lines().rev().take(3).collect();
                        Err(tail.into_iter().rev().collect::<Vec<_>>().join(" · "))
                    }
                }
            }
        };
        let _ = tx.send(Resp { page, result });
        wake();
    });
}
