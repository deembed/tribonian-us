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
}

#[derive(Default)]
pub struct CaseFiles {
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
            folders.entry(folder.clone()).or_default().push(Note {
                pdf: pdf.is_file().then_some(pdf),
                stem,
                folder,
                path: p,
                r#ref: r,
                title,
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

    pub fn count(&self) -> usize {
        self.folders.values().map(|v| v.len()).sum()
    }
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
