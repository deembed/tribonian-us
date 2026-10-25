//! "Check setup": for a first install, or when something stops working. Two parts:
//!
//! 1. The repo's `check.py --json` on a thread: is each tool and database there, does each one answer
//!    a probe query (one known case, State Farm, through an index), and which data mode (api, lite,
//!    full) the data supports. The checks live in the Python so the command line and the app agree.
//! 2. The app's own checks, done here: gui.toml, the co-citation views it found, and whether gui.toml
//!    points at the same databases the scripts use. The two are configured separately (gui.toml vs
//!    the CL_* environment variables), so a mismatch is an easy first-install mistake.
//!
//! Reads only. The live API request is opt-in (it uses one of the day's requests).

use crate::App;
use eframe::egui::{self, Color32, RichText, Ui};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc::{channel, Receiver};

#[derive(Clone, Debug, Deserialize)]
pub struct Item {
    pub group: String,
    pub label: String,
    pub status: String,
    pub detail: String,
    #[serde(default)]
    pub fix: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ModeState {
    pub ok: bool,
    pub missing: Vec<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Report {
    pub best: Option<String>,
    pub modes: BTreeMap<String, ModeState>,
    pub checks: Vec<Item>,
    #[serde(default)]
    pub paths: BTreeMap<String, String>,
}

#[derive(Default)]
pub struct CheckUi {
    /// Include check.py --api (one live request).
    pub api: bool,
    pub result: Option<Result<Report, String>>,
    rx: Option<Receiver<Result<Report, String>>>,
    started: bool,
}

fn spawn(python: String, repo: PathBuf, api: bool, wake: impl Fn() + Send + 'static) -> Receiver<Result<Report, String>> {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let mut args = vec!["check.py".to_string(), "--json".to_string()];
        if api {
            args.push("--api".into());
        }
        // check.py exits 1 when no mode is ready; its JSON is on stdout either way
        let r = match Command::new(&python).current_dir(&repo).args(&args).output() {
            Err(e) => Err(format!("could not run {python} check.py in {}: {e}", repo.display())),
            Ok(o) => serde_json::from_slice::<Report>(&o.stdout)
                .map_err(|e| format!("check.py gave no report ({e}):\n{}", String::from_utf8_lossy(&o.stderr))),
        };
        let _ = tx.send(r);
        wake();
    });
    rx
}

fn start(app: &mut App) {
    app.check_ui.started = true;
    let Some(cfg) = &app.cfg else { return };
    let ctx = app.egui_ctx.clone();
    app.check_ui.rx = Some(spawn(cfg.python.clone(), cfg.repo_dir.clone(), app.check_ui.api, move || {
        if let Some(c) = &ctx {
            c.request_repaint();
        }
    }));
}

/// One of the app's own checks, shaped like check.py's.
struct Local {
    status: &'static str,
    label: String,
    detail: String,
    fix: String,
}

fn local(status: &'static str, label: &str, detail: String, fix: &str) -> Local {
    Local { status, label: label.into(), detail, fix: fix.into() }
}

/// The scripts run with the repo as working directory, so their relative paths are relative to it.
fn resolve(repo: &Path, s: &str) -> PathBuf {
    let p = PathBuf::from(s);
    if p.is_absolute() {
        p
    } else {
        repo.join(p)
    }
}

/// Same file once symlinks are followed (bulk/ is usually a link to another disk).
fn same(a: &Path, b: &Path) -> bool {
    match (a.canonicalize(), b.canonicalize()) {
        (Ok(x), Ok(y)) => x == y,
        _ => a == b,
    }
}

fn local_checks(app: &App) -> Vec<Local> {
    let mut out = Vec::new();
    let cfg = match (&app.cfg, &app.cfg_err) {
        (Some(c), _) => c,
        (None, e) => {
            out.push(local(
                "fail",
                "gui.toml",
                e.clone().unwrap_or_else(|| "not loaded".into()),
                "cp gui/gui.toml.example gui/gui.toml   and set bulk_dir and repo_dir",
            ));
            return out;
        }
    };
    let path = app.cfg_path.as_ref().map(|p| p.display().to_string()).unwrap_or_default();
    out.push(local("ok", "gui.toml", path, ""));
    if cfg.repo_dir.join("check.py").is_file() {
        out.push(local("ok", "repo_dir", cfg.repo_dir.display().to_string(), ""));
    } else {
        out.push(local("fail", "repo_dir", format!("{}: no check.py there", cfg.repo_dir.display()), "set repo_dir in gui.toml to the tribonian-us folder"));
    }
    if !cfg.bulk_dir.is_dir() {
        out.push(local("fail", "bulk_dir", format!("{}: not a folder (disk not mounted?)", cfg.bulk_dir.display()), "set bulk_dir in gui.toml"));
    }
    let good = app.views.iter().filter(|v| v.ok).count();
    let old: Vec<String> = app.views.iter().filter(|v| !v.ok).map(|v| format!("{} (schema {})", v.stem, v.schema)).collect();
    let default_found = app.views.iter().any(|v| v.ok && v.stem == cfg.default_view);
    let detail = format!(
        "{good} usable in {}{}; default_view {} {}",
        cfg.bulk_dir.display(),
        if old.is_empty() { String::new() } else { format!("; older schema, skipped: {}", old.join(", ")) },
        cfg.default_view,
        if default_found { "found" } else { "NOT found" }
    );
    // Old-schema files are only skipped (the app never opens them), so they don't make this a warning.
    let st = if good > 0 && default_found { "ok" } else { "warn" };
    out.push(local(st, "Co-citation views", detail, if good == 0 { "./cocite.py build (README stage 5)" } else { "" }));
    // gui.toml vs the scripts' paths
    if let Some(Ok(rep)) = &app.check_ui.result {
        let pairs = [
            ("main_db", "Main database", cfg.main_db(), "main_db (or bulk_dir)", "CL_DB"),
            ("opinions_db", "Opinion text", cfg.opinions_db(), "opinions_db", "CL_OPS_DB"),
            ("search_db", "Search index", cfg.search_db(), "search_db", "CL_SEARCH_DB"),
            ("cocite_dir", "Co-citation folder", cfg.bulk_dir.clone(), "bulk_dir", "CL_COCITE_DB"),
        ];
        for (key, label, gui, toml_key, env) in pairs {
            let Some(s) = rep.paths.get(key) else { continue };
            let script = resolve(&cfg.repo_dir, s);
            let label = format!("Same {} as the scripts", label.to_lowercase());
            if same(&gui, &script) {
                out.push(local("ok", &label, gui.display().to_string(), ""));
            } else {
                out.push(Local {
                    status: "warn",
                    label,
                    detail: format!("app reads {}, scripts use {}", gui.display(), script.display()),
                    fix: format!("make {toml_key} in gui.toml and {env} (or the default) agree"),
                });
            }
        }
    }
    out
}

fn mark(ui: &mut Ui, status: &str) {
    let (t, c) = match status {
        "ok" => ("OK  ", Color32::from_rgb(90, 170, 100)),
        "warn" => ("WARN", Color32::from_rgb(220, 170, 50)),
        "fail" => ("FAIL", Color32::LIGHT_RED),
        _ => ("--  ", Color32::GRAY),
    };
    ui.label(RichText::new(t).monospace().strong().color(c));
}

fn row(ui: &mut Ui, status: &str, label: &str, detail: &str, fix: &str) {
    ui.horizontal_wrapped(|ui| {
        mark(ui, status);
        ui.label(RichText::new(label).strong());
        ui.label(detail);
    });
    if !fix.is_empty() && (status == "warn" || status == "fail") {
        ui.horizontal_wrapped(|ui| {
            ui.add_space(44.0);
            ui.label(RichText::new(format!("fix: {fix}")).monospace().weak());
            if ui.small_button("Copy").on_hover_text("copy the command").clicked() {
                ui.ctx().copy_text(fix.to_string());
            }
        });
    }
}

pub fn check_page(app: &mut App, ui: &mut Ui) {
    if let Some(rx) = &app.check_ui.rx {
        if let Ok(r) = rx.try_recv() {
            app.check_ui.result = Some(r);
            app.check_ui.rx = None;
        }
    }
    if !app.check_ui.started {
        start(app);
    }
    let running = app.check_ui.rx.is_some();
    ui.heading("Check setup");
    ui.weak("Is each piece there, and does it answer a query? The same checks as ./check.py on the command line.");
    ui.horizontal(|ui| {
        if ui.add_enabled(!running && app.cfg.is_some(), egui::Button::new("Run again")).clicked() {
            start(app);
        }
        ui.checkbox(&mut app.check_ui.api, "include one live CourtListener request");
        if running {
            ui.spinner();
            ui.weak("checking…");
        }
    });
    ui.separator();
    let locals = local_checks(app);
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        if let Some(r) = &app.check_ui.result {
            match r {
                Err(e) => {
                    ui.colored_label(Color32::LIGHT_RED, e);
                }
                Ok(rep) => {
                    ui.label(RichText::new(format!("Data mode: {}", rep.best.as_deref().unwrap_or("none"))).size(18.0).strong())
                        .on_hover_text("api: everything from CourtListener · lite: small bulk tables local, opinion text from the API · full: everything local");
                    for m in ["api", "lite", "full"] {
                        if let Some(s) = rep.modes.get(m) {
                            row(ui, if s.ok { "ok" } else { "fail" }, m, &if s.ok { "ready".to_string() } else { format!("needs {}", s.missing.join(", ")) }, "");
                        }
                    }
                    let mut group = "";
                    for it in &rep.checks {
                        if it.group != group {
                            group = &it.group;
                            ui.add_space(10.0);
                            ui.label(RichText::new(group).size(16.0).strong());
                        }
                        row(ui, &it.status, &it.label, &it.detail, &it.fix);
                    }
                }
            }
        }
        ui.add_space(10.0);
        ui.label(RichText::new("This app").size(16.0).strong());
        for l in &locals {
            row(ui, l.status, &l.label, &l.detail, &l.fix);
        }
    });
}
