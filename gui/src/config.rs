//! Where the app finds things.
//!
//! The scripts are the one source of truth. At start the app runs `check.py --paths` in the repo and
//! uses what it prints: each data path (CL_* variable, else ~/.config/tribonian-us/config.toml, else
//! the default; see tconfig.py), the config file's name, and its [gui] table. So the app and the
//! scripts read the same files, and the app needs no config of its own.
//!
//! The GUI's settings (default view, Obsidian vault, interpreter) go in the config file's [gui] table.
//! gui/gui.toml is the old place: still read, it overrides [gui], and a path in it overrides the
//! scripts' path for the app only. Check setup flags both.

use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::process::Command;

/// gui.toml as written: every key optional.
#[derive(Deserialize, Default)]
struct FileConfig {
    repo_dir: Option<PathBuf>,
    bulk_dir: Option<PathBuf>,
    default_view: Option<String>,
    obsidian_vault: Option<String>,
    obsidian_cases_dir: Option<String>,
    search_db: Option<PathBuf>,
    main_db: Option<PathBuf>,
    opinions_db: Option<PathBuf>,
    python: Option<String>,
}

/// check.py --paths
#[derive(Deserialize)]
struct ScriptPaths {
    main_db: String,
    opinions_db: String,
    search_db: String,
    cocite_dir: String,
    #[serde(default)]
    config_file: Option<String>,
    #[serde(default)]
    config_error: Option<String>,
    /// The config file's [gui] table.
    #[serde(default)]
    gui: serde_json::Map<String, serde_json::Value>,
}

#[derive(Clone, Debug)]
pub struct Config {
    /// Folder holding cocite*.sqlite.
    pub bulk_dir: PathBuf,
    /// The tribonian-us repo: cases/, md-cases/, pdf/, data/cocite_labels.json, the scripts.
    pub repo_dir: PathBuf,
    /// File stem of the view opened at start, e.g. "cocite-appellate-sentence-m0.2".
    pub default_view: String,
    /// Obsidian vault name, for obsidian:// links. None hides "Open note".
    pub obsidian_vault: Option<String>,
    /// Where md-cases/ is mirrored inside the vault (sync_vault.sh).
    pub obsidian_cases_dir: String,
    /// casedb.py's full-text index.
    pub search_db: Option<PathBuf>,
    /// casedb.py's main database (clusters, cites, dockets).
    pub main_db: Option<PathBuf>,
    /// casedb.py's opinion-text database.
    pub opinions_db: Option<PathBuf>,
    /// Interpreter for the repo's scripts. Default "python3".
    pub python: String,
    /// Where the data paths came from: None = check.py --paths; Some(why not) = the app's own defaults.
    pub paths_error: Option<String>,
    /// Path keys set in gui.toml, which override the scripts' paths.
    pub overrides: Vec<&'static str>,
    /// The scripts' config file (~/.config/tribonian-us/config.toml), if there is one, and why it was
    /// ignored if it was.
    pub config_file: Option<PathBuf>,
    pub config_error: Option<String>,
}

/// The scripts run in the repo, so a relative path in a CL_* variable is relative to it.
fn resolve(repo: &Path, s: &str) -> PathBuf {
    let p = PathBuf::from(s);
    if p.is_absolute() { p } else { repo.join(p) }
}

fn script_paths(python: &str, repo: &Path) -> Result<ScriptPaths, String> {
    let o = Command::new(python)
        .current_dir(repo)
        .args(["check.py", "--paths"])
        .output()
        .map_err(|e| format!("could not run {python} check.py --paths: {e}"))?;
    serde_json::from_slice(&o.stdout)
        .map_err(|e| format!("check.py --paths gave no paths ({e}): {}", String::from_utf8_lossy(&o.stderr).trim()))
}

impl Config {
    /// Returns the config, and the (old) gui.toml it read, if any: the first CLI argument, else
    /// ./gui.toml, else gui.toml beside Cargo.toml.
    pub fn load() -> Result<(Config, Option<PathBuf>), String> {
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
        let mut candidates: Vec<PathBuf> = std::env::args().skip(1).take(1).map(PathBuf::from).collect();
        candidates.push(PathBuf::from("gui.toml"));
        candidates.push(manifest.join("gui.toml"));
        let mut file = None;
        let mut fc = FileConfig::default();
        for p in candidates {
            if p.is_file() {
                let text = std::fs::read_to_string(&p).map_err(|e| format!("{}: {e}", p.display()))?;
                fc = toml::from_str(&text).map_err(|e| format!("{}: {e}", p.display()))?;
                file = Some(p);
                break;
            }
        }
        // The repo: gui.toml, else the folder above gui/ (where this app was built from).
        let repo_dir = fc.repo_dir.clone().or_else(|| manifest.parent().map(Path::to_path_buf)).unwrap_or_default();
        if !repo_dir.join("check.py").is_file() {
            return Err(format!(
                "No tribonian-us scripts in {}. Set repo_dir in gui.toml (copy gui.toml.example).",
                repo_dir.display()
            ));
        }
        let python = fc.python.clone().unwrap_or_else(|| "python3".into());
        let (sp, paths_error) = match script_paths(&python, &repo_dir) {
            Ok(sp) => (Some(sp), None),
            Err(e) => (None, Some(e)),
        };
        let from = |set: &Option<PathBuf>, pick: fn(&ScriptPaths) -> &String| -> Option<PathBuf> {
            set.clone().or_else(|| sp.as_ref().map(|s| resolve(&repo_dir, pick(s))))
        };
        let mut overrides = Vec::new();
        for (k, v) in [("bulk_dir", &fc.bulk_dir), ("main_db", &fc.main_db), ("opinions_db", &fc.opinions_db), ("search_db", &fc.search_db)] {
            if v.is_some() {
                overrides.push(k);
            }
        }
        // GUI settings: gui.toml, else the config file's [gui], else the default
        let g = |k: &str| -> Option<String> {
            sp.as_ref().and_then(|s| s.gui.get(k)).and_then(|v| v.as_str()).map(String::from)
        };
        let default_view = fc.default_view.clone().or_else(|| g("default_view"));
        let obsidian_vault = fc.obsidian_vault.clone().or_else(|| g("obsidian_vault"));
        let obsidian_cases_dir = fc.obsidian_cases_dir.clone().or_else(|| g("obsidian_cases_dir"));
        let python = fc.python.clone().or_else(|| g("python")).unwrap_or(python);
        let config_file = sp.as_ref().and_then(|s| s.config_file.as_ref()).map(PathBuf::from);
        let config_error = sp.as_ref().and_then(|s| s.config_error.clone());
        let cfg = Config {
            bulk_dir: from(&fc.bulk_dir, |s| &s.cocite_dir).unwrap_or_else(|| repo_dir.join("bulk")),
            main_db: from(&fc.main_db, |s| &s.main_db),
            opinions_db: from(&fc.opinions_db, |s| &s.opinions_db),
            search_db: from(&fc.search_db, |s| &s.search_db),
            default_view: default_view.unwrap_or_else(|| "cocite-appellate-sentence-m0.2".into()),
            obsidian_vault,
            obsidian_cases_dir: obsidian_cases_dir.unwrap_or_else(|| "08-Reference/Cases".into()),
            python,
            repo_dir,
            paths_error,
            overrides,
            config_file,
            config_error,
        };
        Ok((cfg, file))
    }

    // The fallbacks below apply only when check.py --paths could not run.
    pub fn search_db(&self) -> PathBuf {
        self.search_db.clone().unwrap_or_else(|| self.bulk_dir.join("search.sqlite"))
    }
    pub fn main_db(&self) -> PathBuf {
        self.main_db.clone().unwrap_or_else(|| self.bulk_dir.join("courtlistener.sqlite"))
    }
    pub fn opinions_db(&self) -> PathBuf {
        self.opinions_db.clone().unwrap_or_else(|| self.repo_dir.join("db").join("opinions.sqlite"))
    }
    pub fn labels_file(&self) -> PathBuf {
        self.repo_dir.join("data").join("cocite_labels.json")
    }
    pub fn cases_dir(&self) -> PathBuf {
        self.repo_dir.join("cases")
    }
    pub fn notes_dir(&self) -> PathBuf {
        self.repo_dir.join("md-cases")
    }
    /// Where Export SVG on the area graph writes: the only place in the repo the app itself writes to.
    pub fn exports_dir(&self) -> PathBuf {
        self.repo_dir.join("build").join("exports")
    }
    pub fn pdf_dir(&self) -> PathBuf {
        self.repo_dir.join("pdf")
    }
}
