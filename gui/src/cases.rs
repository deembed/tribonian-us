//! The cases already built: cases/_index.json plus the md-cases/ folder tree and pdf/ mirror.
//! Small files on the fast disk, so this is read on the UI thread at start (and on Reload).

use crate::config::Config;
use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct IndexEntry {
    pub slug: String,
    pub name: String,
    pub cite: String,
    pub year: String,
    pub court: String,
    pub full: bool,
}

#[derive(Clone, Debug)]
pub struct Note {
    /// File stem = the case's slug for case notes.
    pub stem: String,
    /// Folder relative to md-cases/ ("" for the top level).
    pub folder: String,
    pub path: PathBuf,
    pub r#ref: Option<i64>,
    pub title: String,
    pub pdf: Option<PathBuf>,
    /// `scotus.py build` notes (docket and filing notes): the docket number from the front matter.
    pub docket: Option<String>,
    /// A filing note's `source`: the URL of the filed PDF, which matches the docket entry's link.
    pub source: Option<String>,
}

#[derive(Default)]
pub struct CaseFiles {
    /// Parsed front matter, per note, read on first view.
    fm: std::cell::RefCell<HashMap<PathBuf, Vec<(String, String)>>>,
    pub index: HashMap<i64, IndexEntry>,
    pub folders: BTreeMap<String, Vec<Note>>,
    pub by_ref: HashMap<i64, (String, usize)>,
    pub error: Option<String>,
}

fn walk(dir: &Path, root: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, root, out);
        } else if p.extension().and_then(|x| x.to_str()) == Some("md") {
            out.push(p);
        }
    }
}

impl CaseFiles {
    pub fn load(cfg: &Config) -> CaseFiles {
        let mut cf = CaseFiles::default();
        let ip = cfg.cases_dir().join("_index.json");
        match std::fs::read_to_string(&ip).map_err(|e| e.to_string()).and_then(|t| {
            serde_json::from_str::<serde_json::Value>(&t).map_err(|e| e.to_string())
        }) {
            Ok(v) => {
                for (k, e) in v.as_object().into_iter().flatten() {
                    let Ok(r) = k.parse::<i64>() else { continue };
                    let s = |f: &str| match &e[f] {
                        serde_json::Value::String(s) => s.clone(),
                        serde_json::Value::Null => String::new(),
                        other => other.to_string(),
                    };
                    cf.index.insert(
                        r,
                        IndexEntry {
                            slug: s("slug"),
                            name: s("name"),
                            cite: s("cite"),
                            year: s("year"),
                            court: s("court"),
                            full: !matches!(e["full"], serde_json::Value::Null | serde_json::Value::Bool(false))
                                && !s("full").is_empty(),
                        },
                    );
                }
            }
            Err(e) => cf.error = Some(format!("{}: {e}", ip.display())),
        }
        let slug_ref: HashMap<&str, i64> = cf.index.iter().map(|(r, e)| (e.slug.as_str(), *r)).collect();
        let root = cfg.notes_dir();
        let mut files = Vec::new();
        walk(&root, &root, &mut files);
        let mut folders: BTreeMap<String, Vec<Note>> = BTreeMap::new();
        for p in files {
            let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_string();
            let folder = p
                .parent()
                .and_then(|d| d.strip_prefix(&root).ok())
                .map(|d| d.to_string_lossy().into_owned())
                .unwrap_or_default();
            let r = slug_ref.get(stem.as_str()).copied();
            let title = r
                .and_then(|r| cf.index.get(&r))
                .map(|e| if e.cite.is_empty() { e.name.clone() } else { format!("{}, {}", e.name, e.cite) })
                .unwrap_or_else(|| stem.clone());
            let pdf = cfg.pdf_dir().join(&folder).join(format!("{stem}.pdf"));
            // Supreme Court docket and filing notes are no cases in the index; their front matter says
            // which docket they belong to, so View Cases can open the docket page for them
            let (mut docket, mut source, mut title) = (None, None, title);
            if r.is_none() && stem.starts_with("scotus-") {
                let fm = head_matter(&p);
                let get = |k: &str| fm.iter().find(|(x, _)| x == k).map(|(_, v)| v.clone()).filter(|v| !v.is_empty());
                docket = get("docket");
                source = get("source");
                if let Some(t) = get("title") {
                    title = t;
                }
            }
            folders.entry(folder.clone()).or_default().push(Note {
                pdf: pdf.is_file().then_some(pdf),
                stem,
                folder,
                path: p,
                r#ref: r,
                title,
                docket,
                source,
            });
        }
        for (f, notes) in folders.iter_mut() {
            notes.sort_by(|a, b| a.title.to_lowercase().cmp(&b.title.to_lowercase()));
            for (i, n) in notes.iter().enumerate() {
                if let Some(r) = n.r#ref {
                    cf.by_ref.insert(r, (f.clone(), i));
                }
            }
        }
        cf.folders = folders;
        cf
    }

    pub fn note_of(&self, r: i64) -> Option<&Note> {
        let (f, i) = self.by_ref.get(&r)?;
        self.folders.get(f)?.get(*i)
    }

    /// The note's YAML front matter as (key, value) rows; list items are joined with " · ".
    pub fn front_matter(&self, path: &Path) -> Vec<(String, String)> {
        if let Some(v) = self.fm.borrow().get(path) {
            return v.clone();
        }
        let mut out: Vec<(String, String)> = Vec::new();
        if let Ok(text) = std::fs::read_to_string(path) {
            let mut lines = text.lines();
            if lines.next() == Some("---") {
                for l in lines {
                    if l == "---" {
                        break;
                    }
                    let unq = |v: &str| v.trim().trim_matches('"').to_string();
                    if let Some(item) = l.trim_start().strip_prefix("- ") {
                        if let Some(last) = out.last_mut() {
                            if !last.1.is_empty() {
                                last.1.push_str(" · ");
                            }
                            last.1.push_str(&unq(item));
                        }
                    } else if let Some((k, v)) = l.split_once(':') {
                        out.push((k.trim().to_string(), unq(v)));
                    }
                }
            }
        }
        self.fm.borrow_mut().insert(path.to_path_buf(), out.clone());
        out
    }

    /// The notes `scotus.py build` wrote for a docket: (the docket note, the filing notes).
    pub fn docket_notes(&self, num: &str) -> (Option<&Note>, Vec<&Note>) {
        let mine: Vec<&Note> = self.folders.values().flatten().filter(|n| n.docket.as_deref() == Some(num)).collect();
        let docket = mine.iter().find(|n| n.source.as_deref().is_some_and(|s| s.contains("/RSS/Cases/JSON/"))).copied();
        let filings = mine.into_iter().filter(|n| docket.is_none_or(|d| d.path != n.path)).collect();
        (docket, filings)
    }

    pub fn count(&self) -> usize {
        self.folders.values().map(|v| v.len()).sum()
    }
}

/// The first lines of a note's YAML front matter as (key, value), quotes removed; list items skipped.
/// Reads at most 8 KB: front matter is at the top, and filing notes run to 90 KB.
fn head_matter(path: &Path) -> Vec<(String, String)> {
    use std::io::Read;
    let mut buf = Vec::new();
    if let Ok(f) = std::fs::File::open(path) {
        let _ = f.take(8192).read_to_end(&mut buf);
    }
    let text = String::from_utf8_lossy(&buf);
    let mut lines = text.lines();
    let mut out = Vec::new();
    if lines.next() != Some("---") {
        return out;
    }
    for l in lines {
        if l == "---" {
            break;
        }
        if l.starts_with(' ') || l.starts_with('-') {
            continue;
        }
        if let Some((k, v)) = l.split_once(':') {
            out.push((k.trim().to_string(), v.trim().trim_matches('"').to_string()));
        }
    }
    out
}

/// Open a file or URL with the desktop's default handler.
pub fn open_external(target: &str) {
    let _ = std::process::Command::new("xdg-open").arg(target).spawn();
}

/// obsidian://open?vault=..&file=.. for a note under md-cases/.
pub fn obsidian_url(cfg: &Config, note: &Note) -> Option<String> {
    let vault = cfg.obsidian_vault.as_ref()?;
    let mut file = cfg.obsidian_cases_dir.trim_end_matches('/').to_string();
    if !note.folder.is_empty() {
        file.push('/');
        file.push_str(&note.folder);
    }
    file.push('/');
    file.push_str(&note.stem);
    Some(format!("obsidian://open?vault={}&file={}", pct(vault), pct(&file)))
}

fn pct(s: &str) -> String {
    let mut o = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => o.push(b as char),
            _ => o.push_str(&format!("%{b:02X}")),
        }
    }
    o
}
