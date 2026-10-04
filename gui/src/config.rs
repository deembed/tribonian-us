//! gui.toml: every path the app reads. Nothing is hard-coded.

use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Deserialize, Clone, Debug)]
pub struct Config {
    /// Folder holding cocite*.sqlite (the bulk/ symlink target).
    pub bulk_dir: PathBuf,
    /// The LawFormatting repo: cases/, md-cases/, pdf/, cocite_labels.json.
    pub repo_dir: PathBuf,
    /// File stem of the view opened at start, e.g. "cocite-appellate-sentence-m0.2".
    #[serde(default = "default_view")]
    pub default_view: String,
    /// Obsidian vault name, for obsidian:// links. Leave out to hide "Open note".
    #[serde(default)]
    pub obsidian_vault: Option<String>,
    /// Where md-cases/ is mirrored inside the vault (sync_vault.sh).
    #[serde(default = "default_vault_cases")]
    pub obsidian_cases_dir: String,
    /// casedb.py's full-text index; default bulk_dir/search.sqlite. Worth a copy on the SSD:
    /// cold searches on the HDD take seconds.
    #[serde(default)]
    pub search_db: Option<PathBuf>,
    /// casedb.py's main database (clusters, cites, dockets); default bulk_dir/courtlistener.sqlite.
    /// Used for names, citations and courts of cases the cocite views don't hold.
    #[serde(default)]
    pub main_db: Option<PathBuf>,
}

fn default_view() -> String {
    "cocite-appellate-sentence-m0.2".into()
}
fn default_vault_cases() -> String {
    "08-Reference/Cases".into()
}

impl Config {
    /// First CLI argument, else ./gui.toml, else gui.toml beside Cargo.toml.
    pub fn load() -> Result<(Config, PathBuf), String> {
        let mut tried = Vec::new();
        let mut candidates: Vec<PathBuf> = std::env::args().skip(1).take(1).map(PathBuf::from).collect();
        candidates.push(PathBuf::from("gui.toml"));
        candidates.push(Path::new(env!("CARGO_MANIFEST_DIR")).join("gui.toml"));
        for p in candidates {
            if p.is_file() {
                let text = std::fs::read_to_string(&p).map_err(|e| format!("{}: {e}", p.display()))?;
                let cfg: Config = toml::from_str(&text).map_err(|e| format!("{}: {e}", p.display()))?;
                return Ok((cfg, p));
            }
            tried.push(p.display().to_string());
        }
        Err(format!(
            "No gui.toml found (tried {}). Copy gui.toml.example to gui.toml and edit it.",
            tried.join(", ")
        ))
    }

    pub fn search_db(&self) -> PathBuf {
        self.search_db.clone().unwrap_or_else(|| self.bulk_dir.join("search.sqlite"))
    }
    pub fn main_db(&self) -> PathBuf {
        self.main_db.clone().unwrap_or_else(|| self.bulk_dir.join("courtlistener.sqlite"))
    }
    pub fn labels_file(&self) -> PathBuf {
        self.repo_dir.join("cocite_labels.json")
    }
    pub fn cases_dir(&self) -> PathBuf {
        self.repo_dir.join("cases")
    }
    pub fn notes_dir(&self) -> PathBuf {
        self.repo_dir.join("md-cases")
    }
    pub fn pdf_dir(&self) -> PathBuf {
        self.repo_dir.join("pdf")
    }
}
