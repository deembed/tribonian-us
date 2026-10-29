//! View Dockets data: the court tree (`tribonian dockets courts` -> cases/dockets/_courts.json) and the
//! fetched RECAP dockets (`tribonian dockets index` -> cases/recap/_index.json; recap fetch and build refresh it).
//! The app only reads these; every change goes through the scripts in the build queue.

use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Court {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub short: String,
    #[serde(default)]
    pub cite: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Federal {
    #[serde(default)]
    pub supreme: Vec<Court>,
    #[serde(default)]
    pub appeals: Vec<Court>,
    #[serde(default)]
    pub district: Vec<Court>,
    #[serde(default)]
    pub bankruptcy: Vec<Court>,
    #[serde(default)]
    pub special: Vec<Court>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct State {
    pub name: String,
    #[serde(default)]
    pub highest: Vec<Court>,
    #[serde(default)]
    pub appellate: Vec<Court>,
    #[serde(default)]
    pub trial: Vec<Court>,
    #[serde(default)]
    pub special: Vec<Court>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct CourtTree {
    #[serde(default)]
    pub made: String,
    #[serde(default)]
    pub source: String,
    pub federal: Federal,
    /// postal code -> the state's courts (the file is written sorted by state name)
    pub state: BTreeMap<String, State>,
}

impl CourtTree {
    /// The federal courts whose dockets RECAP holds (everything federal but the Supreme Court).
    pub fn recap_courts(&self) -> impl Iterator<Item = &Court> {
        let f = &self.federal;
        f.appeals.iter().chain(f.district.iter()).chain(f.bankruptcy.iter()).chain(f.special.iter())
    }
    pub fn court(&self, id: &str) -> Option<&Court> {
        let f = &self.federal;
        f.supreme
            .iter()
            .chain(self.recap_courts())
            .chain(self.state.values().flat_map(|s| s.highest.iter().chain(&s.appellate).chain(&s.trial).chain(&s.special)))
            .find(|c| c.id == id)
    }
    /// States sorted by name: (postal code, state).
    pub fn states(&self) -> Vec<(&String, &State)> {
        let mut v: Vec<_> = self.state.iter().collect();
        v.sort_by(|a, b| a.1.name.cmp(&b.1.name));
        v
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Doc {
    pub key: String,
    #[serde(default)]
    pub filed: String,
    #[serde(default)]
    pub pages: Option<i64>,
    #[serde(default)]
    pub description: String,
    /// "text" (fetched, text saved), "recap" (in RECAP, not fetched), "pacer" (not in RECAP: PACER only)
    #[serde(default)]
    pub status: String,
    /// The court's own writing (an order, opinion, judgment ...), which recap fetch picks by default.
    #[serde(default)]
    pub order: bool,
    #[serde(default)]
    pub pacer: String,
    #[serde(default)]
    pub courtlistener: String,
    /// The document's note, relative to the work folder ("" when not built).
    #[serde(default)]
    pub note: String,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct RecapDocket {
    pub file: String,
    pub id: i64,
    pub court: String,
    #[serde(default)]
    pub number: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub filed: String,
    #[serde(default)]
    pub terminated: String,
    #[serde(default)]
    pub nature: String,
    #[serde(default)]
    pub courtlistener: String,
    #[serde(default)]
    pub note: String,
    #[serde(default)]
    pub pdfs: Vec<String>,
    #[serde(default)]
    pub documents: Vec<Doc>,
}

impl RecapDocket {
    pub fn open(&self) -> bool {
        self.terminated.is_empty()
    }
    pub fn status(&self) -> String {
        if self.open() {
            format!("open · filed {}", self.filed)
        } else {
            format!("closed {} · filed {}", self.terminated, self.filed)
        }
    }
    pub fn count(&self, status: &str) -> usize {
        self.documents.iter().filter(|d| d.status == status).count()
    }
}

/// The queue key of a docket's jobs (negative, clear of case refs and scotus.rs's keys).
pub fn docket_key(id: i64) -> i64 {
    -(2_000_000_000 + id)
}

/// The queue key of a fetch by court and number, before the docket's id is known.
pub fn add_key(court: &str, number: &str) -> i64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    (court, number).hash(&mut h);
    -(3_000_000_000 + (h.finish() % 1_000_000_000) as i64)
}

/// The key of `tribonian dockets courts` in the queue.
pub const COURTS_KEY: i64 = -2;

fn read<T: for<'de> Deserialize<'de>>(p: &Path) -> Result<Option<T>, String> {
    match std::fs::read_to_string(p) {
        Ok(t) => serde_json::from_str(&t).map(Some).map_err(|e| format!("{}: {e}", p.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("{}: {e}", p.display())),
    }
}

pub fn load_courts(cases_dir: &Path) -> Result<Option<CourtTree>, String> {
    read(&cases_dir.join("dockets").join("_courts.json"))
}

#[derive(Deserialize)]
struct Index {
    #[serde(default)]
    dockets: Vec<RecapDocket>,
}

pub fn load_index(cases_dir: &Path) -> Result<Vec<RecapDocket>, String> {
    Ok(read::<Index>(&cases_dir.join("recap").join("_index.json"))?.map(|i| i.dockets).unwrap_or_default())
}

// ------------------------------------------------------------------ Search Dockets

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Hit {
    pub id: i64,
    #[serde(default)]
    pub court: String,
    #[serde(default)]
    pub number: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub filed: String,
    #[serde(default)]
    pub terminated: String,
    #[serde(default)]
    pub courtlistener: String,
    /// already fetched here (CourtListener hits only)
    #[serde(default)]
    pub fetched: bool,
}

/// `tribonian dockets search --json`: the fetched dockets that match, and CourtListener's (None when offline).
#[derive(Clone, Debug, Default, Deserialize)]
pub struct SearchResult {
    #[serde(default)]
    pub mode: String,
    #[serde(default)]
    pub fetched: Vec<Hit>,
    #[serde(default)]
    pub api: Option<Vec<Hit>>,
    #[serde(default)]
    pub count: Option<i64>,
    #[serde(default)]
    pub cached: bool,
    #[serde(default)]
    pub error: String,
}

pub struct SearchResp {
    pub query: String,
    pub result: Result<SearchResult, String>,
}

/// Run the search on its own thread (it may make one API request).
pub fn spawn_search(py: crate::config::Py, query: String, tx: std::sync::mpsc::Sender<SearchResp>, wake: impl Fn() + Send + 'static) {
    std::thread::spawn(move || {
        let out = py.cmd().args(["dockets", "search", "--json", "--"]).args(query.split_whitespace()).output();
        let result = match out {
            Err(e) => Err(format!("{} -m tribonian: {e}", py.python)),
            Ok(o) => {
                let stdout = String::from_utf8_lossy(&o.stdout);
                match stdout.lines().rev().find(|l| l.starts_with('{')) {
                    Some(line) if o.status.success() => serde_json::from_str::<SearchResult>(line).map_err(|e| e.to_string()),
                    _ => {
                        let err = String::from_utf8_lossy(&o.stderr);
                        let tail: Vec<&str> = err.lines().rev().take(3).collect();
                        Err(tail.into_iter().rev().collect::<Vec<_>>().join(" · "))
                    }
                }
            }
        };
        let _ = tx.send(SearchResp { query, result });
        wake();
    });
}
