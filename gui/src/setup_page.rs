//! "Setup guide": the tricky part of a first install as steps. `tribonian setup --json` decides each step and its
//! actions (the checks are check.py's, so the command line and the app agree); this page draws them and runs them.
//! The app writes nothing itself: settings go through `tribonian setup set` (the value on stdin, so a token stays
//! out of the process list and the log), everything long through the build queue.

use crate::config::Py;
use crate::model::Page;
use crate::{App, Queued, QueuedJob};
use eframe::egui::{self, Color32, RichText, Ui};
use serde::Deserialize;
use std::collections::HashMap;
use std::io::Write;
use std::process::Stdio;
use std::sync::mpsc::{channel, Receiver};

/// Build-queue key for setup commands.
pub const SETUP_KEY: i64 = -3;

#[derive(Clone, Debug, Deserialize)]
pub struct Line {
    pub status: String,
    pub text: String,
    #[serde(default)]
    pub fix: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Action {
    Run {
        label: String,
        commands: Vec<Vec<String>>,
        #[serde(default)]
        tip: String,
    },
    Copy { label: String, text: String },
    Link { label: String, url: String },
    Input { key: String, label: String, hint: String, secret: bool, value: String, placeholder: String },
    Set { key: String, value: String, label: String },
}

#[derive(Clone, Debug, Deserialize)]
pub struct Step {
    pub id: String,
    pub title: String,
    pub why: String,
    pub done: bool,
    pub lines: Vec<Line>,
    pub actions: Vec<Action>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Report {
    pub actions_required: usize,
    pub config_file: String,
    pub steps: Vec<Step>,
}

#[derive(Default)]
pub struct SetupUi {
    pub result: Option<Result<Report, String>>,
    rx: Option<Receiver<Result<Report, String>>>,
    started: bool,
    was_busy: bool,
    /// What's typed in each input, by setting; an entry exists once the field has been shown.
    drafts: HashMap<String, String>,
    saving: Option<Receiver<Result<String, String>>>,
    message: Option<Result<String, String>>,
}

impl SetupUi {
    /// The start screen's count: None until the first report arrives, or when it failed.
    pub fn actions_required(&self) -> Option<usize> {
        match &self.result {
            Some(Ok(r)) => Some(r.actions_required),
            _ => None,
        }
    }
}

fn refresh(app: &mut App) {
    app.setup_ui.started = true;
    let Some(cfg) = &app.cfg else { return };
    let py = cfg.py();
    let ctx = app.egui_ctx.clone();
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let r = match py.cmd().args(["setup", "--json"]).output() {
            Err(e) => Err(format!("could not run {} -m tribonian setup: {e}", py.python)),
            Ok(o) => serde_json::from_slice::<Report>(&o.stdout)
                .map_err(|e| format!("tribonian setup gave no report ({e}):\n{}", String::from_utf8_lossy(&o.stderr))),
        };
        let _ = tx.send(r);
        if let Some(c) = &ctx {
            c.request_repaint();
        }
    });
    app.setup_ui.rx = Some(rx);
}

fn save(py: Py, key: String, value: String, ctx: Option<egui::Context>) -> Receiver<Result<String, String>> {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let run = || -> Result<String, String> {
            let mut child = py
                .cmd()
                .args(["setup", "set", &key])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .map_err(|e| e.to_string())?;
            if let Some(mut i) = child.stdin.take() {
                i.write_all(value.as_bytes()).map_err(|e| e.to_string())?;
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

/// Keep the report current: the first time, and whenever the build queue empties. Called by the start
/// screen (for its count) and this page.
pub fn tick(app: &mut App) {
    if let Some(rx) = &app.setup_ui.rx {
        if let Ok(r) = rx.try_recv() {
            app.setup_ui.result = Some(r);
            app.setup_ui.rx = None;
        }
    }
    if let Some(rx) = &app.setup_ui.saving {
        if let Ok(r) = rx.try_recv() {
            app.setup_ui.message = Some(r);
            app.setup_ui.saving = None;
            refresh(app);
        }
    }
    let busy = app.job.is_some() || !app.queue.is_empty();
    let ended = app.setup_ui.was_busy && !busy;
    app.setup_ui.was_busy = busy;
    if !app.setup_ui.started || (ended && app.setup_ui.rx.is_none()) {
        refresh(app);
    }
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

/// What a click asks for; done after the report is no longer borrowed.
enum Do {
    Queue(String, Vec<Vec<String>>),
    Save(String, String),
}

fn action(ui: &mut Ui, a: &Action, drafts: &mut HashMap<String, String>, queued: bool, saving: bool, todo: &mut Option<Do>) {
    match a {
        Action::Run { label, commands, tip } => {
            let hover = commands.iter().map(|c| format!("tribonian {}", c.join(" "))).collect::<Vec<_>>().join("\n");
            let hover = if tip.is_empty() { hover } else { format!("{tip}\n{hover}") };
            let b = ui.add_enabled(!queued, egui::Button::new(RichText::new(label).strong())).on_hover_text(&hover).on_disabled_hover_text("in the build queue");
            if b.clicked() {
                *todo = Some(Do::Queue(label.clone(), commands.clone()));
            }
        }
        Action::Copy { label, text } => {
            ui.horizontal_wrapped(|ui| {
                ui.label(label);
                ui.label(RichText::new(text).monospace());
                if ui.small_button("Copy").clicked() {
                    ui.ctx().copy_text(text.clone());
                }
            });
        }
        Action::Link { label, url } => {
            ui.hyperlink_to(label, url);
        }
        Action::Input { key, label, hint, secret, value, placeholder } => {
            let d = drafts.entry(key.clone()).or_insert_with(|| value.clone());
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(label).strong());
                ui.add(egui::TextEdit::singleline(d).password(*secret).hint_text(placeholder.as_str()).desired_width(420.0));
                let same = d.trim() == value.trim();
                if ui.add_enabled(!saving && !same, egui::Button::new("Save")).clicked() {
                    *todo = Some(Do::Save(key.clone(), d.trim().to_string()));
                }
                ui.weak(hint);
            });
        }
        Action::Set { key, value, label } => {
            if ui.add_enabled(!saving, egui::Button::new(label)).clicked() {
                *todo = Some(Do::Save(key.clone(), value.clone()));
            }
        }
    }
}

pub fn setup_page(app: &mut App, ui: &mut Ui) {
    tick(app);
    ui.heading("Setup guide");
    ui.weak("The tricky part of a first install, one step at a time. The same steps as tribonian setup on the command line.");
    ui.horizontal(|ui| {
        if ui.button("Check setup").on_hover_text("every check in detail").clicked() {
            app.goto = Some(Page::Check);
        }
        if ui.add_enabled(app.setup_ui.rx.is_none(), egui::Button::new("Run again")).clicked() {
            refresh(app);
        }
        if app.setup_ui.rx.is_some() {
            ui.spinner();
        }
    });
    if let Some(m) = &app.setup_ui.message {
        match m {
            Ok(t) => ui.colored_label(Color32::from_rgb(90, 170, 100), t),
            Err(t) => ui.colored_label(Color32::LIGHT_RED, t),
        };
    }
    if let Some(e) = &app.cfg_err {
        ui.colored_label(Color32::LIGHT_RED, e);
    }
    ui.separator();
    let queued = app.is_queued(SETUP_KEY);
    let saving = app.setup_ui.saving.is_some();
    let result = app.setup_ui.result.clone();
    let mut todo = None;
    let drafts = &mut app.setup_ui.drafts;
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| match &result {
        None => {
            ui.spinner();
        }
        Some(Err(e)) => {
            ui.colored_label(Color32::LIGHT_RED, e);
            ui.label("The scripts didn't run. From the checkout, in a terminal:");
            ui.label(RichText::new(if cfg!(windows) { ".\\bootstrap.ps1" } else { "./bootstrap.sh" }).monospace());
        }
        Some(Ok(rep)) => {
            let next = rep.steps.iter().position(|s| !s.done);
            for (i, s) in rep.steps.iter().enumerate() {
                let (tag, color) = if s.done { ("done", Color32::from_rgb(90, 170, 100)) } else { ("action required", Color32::from_rgb(220, 170, 50)) };
                let title = RichText::new(format!("{}. {} ({tag})", i + 1, s.title)).size(16.0).strong().color(color);
                egui::CollapsingHeader::new(title).id_salt(&s.id).default_open(Some(i) == next).show(ui, |ui| {
                    ui.label(&s.why);
                    for l in &s.lines {
                        ui.horizontal_wrapped(|ui| {
                            mark(ui, &l.status);
                            ui.label(&l.text);
                        });
                        if !l.fix.is_empty() {
                            ui.horizontal_wrapped(|ui| {
                                ui.add_space(44.0);
                                ui.label(RichText::new(format!("fix: {}", l.fix)).monospace().weak());
                            });
                        }
                    }
                    ui.add_space(4.0);
                    for a in &s.actions {
                        action(ui, a, drafts, queued, saving, &mut todo);
                        ui.add_space(4.0);
                    }
                });
                ui.add_space(6.0);
            }
            ui.add_space(8.0);
            if next.is_none() {
                ui.label("All set. For the US Code, citation maps and first cases, see Getting started in the README.");
            }
            ui.weak(format!("config file: {}", rep.config_file));
        }
    });
    match todo {
        Some(Do::Queue(label, commands)) => {
            let steps = commands.into_iter().map(|c| (c, None)).collect();
            app.enqueue(Queued { r#ref: SETUP_KEY, title: format!("Setup: {label}"), job: QueuedJob::Steps(steps) });
        }
        Some(Do::Save(key, value)) => {
            if let Some(cfg) = &app.cfg {
                app.setup_ui.drafts.remove(&key);
                app.setup_ui.saving = Some(save(cfg.py(), key, value, app.egui_ctx.clone()));
            }
        }
        None => {}
    }
}
