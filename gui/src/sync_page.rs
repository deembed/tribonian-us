//! Sync: first-run setup and CourtListener bulk data on one page (claude/scope-sync.md in the Project).
//!
//! Rows: Tribonian (the Python pipeline), LaTeX, the CourtListener API key, bulk data. The first is this app's own
//! (bootstrap.rs: uv installs the release's tribonian); everything after it is `tribonian setup` subcommands, so the
//! command line does the same. `tribonian setup --json` reports the rows; the app writes nothing itself: the key goes
//! to `tribonian setup key set` on stdin (never the process list or the log), long steps through the build queue.
//!
//! Start-up rule: the app opens here until the required rows pass (Tribonian, LaTeX, and the key answered: stored or
//! skipped); no first-launch flag, so someone who quits half way comes back here.

use crate::bootstrap;
use crate::config::{Py, NOT_INSTALLED};
use crate::model::Page;
use crate::{App, Queued, QueuedJob};
use eframe::egui::{self, Color32, RichText, Ui};
use serde::Deserialize;
use std::io::Write;
use std::process::Stdio;
use std::sync::mpsc::{channel, Receiver};

/// Build-queue key for setup commands.
pub const SYNC_KEY: i64 = -3;

const GREEN: Color32 = Color32::from_rgb(90, 170, 100);
const AMBER: Color32 = Color32::from_rgb(220, 170, 50);

#[derive(Clone, Debug, Deserialize)]
pub struct Row {
    pub id: String,
    pub required: bool,
    pub status: String,
    pub text: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ModeInfo {
    pub value: String,
    pub set: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct BulkFile {
    pub name: String,
    pub downloaded: Option<String>,
    pub loaded: Option<String>,
    pub latest: Option<String>,
    pub size: Option<u64>,
    pub status: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Space {
    pub path: String,
    pub need_gb: i64,
    pub free_gb: i64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Stale {
    pub what: String,
    pub rebuild: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Bulk {
    pub files: Vec<BulkFile>,
    pub complete_set: Option<String>,
    pub error: String,
    pub bulk_dir: String,
    pub opinions_db: String,
    pub space: Vec<Space>,
    pub stale: Vec<Stale>,
    pub swap_pending: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Report {
    pub version: String,
    pub config_file: String,
    pub required_ok: bool,
    pub rows: Vec<Row>,
    pub has_key: bool,
    pub data: Option<String>,
    pub mode: ModeInfo,
    pub unavailable: String,
    pub bulk: Bulk,
}

impl Report {
    fn row(&self, id: &str) -> Option<&Row> {
        self.rows.iter().find(|r| r.id == id)
    }
}

#[derive(Default)]
pub struct SyncUi {
    pub result: Option<Result<Report, String>>,
    rx: Option<Receiver<Result<Report, String>>>,
    started: bool,
    was_busy: bool,
    /// The last report asked CourtListener for the latest bulk set (Check for updates / Add bulk data).
    remote: bool,
    show_bulk: bool,
    key_draft: String,
    folders: [Option<String>; 2],
    saving: Option<Receiver<Result<String, String>>>,
    message: Option<Result<String, String>>,
    install: Option<Receiver<bootstrap::Msg>>,
    install_log: Vec<String>,
    install_result: Option<Result<(), String>>,
    /// Set up everything: install what is missing, row by row, then stop at the key.
    auto: bool,
    startup_done: bool,
    last_mode: Option<String>,
}

/// The Tribonian row, which the app decides itself.
enum Tri {
    Missing,
    Broken(String),
    Mismatch(String),
    Ok(String),
}

fn tri(app: &App) -> Tri {
    let Some(cfg) = &app.cfg else {
        return match &app.cfg_err {
            Some(e) if e == NOT_INSTALLED => Tri::Missing,
            Some(e) => Tri::Broken(e.clone()),
            None => Tri::Missing,
        };
    };
    match &app.sync_ui.result {
        Some(Ok(r)) if cfg.installed && r.version != bootstrap::VERSION => Tri::Mismatch(r.version.clone()),
        Some(Ok(r)) => Tri::Ok(r.version.clone()),
        Some(Err(e)) => Tri::Broken(e.clone()),
        None => Tri::Ok(String::new()),
    }
}

impl SyncUi {
    /// The start screen's count of required rows still to do: None until known.
    pub fn actions_required(&self) -> Option<usize> {
        match &self.result {
            Some(Ok(r)) => Some(r.rows.iter().filter(|x| x.required && x.status == "fail").count()),
            _ => None,
        }
    }
}

fn refresh(app: &mut App, remote: bool) {
    app.sync_ui.started = true;
    app.sync_ui.remote = remote;
    let Some(cfg) = &app.cfg else { return };
    let py = cfg.py();
    let ctx = app.egui_ctx.clone();
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let mut c = py.cmd();
        c.args(["setup", "--json"]);
        if remote {
            c.arg("--remote");
        }
        let r = match c.output() {
            Err(e) => Err(format!("could not run {} -m tribonian setup: {e}", py.python)),
            Ok(o) => serde_json::from_slice::<Report>(&o.stdout)
                .map_err(|e| format!("tribonian setup gave no report ({e}):\n{}", String::from_utf8_lossy(&o.stderr))),
        };
        let _ = tx.send(r);
        if let Some(c) = &ctx {
            c.request_repaint();
        }
    });
    app.sync_ui.rx = Some(rx);
}

/// `tribonian setup <args>` with input on stdin; Ok(its output) or Err(its error output).
fn run_setup(py: Py, args: Vec<String>, input: String, ctx: Option<egui::Context>) -> Receiver<Result<String, String>> {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let run = || -> Result<String, String> {
            let mut child = py
                .cmd()
                .arg("setup")
                .args(&args)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .map_err(|e| e.to_string())?;
            if let Some(mut i) = child.stdin.take() {
                i.write_all(input.as_bytes()).map_err(|e| e.to_string())?;
            }
            let o = child.wait_with_output().map_err(|e| e.to_string())?;
            if o.status.success() {
                Ok(String::from_utf8_lossy(&o.stdout).trim().to_string())
            } else {
                Err(String::from_utf8_lossy(&o.stderr).trim().to_string())
            }
        };
        let _ = tx.send(run());
        if let Some(c) = &ctx {
            c.request_repaint();
        }
    });
    rx
}

fn start_install(app: &mut App) {
    app.sync_ui.install_log.clear();
    app.sync_ui.install_result = None;
    let ctx = app.egui_ctx.clone();
    app.sync_ui.install = Some(bootstrap::spawn(move || {
        if let Some(c) = &ctx {
            c.request_repaint();
        }
    }));
}

fn queue(app: &mut App, title: &str, steps: Vec<Vec<&str>>) {
    let steps = steps.into_iter().map(|s| (s.into_iter().map(String::from).collect(), None)).collect();
    app.enqueue(Queued { r#ref: SYNC_KEY, title: format!("Sync: {title}"), job: QueuedJob::Steps(steps) });
}

/// Keep the report current (the first time, after every save or install, and whenever the build queue empties),
/// apply the start-up rule, and carry Set up everything forward. Called every frame from the app.
pub fn tick(app: &mut App) {
    if let Some(rx) = &app.sync_ui.install {
        let mut done = None;
        while let Ok(m) = rx.try_recv() {
            match m {
                bootstrap::Msg::Line(l) => app.sync_ui.install_log.push(l),
                bootstrap::Msg::Done(r) => done = Some(r),
            }
        }
        if let Some(r) = done {
            app.sync_ui.install = None;
            if r.is_err() {
                app.sync_ui.auto = false;
            }
            app.sync_ui.install_result = Some(r);
            app.load_config();
            app.sync_ui.result = None;
            refresh(app, false);
        }
    }
    if let Some(rx) = &app.sync_ui.rx {
        if let Ok(r) = rx.try_recv() {
            app.sync_ui.rx = None;
            if let Ok(rep) = &r {
                if let Some(cfg) = &mut app.cfg {
                    cfg.has_key = Some(rep.has_key);
                    // The mode is chosen by tribonian setup only on first setup and when a row changes state;
                    // when it chose one, the app follows it.
                    if app.sync_ui.last_mode.as_ref().is_some_and(|m| *m != rep.mode.value) {
                        cfg.data_mode = rep.mode.value.clone();
                        cfg.mode = rep.mode.value.clone();
                        crate::recent::save_mode(&cfg.mode);
                    }
                }
                app.sync_ui.last_mode = Some(rep.mode.value.clone());
            }
            app.sync_ui.result = Some(r);
        }
    }
    if let Some(rx) = &app.sync_ui.saving {
        if let Ok(r) = rx.try_recv() {
            app.sync_ui.message = Some(r);
            app.sync_ui.saving = None;
            refresh(app, app.sync_ui.remote);
        }
    }
    let busy = app.job.is_some() || !app.queue.is_empty();
    let ended = app.sync_ui.was_busy && !busy;
    app.sync_ui.was_busy = busy;
    if app.cfg.is_some() && (!app.sync_ui.started || (ended && app.sync_ui.rx.is_none())) {
        refresh(app, app.sync_ui.remote);
    }
    let known = app.cfg.is_none() || app.sync_ui.result.is_some();
    let ok = matches!(tri(app), Tri::Ok(_)) && matches!(&app.sync_ui.result, Some(Ok(r)) if r.required_ok);
    if !app.sync_ui.startup_done && known {
        app.sync_ui.startup_done = true;
        if !ok && app.nav.get(app.pos) == Some(&Page::Home) {
            app.goto = Some(Page::Sync);
        }
    }
    if app.sync_ui.auto && app.sync_ui.install.is_none() && app.sync_ui.rx.is_none() && !busy {
        match tri(app) {
            Tri::Missing | Tri::Mismatch(_) => start_install(app),
            Tri::Broken(_) => app.sync_ui.auto = false,
            Tri::Ok(_) => {
                let latex = match &app.sync_ui.result {
                    Some(Ok(r)) => r.row("latex").map(|x| x.status.clone()),
                    _ => None,
                };
                app.sync_ui.auto = false;
                if latex.as_deref() == Some("fail") {
                    queue(app, "LaTeX", vec![vec!["setup", "latex", "install"]]);
                }
            }
        }
    }
}

fn mark(ui: &mut Ui, status: &str) {
    let (t, c) = match status {
        "ok" => ("OK  ", GREEN),
        "skipped" => ("SKIP", AMBER),
        "fail" | "partial" | "stale" => ("TODO", AMBER),
        "busy" => ("... ", Color32::GRAY),
        _ => ("--  ", Color32::GRAY),
    };
    ui.label(RichText::new(t).monospace().strong().color(c));
}

fn gb(bytes: Option<u64>) -> String {
    bytes.map(|b| format!("{:.1} GB", b as f64 / 1e9)).unwrap_or_default()
}

/// What a click asks for; done after the report is no longer borrowed.
enum Do {
    Install,
    /// Download, then load: two jobs, so the databases stay open for browsing during the download and are only
    /// released for the load's swap (App::start_next).
    SyncAll,
    Queue(&'static str, Vec<Vec<String>>),
    Setup(Vec<String>, String),
    Refresh(bool),
    Auto,
}

pub fn sync_page(app: &mut App, ui: &mut Ui) {
    tick(app);
    ui.heading("Sync");
    ui.weak("Setup and CourtListener data. The same steps as tribonian setup on the command line.");
    let mut todo: Option<Do> = None;
    let queued = app.is_queued(SYNC_KEY);
    let installing = app.sync_ui.install.is_some();
    let saving = app.sync_ui.saving.is_some();
    let t = tri(app);
    let result = app.sync_ui.result.clone();
    let rep = result.as_ref().and_then(|r| r.as_ref().ok());
    let required_ok = matches!(t, Tri::Ok(_)) && rep.is_some_and(|r| r.required_ok);
    ui.horizontal(|ui| {
        if !required_ok
            && ui
                .add_enabled(!queued && !installing && !app.sync_ui.auto, egui::Button::new(RichText::new("Set up everything").strong()))
                .on_hover_text("installs each required row in order, then stops at the API key")
                .clicked()
        {
            todo = Some(Do::Auto);
        }
        if ui.add_enabled(app.sync_ui.rx.is_none() && app.cfg.is_some(), egui::Button::new("Run again")).clicked() {
            todo = Some(Do::Refresh(app.sync_ui.remote));
        }
        if ui.button("Check setup").on_hover_text("every check in detail (tribonian check)").clicked() {
            app.goto = Some(Page::Check);
        }
        if app.sync_ui.rx.is_some() || app.sync_ui.auto {
            ui.spinner();
        }
    });
    if let Some(m) = &app.sync_ui.message {
        match m {
            Ok(t) => ui.colored_label(GREEN, t),
            Err(t) => ui.colored_label(Color32::LIGHT_RED, t),
        };
    }
    ui.separator();
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        egui::Grid::new("sync-rows").num_columns(3).spacing([12.0, 10.0]).show(ui, |ui| {
            // Tribonian
            let (status, text) = match &t {
                Tri::Missing => ("fail", "not installed".to_string()),
                Tri::Broken(e) => ("fail", e.clone()),
                Tri::Mismatch(v) => ("fail", format!("tribonian {v} is installed; this app needs {}", bootstrap::VERSION)),
                Tri::Ok(v) if v.is_empty() => ("busy", "checking".to_string()),
                Tri::Ok(_) => ("ok", rep.and_then(|r| r.row("tribonian")).map(|r| r.text.clone()).unwrap_or_default()),
            };
            mark(ui, if installing { "busy" } else { status });
            ui.label(RichText::new("Tribonian").strong()).on_hover_text("the Python pipeline every row below runs");
            ui.vertical(|ui| {
                ui.label(&text);
                let installable = app.cfg.as_ref().is_none_or(|c| c.installed);
                if status == "fail" && installable {
                    let label = if matches!(t, Tri::Mismatch(_)) { "Update" } else { "Install" };
                    if ui
                        .add_enabled(!installing, egui::Button::new(label))
                        .on_hover_text(format!("uv installs tribonian {} from {}", bootstrap::VERSION, bootstrap::wheel()))
                        .clicked()
                    {
                        todo = Some(Do::Install);
                    }
                } else if status == "fail" {
                    ui.label("From the checkout, in a terminal:");
                    ui.label(RichText::new(if cfg!(windows) { ".\\bootstrap.ps1" } else { "./bootstrap.sh" }).monospace());
                }
                if installing || !app.sync_ui.install_log.is_empty() {
                    if let Some(Err(e)) = &app.sync_ui.install_result {
                        ui.colored_label(Color32::LIGHT_RED, e);
                    }
                    egui::CollapsingHeader::new("install log").default_open(installing).show(ui, |ui| {
                        egui::ScrollArea::vertical().max_height(160.0).stick_to_bottom(true).show(ui, |ui| {
                            for l in &app.sync_ui.install_log {
                                ui.label(RichText::new(l).monospace().small());
                            }
                        });
                    });
                }
            });
            ui.end_row();

            let Some(rep) = rep else {
                if let Some(Err(e)) = &result {
                    if app.cfg.is_some() && !matches!(t, Tri::Broken(_)) {
                        ui.label("");
                        ui.colored_label(Color32::LIGHT_RED, e);
                        ui.end_row();
                    }
                }
                return;
            };

            // LaTeX
            if let Some(r) = rep.row("latex") {
                mark(ui, &r.status);
                ui.label(RichText::new("LaTeX").strong()).on_hover_text("pdflatex with XCharter, tcolorbox and ulem: needed for PDFs");
                ui.vertical(|ui| {
                    ui.label(&r.text);
                    if r.status == "fail" {
                        let label = if r.text.contains("not found") { "Install TinyTeX" } else { "Install the packages" };
                        if ui
                            .add_enabled(!queued, egui::Button::new(label))
                            .on_hover_text("tribonian setup latex install: TinyTeX per user (no administrator rights) if there is no TeX, then each package a test document needs")
                            .on_disabled_hover_text("in the build queue")
                            .clicked()
                        {
                            todo = Some(Do::Queue("LaTeX", vec![vec!["setup".into(), "latex".into(), "install".into()]]));
                        }
                    }
                });
                ui.end_row();
            }

            // API key
            if let Some(r) = rep.row("key") {
                mark(ui, &r.status);
                ui.label(RichText::new("CourtListener API key").strong());
                ui.vertical(|ui| {
                    ui.label(&r.text);
                    let draft = &mut app.sync_ui.key_draft;
                    ui.horizontal_wrapped(|ui| {
                        ui.add(egui::TextEdit::singleline(draft).password(true).hint_text(if r.status == "ok" { "paste a new key to replace it" } else { "paste the key" }).desired_width(320.0));
                        if ui.add_enabled(!saving && !draft.trim().is_empty(), egui::Button::new("Save")).clicked() {
                            todo = Some(Do::Setup(vec!["key".into(), "set".into()], draft.trim().to_string()));
                        }
                        if r.status == "fail" && ui.add_enabled(!saving, egui::Button::new("Skip")).on_hover_text("start offline: no CourtListener requests until a key is added").clicked() {
                            todo = Some(Do::Setup(vec!["key".into(), "skip".into()], String::new()));
                        }
                        if r.status == "ok" {
                            if ui.add_enabled(!queued, egui::Button::new("Test")).on_hover_text("one live request; counts against the day's limit").clicked() {
                                todo = Some(Do::Queue("test the key", vec![vec!["setup".into(), "test-token".into()]]));
                            }
                            if ui.add_enabled(!saving, egui::Button::new("Remove")).clicked() {
                                todo = Some(Do::Setup(vec!["key".into(), "forget".into()], String::new()));
                            }
                        }
                    });
                    if r.status != "ok" {
                        ui.horizontal_wrapped(|ui| {
                            ui.hyperlink_to("Make a free CourtListener account", "https://www.courtlistener.com/");
                            ui.weak("then copy the key from your profile's API page. Without an account the limit is 125 requests a day.");
                        });
                    }
                });
                ui.end_row();
            }

            // Bulk data
            if let Some(r) = rep.row("data") {
                mark(ui, &r.status);
                ui.label(RichText::new("Bulk data").strong()).on_hover_text("optional: CourtListener's quarterly database, for the work too large for the API");
                ui.vertical(|ui| {
                    ui.label(&r.text);
                    // Large downloads stay off the default path: behind a button in api and offline-without-data.
                    let hidden = r.status != "ok" && matches!(rep.mode.value.as_str(), "api" | "offline");
                    if hidden && !app.sync_ui.show_bulk {
                        if ui.button("Add bulk data").clicked() {
                            app.sync_ui.show_bulk = true;
                            todo = Some(Do::Refresh(true));
                        }
                    } else if !app.sync_ui.remote && ui.button("Check for updates").on_hover_text("ask CourtListener for the latest set (one request per file)").clicked() {
                        todo = Some(Do::Refresh(true));
                    }
                });
                ui.end_row();
            }

            // Co-citation clusters: built from the bulk data (tribonian setup clusters; README stage 5)
            if let Some(r) = rep.row("clusters") {
                mark(ui, &r.status);
                ui.label(RichText::new("Clusters").strong()).on_hover_text("the co-citation views and areas View Clusters opens");
                ui.vertical(|ui| {
                    ui.label(&r.text);
                    if r.status != "ok" {
                        let label = if r.status == "none" { "Build clusters" } else { "Build clusters again" };
                        if ui
                            .add_enabled(!queued, egui::Button::new(label))
                            .on_hover_text("tribonian setup clusters: prep, both views, the citation positions and the areas; long, and a rerun continues where it stopped")
                            .on_disabled_hover_text("in the build queue")
                            .clicked()
                        {
                            todo = Some(Do::Queue("clusters", vec![vec!["setup".into(), "clusters".into()]]));
                        }
                    }
                });
                ui.end_row();
            }
        });

        let Some(rep) = rep else { return };
        if !rep.unavailable.is_empty() {
            ui.add_space(8.0);
            egui::Frame::group(ui.style()).show(ui, |ui| {
                ui.colored_label(AMBER, &rep.unavailable);
                ui.label("Two ways to get them: paste a CourtListener key above, or Add bulk data.");
            });
        }
        let show = app.sync_ui.show_bulk || rep.data.is_some() || !matches!(rep.mode.value.as_str(), "api" | "offline");
        if show {
            ui.add_space(10.0);
            ui.separator();
            bulk(ui, rep, &mut app.sync_ui.folders, queued, saving, &mut todo);
        }
        ui.add_space(8.0);
        ui.weak(format!("config file: {}", rep.config_file));
    });

    match todo {
        Some(Do::Install) => start_install(app),
        Some(Do::Auto) => app.sync_ui.auto = true,
        Some(Do::Queue(title, steps)) => {
            let steps = steps.into_iter().map(|s| (s, None)).collect();
            app.enqueue(Queued { r#ref: SYNC_KEY, title: format!("Sync: {title}"), job: QueuedJob::Steps(steps) });
        }
        Some(Do::SyncAll) => {
            for (title, step) in [("download", ["setup", "download"]), ("load", ["setup", "update"])] {
                let steps = vec![(step.iter().map(|s| s.to_string()).collect(), None)];
                app.enqueue(Queued { r#ref: SYNC_KEY, title: format!("Sync: {title}"), job: QueuedJob::Steps(steps) });
            }
        }
        Some(Do::Setup(args, input)) => {
            if let Some(cfg) = &app.cfg {
                if args.first().map(String::as_str) == Some("key") {
                    app.sync_ui.key_draft.clear();
                }
                app.sync_ui.saving = Some(run_setup(cfg.py(), args, input, app.egui_ctx.clone()));
            }
        }
        Some(Do::Refresh(remote)) => refresh(app, remote),
        None => {}
    }
}

fn bulk(ui: &mut Ui, rep: &Report, folders: &mut [Option<String>; 2], queued: bool, saving: bool, todo: &mut Option<Do>) {
    let b = &rep.bulk;
    ui.heading("Bulk data");
    ui.weak("CourtListener publishes a full snapshot each quarter: an update is a new download and a rebuild beside the current \
             database, swapped in only when its health check passes. Lite saves disk space, not download size: it still needs \
             the opinions download.");
    ui.add_space(4.0);
    for (i, (key, label, value, hint)) in [
        ("bulk_dir", "Bulk folder", &b.bulk_dir, "the downloads and the main database"),
        ("opinions_db", "Opinion text file", &b.opinions_db, "opinions.sqlite, read hard: use an SSD"),
    ]
    .into_iter()
    .enumerate()
    {
        let d = folders[i].get_or_insert_with(|| value.clone());
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new(label).strong());
            ui.add(egui::TextEdit::singleline(d).desired_width(420.0));
            if ui.add_enabled(!saving && d.trim() != value.trim(), egui::Button::new("Save")).on_hover_text("then restart the app").clicked() {
                *todo = Some(Do::Setup(vec!["set".into(), key.into()], d.trim().to_string()));
            }
            ui.weak(hint);
        });
    }
    for s in &b.space {
        let enough = s.free_gb >= s.need_gb;
        ui.horizontal_wrapped(|ui| {
            mark(ui, if enough { "ok" } else { "fail" });
            ui.label(format!("{}: {} GB free, about {} GB needed there (new data beside the current)", s.path, s.free_gb, s.need_gb));
        });
    }
    if !b.error.is_empty() {
        ui.colored_label(Color32::LIGHT_RED, &b.error);
    }
    ui.add_space(6.0);
    egui::Grid::new("sync-bulk").striped(true).num_columns(6).spacing([14.0, 4.0]).show(ui, |ui| {
        for h in ["File", "Downloaded", "Loaded", "Latest", "Status", ""] {
            ui.label(RichText::new(h).strong());
        }
        ui.end_row();
        for f in &b.files {
            ui.label(&f.name);
            ui.label(f.downloaded.as_deref().unwrap_or("-"));
            ui.label(f.loaded.as_deref().unwrap_or("-"));
            ui.label(match &f.latest {
                Some(d) => format!("{d} ({})", gb(f.size)),
                None => "-".into(),
            });
            let colour = if f.status == "loaded" { GREEN } else { AMBER };
            ui.colored_label(colour, &f.status);
            let action = if f.status.starts_with("partial") {
                Some("Resume")
            } else if f.status == "not downloaded" || f.status == "update available" {
                Some("Download")
            } else {
                None
            };
            if let Some(a) = action {
                if ui.add_enabled(!queued, egui::Button::new(a)).on_disabled_hover_text("in the build queue").clicked() {
                    *todo = Some(Do::Queue("download", vec![vec!["setup".into(), "download".into(), "--only".into(), f.name.clone()]]));
                }
            } else {
                ui.label("");
            }
            ui.end_row();
        }
    });
    ui.add_space(6.0);
    let newer = b.complete_set.as_ref().is_some_and(|d| b.files.iter().any(|f| f.loaded.as_ref() != Some(d)));
    ui.horizontal(|ui| {
        if ui
            .add_enabled(!queued, egui::Button::new(RichText::new("Sync all").strong()))
            .on_hover_text("download what is missing (restartable), then load it beside the current database, check it and swap it in")
            .clicked()
        {
            *todo = Some(Do::SyncAll);
        }
        if (newer || b.swap_pending)
            && ui
                .add_enabled(!queued, egui::Button::new(format!("Load {}", b.complete_set.as_deref().unwrap_or(""))))
                .on_hover_text("the downloaded set: built beside the current database, swapped in when its health check passes")
                .clicked()
        {
            *todo = Some(Do::Queue("load", vec![vec!["setup".into(), "update".into()]]));
        }
    });
    if !b.stale.is_empty() {
        ui.add_space(6.0);
        ui.label(RichText::new("Built from the previous set").strong());
        for s in &b.stale {
            ui.horizontal_wrapped(|ui| {
                ui.label(&s.what);
                if ui.add_enabled(!queued, egui::Button::new("Rebuild")).on_hover_text(&s.rebuild).clicked() {
                    *todo = Some(Do::Queue("clusters", vec![vec!["setup".into(), "clusters".into()]]));
                }
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `tribonian setup --json` on an empty install with one partial download.
    const EMPTY: &str = r#"{"version": "0.1.0", "config_file": "/c/config.toml", "required_ok": false, "rows": [{"id": "tribonian", "required": true, "status": "ok", "text": "tribonian 0.1.0, Python 3.10"}, {"id": "latex", "required": true, "status": "fail", "text": "missing LaTeX packages: XCharter.sty"}, {"id": "key", "required": true, "status": "fail", "text": "not set"}, {"id": "data", "required": false, "status": "none", "text": "none installed"}], "has_key": false, "data": null, "mode": {"value": "offline", "set": true}, "unavailable": "", "bulk": {"files": [{"name": "courts", "downloaded": null, "loaded": null, "latest": null, "size": null, "status": "partial 2026-06-30, 1%"}, {"name": "opinions", "downloaded": "2026-03-31", "loaded": "2026-03-31", "latest": "2026-06-30", "size": 50800000000, "status": "update available"}], "complete_set": null, "error": "", "bulk_dir": "/w/bulk", "opinions_db": "/w/bulk/opinions.sqlite", "space": [{"path": "/w/bulk", "need_gb": 135, "free_gb": 9}], "stale": [{"what": "co-citation clusters", "files": ["/w/bulk/cocite.sqlite"], "rebuild": "tribonian cocite prep"}], "swap_pending": false}}"#;

    #[test]
    fn reads_the_report() {
        let r: Report = serde_json::from_str(EMPTY).unwrap();
        assert!(!r.required_ok);
        assert_eq!(r.row("latex").unwrap().status, "fail");
        assert_eq!(r.bulk.files[1].size, Some(50_800_000_000));
        assert_eq!(gb(r.bulk.files[1].size), "50.8 GB");
        let ui = SyncUi { result: Some(Ok(r)), ..Default::default() };
        assert_eq!(ui.actions_required(), Some(2));
    }
}
