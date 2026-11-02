//! Case History: one federal case across its courts. `tribonian history` walks the dockets and saves the chain to
//! cases/history/*.json with a "view": lanes and rows, the same ones its terminal graph draws. This page only draws
//! them; every change goes through the script. One column per docket, time running down.

use crate::cases::open_external;
use crate::model::Page;
use crate::{App, Queued, QueuedJob};
use eframe::egui::{self, Color32, CornerRadius, FontId, Pos2, RichText, Sense, Shape, Stroke, Ui, Vec2, pos2, vec2};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::time::{Instant, SystemTime};

#[derive(Clone, Deserialize)]
pub struct Lane {
    pub court: String,
    pub number: String,
    pub label: String,
    #[serde(default)]
    pub candidate: bool,
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub name: String,
}

#[derive(Clone, Deserialize)]
pub struct Row {
    pub date: String,
    #[serde(rename = "type")]
    pub typ: String,
    #[serde(default)]
    pub at: usize,
    #[serde(default)]
    pub from: usize,
    #[serde(default)]
    pub to: usize,
    #[serde(default)]
    pub key: bool,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub full: String,
    #[serde(default)]
    pub how: String,
    #[serde(default)]
    pub weak: bool,
    #[serde(default)]
    pub vacated: bool,
}

#[derive(Clone, Deserialize)]
pub struct View {
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub notes: Vec<String>,
    pub lanes: Vec<Lane>,
    pub rows: Vec<Row>,
}

#[derive(Deserialize)]
struct SavedFile {
    seed: (String, String),
    view: Option<View>,
}

#[derive(Clone)]
pub struct Saved {
    pub path: PathBuf,
    pub stamp: SystemTime,
    pub seed: (String, String),
    pub view: Option<View>,
}

impl Saved {
    /// Does this chain hold the docket?
    fn holds(&self, court: &str, number: &str) -> bool {
        self.seed.0 == court && self.seed.1 == number
            || self.view.as_ref().is_some_and(|v| v.lanes.iter().any(|l| l.court == court && l.number == number))
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Picked {
    Row(usize),
    Lane(usize),
}

#[derive(Default)]
pub struct HistoryUi {
    pub court: String,
    pub number: String,
    pub err: Option<String>,
    all: bool,
    picked: Option<Picked>,
    saved: Vec<Saved>,
    scanned: Option<Instant>,
}

/// The page's key: "court number", which is how a docket is named on the command line.
pub fn page_for(court: &str, number: &str) -> Page {
    Page::CaseHistory(format!("{court} {number}"))
}

fn split_key(key: &str) -> Option<(&str, &str)> {
    key.split_once(' ').filter(|(c, n)| !c.is_empty() && !n.is_empty())
}

pub fn title(key: &str) -> String {
    match split_key(key) {
        Some((c, n)) => format!("History: {c} {n}"),
        None => "Case History".into(),
    }
}

/// The saved chains, reread when a file changes (at most every two seconds; a build may finish at any time).
fn scan(h: &mut HistoryUi, dir: &Path) {
    if h.scanned.is_some_and(|t| t.elapsed().as_secs() < 2) {
        return;
    }
    h.scanned = Some(Instant::now());
    let mut out = Vec::new();
    for e in std::fs::read_dir(dir).into_iter().flatten().flatten() {
        let path = e.path();
        if path.extension().is_none_or(|x| x != "json") {
            continue;
        }
        let Ok(stamp) = e.metadata().and_then(|m| m.modified()) else { continue };
        if let Some(old) = h.saved.iter().find(|s| s.path == path && s.stamp == stamp) {
            out.push(old.clone());
        } else if let Some(s) = std::fs::read_to_string(&path).ok().and_then(|t| read_saved(&t, &path, stamp)) {
            out.push(s);
        }
    }
    out.sort_by_key(|s| std::cmp::Reverse(s.stamp));
    h.saved = out;
}

fn read_saved(text: &str, path: &Path, stamp: SystemTime) -> Option<Saved> {
    let f: SavedFile = serde_json::from_str(text).ok()?;
    Some(Saved { path: path.to_path_buf(), stamp, seed: f.seed, view: f.view })
}

fn job_key(court: &str, number: &str) -> i64 {
    crate::dockets::add_key(court, &format!("history {number}"))
}

fn queue_build(app: &mut App, court: &str, number: &str, refresh: bool) {
    let mut args: Vec<String> = ["history", "--court", court, "--number", number].iter().map(|s| s.to_string()).collect();
    if refresh {
        args.push("--refresh".into());
    }
    app.enqueue(Queued {
        r#ref: job_key(court, number),
        title: format!("{court} {number}: case history"),
        job: QueuedJob::Steps(vec![(args, Some("saved ".to_string()))]),
    });
}

pub fn history_page(app: &mut App, ui: &mut Ui, key: &str) {
    if let Some(dir) = app.cfg.as_ref().map(|c| c.work_dir.join("cases").join("history")) {
        scan(&mut app.history_ui, &dir);
    }
    ui.ctx().request_repaint_after(std::time::Duration::from_secs(2));
    match split_key(key) {
        Some((court, number)) => chain_page(app, ui, court, number),
        None => index(app, ui),
    }
}

fn index(app: &mut App, ui: &mut Ui) {
    ui.heading("Case History");
    ui.label("One federal case across its courts: its dockets, appeals and remands. Start from any docket in the chain.");
    ui.add_space(6.0);
    ui.horizontal(|ui| {
        // the Supreme Court first, then the courts of appeals and the rest; its usual starting point is the default
        let courts: Vec<crate::dockets::Court> = app
            .courts
            .as_ref()
            .ok()
            .and_then(|t| t.as_ref())
            .map(|t| t.federal.supreme.iter().chain(t.recap_courts()).cloned().collect())
            .unwrap_or_default();
        if courts.is_empty() {
            ui.add(egui::TextEdit::singleline(&mut app.history_ui.court).hint_text("court id, e.g. scotus").desired_width(130.0))
                .on_hover_text("CourtListener's court id: scotus, ca1–ca11, cadc, cafc; district courts like mdd (D. Md.), nysd (S.D.N.Y.)");
        } else {
            if app.history_ui.court.is_empty() {
                app.history_ui.court = courts[0].id.clone();
            }
            let cur = courts.iter().find(|c| c.id == app.history_ui.court).map(|c| if c.cite.is_empty() { c.name.clone() } else { c.cite.clone() });
            egui::ComboBox::from_id_salt("history-court").selected_text(cur.unwrap_or_else(|| app.history_ui.court.clone())).width(130.0).show_ui(ui, |ui| {
                for c in &courts {
                    let label = if c.cite.is_empty() { c.name.clone() } else { format!("{} — {}", c.cite, c.name) };
                    ui.selectable_value(&mut app.history_ui.court, c.id.clone(), label);
                }
            });
        }
        let r = ui.add(egui::TextEdit::singleline(&mut app.history_ui.number).hint_text("case no., e.g. 21-454").desired_width(160.0));
        let enter = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
        if ui.button("Open").on_hover_text("shows the chain if it is built; otherwise offers to build it").clicked() || enter {
            let (c, n) = (app.history_ui.court.trim().to_lowercase(), app.history_ui.number.trim().to_string());
            if c.is_empty() || n.is_empty() {
                app.history_ui.err = Some("Type a court id and its case number, as the court writes it.".into());
            } else {
                app.history_ui.err = None;
                app.goto = Some(page_for(&c, &n));
            }
        }
    });
    if let Some(e) = &app.history_ui.err {
        ui.colored_label(Color32::LIGHT_RED, e);
    }
    ui.add_space(10.0);
    ui.separator();
    ui.strong("Built");
    if app.history_ui.saved.is_empty() {
        ui.weak("None yet.");
    }
    let mut go = None;
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        for s in &app.history_ui.saved {
            let (name, lanes) = s.view.as_ref().map(|v| (v.name.clone(), v.lanes.len())).unwrap_or_default();
            ui.horizontal(|ui| {
                let t = if name.is_empty() { format!("{} {}", s.seed.0, s.seed.1) } else { name };
                if ui.link(t).clicked() {
                    go = Some(page_for(&s.seed.0, &s.seed.1));
                }
                ui.weak(format!("{} {} · {lanes} dockets", s.seed.0, s.seed.1));
            });
        }
    });
    if go.is_some() {
        app.goto = go;
    }
}

fn chain_page(app: &mut App, ui: &mut Ui, court: &str, number: &str) {
    let queued = app.is_queued(job_key(court, number));
    let saved = app.history_ui.saved.iter().find(|s| s.holds(court, number)).cloned();
    let Some(view) = saved.as_ref().and_then(|s| s.view.clone()) else {
        ui.heading(format!("{court} {number}"));
        ui.label("This case's history isn't built yet.");
        ui.weak("It reads each docket in the chain from CourtListener, which takes several requests and can wait on the rate limit.");
        ui.add_space(6.0);
        if ui.add_enabled(!queued, egui::Button::new("Build history")).on_disabled_hover_text("in the build queue").clicked() {
            queue_build(app, court, number, false);
        }
        return;
    };
    let (seed_c, seed_n) = saved.as_ref().map(|s| s.seed.clone()).unwrap_or_default();
    let queued = queued || app.is_queued(job_key(&seed_c, &seed_n));

    ui.heading(if view.name.is_empty() { format!("{court} {number}") } else { view.name.clone() });
    ui.horizontal(|ui| {
        ui.checkbox(&mut app.history_ui.all, "Every filing");
        if ui
            .add_enabled(!queued, egui::Button::new("Update"))
            .on_hover_text(format!("queues tribonian history --court {seed_c} --number {seed_n} --refresh: reads every docket again, which uses CourtListener requests"))
            .on_disabled_hover_text("in the build queue")
            .clicked()
        {
            queue_build(app, &seed_c, &seed_n, true);
        }
        ui.separator();
        ui.weak("solid: docket's fields  ·  dashed: entry text  ·  dotted: inferred  ·  amber: by name  ·  (?) weak match");
    });
    for n in &view.notes {
        ui.colored_label(ui.visuals().warn_fg_color, format!("note: {n}"));
    }
    ui.separator();

    let rows: Vec<&Row> = view.rows.iter().filter(|r| app.history_ui.all || r.key).collect();
    let spans = spans(view.lanes.len(), &rows);
    let picked = app.history_ui.picked.filter(|p| match p {
        Picked::Row(i) => *i < rows.len(),
        Picked::Lane(i) => *i < view.lanes.len(),
    });
    let detail = if picked.is_some() { 170.0 } else { 0.0 };
    let geo = Geometry { row_h: 20.0, date_w: 92.0, lane_w: 26.0, n: view.lanes.len() };
    let mut click = None;
    let total = view.lanes.len() + rows.len();
    let longest = rows.iter().map(|r| r.text.chars().count().min(MAX_TEXT)).max().unwrap_or(0).max(view.lanes.iter().map(|l| l.label.len() + 26).max().unwrap_or(0));
    let width = geo.text_x() + longest as f32 * 7.5 + 20.0;
    // show_rows works out its rows from the spacing it inherits, so it has to be zero here, not only inside
    let spacing = ui.spacing().item_spacing;
    ui.spacing_mut().item_spacing = Vec2::ZERO;
    egui::ScrollArea::both()
        .id_salt("case-history")
        .auto_shrink([false, false])
        .max_height((ui.available_height() - detail).max(120.0))
        .show_rows(ui, geo.row_h, total, |ui, range| {
            for k in range {
                let w = width.max(ui.available_width());
                let (rect, resp) = ui.allocate_exact_size(vec2(w, geo.row_h), Sense::click());
                let me = if k < view.lanes.len() { Picked::Lane(k) } else { Picked::Row(k - view.lanes.len()) };
                if picked == Some(me) {
                    ui.painter().rect_filled(rect, CornerRadius::ZERO, ui.visuals().selection.bg_fill.gamma_multiply(0.35));
                } else if resp.hovered() {
                    ui.painter().rect_filled(rect, CornerRadius::ZERO, ui.visuals().widgets.hovered.bg_fill.gamma_multiply(0.25));
                }
                match me {
                    Picked::Lane(i) => header_row(ui, &geo, rect.min, i, &view.lanes),
                    Picked::Row(i) => data_row(ui, &geo, rect.min, i, rows[i], &view.lanes, &spans),
                }
                if resp.clicked() {
                    click = Some(me);
                }
            }
        });
    ui.spacing_mut().item_spacing = spacing;
    if let Some(c) = click {
        app.history_ui.picked = if picked == Some(c) { None } else { Some(c) };
    }
    if let Some(p) = picked {
        ui.separator();
        details(app, ui, &view, &rows, p);
    }
}

const MAX_TEXT: usize = 160;

struct Geometry {
    row_h: f32,
    date_w: f32,
    lane_w: f32,
    n: usize,
}

impl Geometry {
    fn lane_x(&self, at: Pos2, i: usize) -> f32 {
        at.x + self.date_w + self.lane_w * (i as f32 + 0.5)
    }
    fn text_x(&self) -> f32 {
        self.date_w + self.lane_w * self.n as f32 + 8.0
    }
}

/// How far each lane's line runs: the first and last row shown on it. By row, not date: several rows can share a date.
fn spans(n: usize, rows: &[&Row]) -> Vec<Option<(usize, usize)>> {
    let mut out: Vec<Option<(usize, usize)>> = vec![None; n];
    for (k, r) in rows.iter().enumerate() {
        let ids = if r.typ == "event" { [r.at, r.at] } else { [r.from, r.to] };
        for i in ids {
            if i < n {
                out[i] = Some(out[i].map_or((k, k), |(a, _)| (a, k)));
            }
        }
    }
    out
}

/// A court's colour: the Supreme Court, the courts of appeals, the district courts.
fn court_color(court: &str, dark: bool) -> Color32 {
    let appeals = court == "cafc" || court == "cadc" || (court.len() > 2 && court.starts_with("ca") && court[2..].chars().all(|c| c.is_ascii_digit()));
    match (court == "scotus", appeals, dark) {
        (true, _, true) => Color32::from_rgb(196, 153, 255),
        (true, _, false) => Color32::from_rgb(112, 60, 190),
        (_, true, true) => Color32::from_rgb(110, 170, 255),
        (_, true, false) => Color32::from_rgb(30, 95, 190),
        (_, _, true) => Color32::from_rgb(110, 205, 150),
        (_, _, false) => Color32::from_rgb(25, 125, 80),
    }
}

fn lane_color(ui: &Ui, lanes: &[Lane], i: usize) -> Color32 {
    lanes.get(i).map(|l| court_color(&l.court, ui.visuals().dark_mode)).unwrap_or(Color32::GRAY)
}

/// The header: each docket's name steps right, over its own column.
fn header_row(ui: &Ui, g: &Geometry, at: Pos2, k: usize, lanes: &[Lane]) {
    let p = ui.painter();
    let y = at.y + g.row_h * 0.5;
    for i in 0..k {
        let x = g.lane_x(at, i);
        p.line_segment([pos2(x, at.y), pos2(x, at.y + g.row_h)], Stroke::new(1.0, lane_color(ui, lanes, i).gamma_multiply(0.35)));
    }
    let lane = &lanes[k];
    let color = lane_color(ui, lanes, k);
    let x = g.lane_x(at, k);
    p.circle_filled(pos2(x, y), 3.0, color);
    p.line_segment([pos2(x, y), pos2(x, at.y + g.row_h)], Stroke::new(1.0, color.gamma_multiply(0.35)));
    let suffix = if lane.candidate { " (?) matched by name only" } else { "" };
    p.text(pos2(x + 8.0, y), egui::Align2::LEFT_CENTER, format!("{}{suffix}", lane.label), FontId::proportional(13.0), color);
}

fn data_row(ui: &Ui, g: &Geometry, at: Pos2, k: usize, r: &Row, lanes: &[Lane], spans: &[Option<(usize, usize)>]) {
    let p = ui.painter();
    let y = at.y + g.row_h * 0.5;
    p.text(pos2(at.x + 4.0, y), egui::Align2::LEFT_CENTER, &r.date, FontId::monospace(12.0), ui.visuals().weak_text_color());
    for (i, s) in spans.iter().enumerate() {
        if let Some((a, b)) = s {
            if *a <= k && k <= *b {
                let top = if *a == k { y } else { at.y };
                let bot = if *b == k { y } else { at.y + g.row_h };
                p.line_segment([pos2(g.lane_x(at, i), top), pos2(g.lane_x(at, i), bot)], Stroke::new(1.5, lane_color(ui, lanes, i).gamma_multiply(0.55)));
            }
        }
    }
    if r.typ == "event" {
        let c = lane_color(ui, lanes, r.at);
        let x = g.lane_x(at, r.at);
        if r.vacated {
            p.circle_filled(pos2(x, y), 4.5, ui.visuals().panel_fill);
            p.circle_stroke(pos2(x, y), 4.5, Stroke::new(1.5, c));
        } else if r.key {
            p.circle_filled(pos2(x, y), 4.5, c);
        } else {
            p.circle_filled(pos2(x, y), 2.5, c.gamma_multiply(0.7));
        }
    } else {
        arrow(ui, lanes, g, at, r);
    }
    let color = if r.weak {
        ui.visuals().warn_fg_color
    } else if r.typ == "event" && !r.key {
        ui.visuals().weak_text_color()
    } else {
        ui.visuals().text_color()
    };
    let shown: String = r.text.chars().take(MAX_TEXT).collect();
    p.text(pos2(at.x + g.text_x(), y), egui::Align2::LEFT_CENTER, shown, FontId::proportional(13.0), color);
}

fn arrow(ui: &Ui, lanes: &[Lane], g: &Geometry, at: Pos2, r: &Row) {
    let p = ui.painter();
    let y = at.y + g.row_h * 0.5;
    let (a, b) = (g.lane_x(at, r.from), g.lane_x(at, r.to));
    let color = if r.how == "name" { ui.visuals().warn_fg_color } else { lane_color(ui, lanes, r.from) };
    let dir = if b > a { 1.0 } else { -1.0 };
    let tip = b - dir * 6.0;
    let ends = [pos2(a, y), pos2(tip, y)];
    let stroke = Stroke::new(1.8, color);
    match r.how.as_str() {
        "field" => {
            p.line_segment(ends, stroke);
        }
        "entry" => {
            p.extend(Shape::dashed_line(&ends, stroke, 5.0, 3.0));
        }
        _ => {
            p.extend(Shape::dashed_line(&ends, stroke, 2.0, 3.0));
        }
    }
    p.add(Shape::convex_polygon(vec![pos2(b, y), pos2(tip, y - 4.5), pos2(tip, y + 4.5)], color, Stroke::NONE));
}

fn details(app: &mut App, ui: &mut Ui, view: &View, rows: &[&Row], p: Picked) {
    let lane = match p {
        Picked::Lane(i) => view.lanes.get(i),
        Picked::Row(i) => rows.get(i).and_then(|r| view.lanes.get(if r.typ == "event" { r.at } else { r.to })),
    };
    egui::ScrollArea::vertical().id_salt("history-detail").auto_shrink([false, false]).show(ui, |ui| {
        if let Picked::Row(i) = p {
            if let Some(r) = rows.get(i) {
                ui.label(RichText::new(format!("{}  {}", r.date, r.text)).strong());
                if !r.full.is_empty() && r.full != r.text {
                    ui.label(&r.full);
                }
                if r.typ != "event" {
                    ui.weak(match r.how.as_str() {
                        "field" => "From the docket's originating-court fields.",
                        "entry" => "From the text of a docket entry.",
                        "name" => "Matched by case name and dates only.",
                        _ => "Inferred through another docket.",
                    });
                }
            }
        }
        let Some(l) = lane else { return };
        ui.add_space(4.0);
        ui.horizontal_wrapped(|ui| {
            ui.strong(&l.label);
            if !l.name.is_empty() {
                ui.label(&l.name);
            }
            if !l.url.is_empty() && ui.button("CourtListener").on_hover_text(&l.url).clicked() {
                open_external(&l.url);
            }
            if l.court == "scotus" {
                if ui.button("Open docket").clicked() {
                    app.goto = Some(Page::Docket(l.number.clone()));
                }
            } else if let Some(id) = app.recap.iter().find(|d| d.court == l.court && d.number == l.number).map(|d| d.id) {
                if ui.button("Open docket").clicked() {
                    app.goto = Some(Page::RecapDocket(id));
                }
            }
        });
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(date: &str, at: usize) -> Row {
        Row { date: date.into(), typ: "event".into(), at, from: 0, to: 0, key: true, text: String::new(), full: String::new(), how: String::new(), weak: false, vacated: false }
    }

    #[test]
    fn page_key() {
        assert_eq!(split_key("idd 2:08-cv-00185"), Some(("idd", "2:08-cv-00185")));
        assert_eq!(split_key(""), None);
    }

    #[test]
    fn lane_runs_by_row_when_dates_repeat() {
        let (a, b, c) = (row("2008-10-16", 0), row("2008-10-16", 0), row("2010-01-01", 1));
        assert_eq!(spans(2, &[&a, &b, &c]), vec![Some((0, 1)), Some((2, 2))]);
    }

    #[test]
    fn court_colours_tell_appeals_from_districts() {
        assert_eq!(court_color("ca9", true), court_color("cadc", true));
        assert_ne!(court_color("ca9", true), court_color("cand", true));
        assert_ne!(court_color("scotus", true), court_color("ca9", true));
    }

    #[test]
    fn reads_a_saved_chain_and_finds_any_docket_in_it() {
        let text = r#"{"seed": ["scotus", "21-454"], "view": {"name": "S", "notes": [], "rows": [],
            "lanes": [{"court": "ca9", "number": "19-35469", "label": "9th Cir. 19-35469", "candidate": false}]}, "dockets": []}"#;
        let s = read_saved(text, Path::new("x.json"), SystemTime::UNIX_EPOCH).unwrap();
        assert!(s.holds("scotus", "21-454") && s.holds("ca9", "19-35469") && !s.holds("ca9", "1-1"));
    }
}
