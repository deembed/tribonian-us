//! "Sub-Cluster Search (draft)": paste a draft (a brief, a paragraph, a question), and see which
//! cases CourtListener's semantic search finds for each paragraph, re-ranked by the semantic score
//! and grouped by the co-citation sub-cluster each case sits in.
//!
//! The search is the repo's `casemap.py semsearch` (semsearch.py), queued like any other script:
//! the draft goes to it as the query argument (blank lines separate paragraphs), the output goes to
//! cases/semsearch/gui-<time>/results.json, and this page reads that file. The app writes nothing;
//! the API token, rate limits and the response cache all stay in the Python. A rerun of the same
//! text costs no API requests (the script's cache is keyed by paragraph, court, window and page).

use crate::model::Page;
use crate::App;
use eframe::egui::{self, Color32, RichText, Ui};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// Linux refuses a single argument over 128 KiB; stay well under it.
const MAX_TEXT: usize = 100_000;

#[derive(Clone, Debug, Deserialize)]
pub struct Hit {
    pub rank: i64,
    pub cluster_id: i64,
    pub case_name: String,
    #[serde(default)]
    pub date: String,
    #[serde(default)]
    pub court: String,
    #[serde(default)]
    pub cite_count: i64,
    pub semantic: f64,
    #[serde(default)]
    pub window: String,
    pub server_rank: Option<i64>,
    pub sub_name: Option<String>,
    pub cluster_name: Option<String>,
    pub area_name: Option<String>,
    pub slice_start: Option<i64>,
    pub cl_code: Option<i64>,
    pub sub_code: Option<i64>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Group {
    pub sub: Option<String>,
    pub name: Option<String>,
    pub area_name: Option<String>,
    pub slice_start: Option<i64>,
    pub cl_code: Option<i64>,
    pub sub_code: Option<i64>,
    pub hits: i64,
    pub score_sum: f64,
    pub best_rank: i64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Paragraph {
    pub paragraph: i64,
    pub text: String,
    pub hits: Vec<Hit>,
    pub by_subcluster: Vec<Group>,
    pub note: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Requests {
    pub fetched: i64,
    pub cached: i64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Results {
    pub searched: String,
    pub court: Option<String>,
    pub pages: i64,
    pub windows: Vec<serde_json::Value>,
    pub requests: Requests,
    pub paragraphs: Vec<Paragraph>,
}

/// One earlier run, for the side panel: its folder, when, and the start of its first paragraph.
pub struct Past {
    pub dir: PathBuf,
    pub searched: String,
    pub first: String,
    pub n: usize,
}

pub struct SemUi {
    pub text: String,
    pub court: String,
    pub since: String,
    pub pages: u32,
    pub top: usize,
    /// Since has been pre-filled with the newest slice (once; the user's edits are kept after that)
    pub since_set: bool,
    /// Folder of the run shown (or being waited for), and the results.json mtime last read.
    pub dir: Option<PathBuf>,
    pub seen: Option<SystemTime>,
    pub results: Option<Result<Results, String>>,
    pub past: Option<Vec<Past>>,
}

impl Default for SemUi {
    fn default() -> Self {
        SemUi { text: String::new(), court: String::new(), since: String::new(), pages: 2, top: 25, since_set: false,
                dir: None, seen: None, results: None, past: None }
    }
}

fn read_results(dir: &Path) -> Result<Results, String> {
    let text = std::fs::read_to_string(dir.join("results.json")).map_err(|e| e.to_string())?;
    serde_json::from_str(&text).map_err(|e| format!("results.json: {e}"))
}

/// Every cases/semsearch/*/results.json, newest first (CLI runs too, not only the GUI's).
fn scan_past(cases: &Path) -> Vec<Past> {
    let mut v: Vec<(SystemTime, Past)> = Vec::new();
    let Ok(rd) = std::fs::read_dir(cases.join("semsearch")) else { return vec![] };
    for e in rd.flatten() {
        let f = e.path().join("results.json");
        let Ok(m) = f.metadata().and_then(|m| m.modified()) else { continue };
        if let Ok(r) = read_results(&e.path()) {
            let first = r.paragraphs.first().map(|p| p.text.chars().take(70).collect()).unwrap_or_default();
            v.push((m, Past { dir: e.path(), searched: r.searched, first, n: r.paragraphs.len() }));
        }
    }
    v.sort_by_key(|x| std::cmp::Reverse(x.0));
    v.into_iter().map(|x| x.1).take(30).collect()
}

/// The co-citation view's tag as cocite.py names it: cocite-<tag>.sqlite, or "all" for cocite.sqlite.
fn view_tag(stem: &str) -> String {
    stem.strip_prefix("cocite-").unwrap_or("all").to_string()
}

fn paragraphs_in(text: &str) -> usize {
    let n = text.split("\n\n").filter(|p| !p.trim().is_empty()).count();
    n.max(1)
}

/// Load the shown run's results when its file appears or changes (one stat per frame).
fn refresh(app: &mut App) {
    let Some(dir) = app.sem_ui.dir.clone() else { return };
    let m = dir.join("results.json").metadata().and_then(|m| m.modified()).ok();
    if m.is_some() && m != app.sem_ui.seen {
        app.sem_ui.seen = m;
        app.sem_ui.results = Some(read_results(&dir));
        app.sem_ui.past = None;
    }
}

pub fn sem_side(app: &mut App, ui: &mut Ui) {
    ui.heading("Sub-Cluster Search");
    ui.weak("Earlier searches (this page's and casemap.py semsearch's)");
    ui.separator();
    let Some(cfg) = app.cfg.as_ref() else { return };
    let cases = cfg.cases_dir();
    if app.sem_ui.past.is_none() {
        app.sem_ui.past = Some(scan_past(&cases));
    }
    let mut pick = None;
    egui::ScrollArea::vertical().id_salt("sem_past").auto_shrink([false, false]).show(ui, |ui| {
        for p in app.sem_ui.past.as_deref().unwrap_or(&[]) {
            let here = app.sem_ui.dir.as_deref() == Some(p.dir.as_path());
            let t = format!("{} · {} ¶", p.searched, p.n);
            let t = if here { RichText::new(t).strong() } else { RichText::new(t) };
            if ui.link(t).on_hover_text(p.dir.display().to_string()).clicked() {
                pick = Some(p.dir.clone());
            }
            ui.weak(&p.first);
            ui.add_space(4.0);
        }
    });
    if let Some(d) = pick {
        app.sem_ui.dir = Some(d);
        app.sem_ui.seen = None;
    }
}

pub fn sem_page(app: &mut App, ui: &mut Ui) {
    refresh(app);
    ui.heading("Sub-Cluster Search (draft)");
    ui.weak(
        "Paste a draft or a question; blank lines separate paragraphs, and each paragraph is one search. \
         CourtListener's semantic search runs once per five-year slice (pages of 20 each), the hits are re-ranked by \
         their semantic score, and each case is placed in its co-citation sub-cluster.",
    );
    ui.add_space(4.0);
    egui::ScrollArea::vertical().id_salt("sem_input").max_height(180.0).show(ui, |ui| {
        ui.add(egui::TextEdit::multiline(&mut app.sem_ui.text).desired_rows(6).desired_width(f32::INFINITY)
            .hint_text("e.g. an agency that reverses a rule must explain why, and cannot ignore an obvious alternative"));
    });
    // Since starts at the newest slice (one window, the script's default); an earlier year widens the search
    if !app.sem_ui.since_set {
        if let Some(last) = app.meta.as_ref().and_then(|m| m.slices.last()) {
            app.sem_ui.since = last.slice.to_string();
            app.sem_ui.since_set = true;
        }
    }
    let paras = paragraphs_in(&app.sem_ui.text);
    let since: Option<i64> = app.sem_ui.since.trim().parse().ok();
    // windows the script will use: the slices ending in or after Since; empty = the newest slice only
    let windows = app.meta.as_ref().map(|m| match since {
        Some(y) => m.slices.iter().filter(|s| s.slice + 4 >= y).count(),
        None => m.slices.len().min(1),
    }).unwrap_or(0);
    let most = paras * windows * app.sem_ui.pages as usize;
    let mut run = false;
    ui.horizontal(|ui| {
        ui.label("Court");
        ui.add(egui::TextEdit::singleline(&mut app.sem_ui.court).hint_text("all; or scotus, ca4 …").desired_width(110.0))
            .on_hover_text("CourtListener court ids, space-separated (scotus, ca1 … ca11, cadc, cafc)");
        ui.label("Since");
        ui.add(egui::TextEdit::singleline(&mut app.sem_ui.since).hint_text("newest").desired_width(50.0))
            .on_hover_text("search every five-year slice ending in or after this year. Starts at the newest slice \
                            (one window); an earlier year widens the search and costs more API requests. Empty = newest slice");
        ui.label("Pages");
        ui.add(egui::DragValue::new(&mut app.sem_ui.pages).range(1..=5))
            .on_hover_text("pages of 20 results per slice (default 2)");
        let empty = app.sem_ui.text.trim().is_empty();
        let too_big = app.sem_ui.text.len() > MAX_TEXT;
        run = ui.add_enabled(!empty && !too_big && app.cfg.is_some(), egui::Button::new("Search")).clicked();
        if too_big {
            ui.colored_label(Color32::LIGHT_RED, format!("over {} characters: run casemap.py semsearch --file instead", MAX_TEXT));
        }
    });
    if windows == 0 {
        ui.weak(format!("no co-citation view open: up to {} API requests per paragraph for the newest slice; \
                         more with an earlier Since", app.sem_ui.pages));
    } else {
        ui.weak(format!(
            "{paras} paragraph(s) × {windows} slice(s) × {} page(s): up to {most} API requests, fewer when cached \
             (free tier: 125 a day; a run over the day's limit stops, and Search again resumes it)",
            app.sem_ui.pages
        ));
    }
    if run {
        start(app);
    }
    ui.separator();
    show_results(app, ui);
}

/// Queue the script; this page then waits for its results.json.
fn start(app: &mut App) {
    let Some(cfg) = app.cfg.as_ref() else { return };
    let stamp = chrono_like_stamp();
    let dir = cfg.cases_dir().join("semsearch").join(format!("gui-{stamp}"));
    let rel = format!("cases/semsearch/gui-{stamp}");
    let mut args: Vec<String> = vec!["casemap.py".into(), "semsearch".into(), "--out".into(), rel, "--pages".into(), app.sem_ui.pages.to_string()];
    if !app.sem_ui.court.trim().is_empty() {
        args.extend(["--court".into(), app.sem_ui.court.trim().to_string()]);
    }
    if let Ok(y) = app.sem_ui.since.trim().parse::<i64>() {
        args.extend(["--since".into(), y.to_string()]);
    }
    if let Some(m) = &app.meta {
        args.extend(["--courts".into(), view_tag(&m.stem)]); // join against the view this window shows
    }
    // "--" so a draft starting with "-" is still the query, not an option
    args.extend(["--".into(), app.sem_ui.text.clone()]);
    let first: String = app.sem_ui.text.split_whitespace().take(6).collect::<Vec<_>>().join(" ");
    app.enqueue_script(0, format!("Semantic search: {first}…"), args);
    app.sem_ui.dir = Some(dir);
    app.sem_ui.seen = None;
    app.sem_ui.results = None;
}

/// YYYYmmdd-HHMMSS in UTC, without a date crate.
fn chrono_like_stamp() -> String {
    let secs = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let (days, rem) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    // civil-from-days (Howard Hinnant's algorithm): day count since 1970-01-01 -> year, month, day
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    format!("{y:04}{m:02}{d:02}-{:02}{:02}{:02}", rem / 3600, rem % 3600 / 60, rem % 60)
}

fn unit_page(slice: Option<i64>, cl: Option<i64>, sub: Option<i64>) -> Option<Page> {
    let s = slice?;
    match sub {
        Some(k) if k % 1000 != 0 => Some(Page::Sub { slice: s, sub: k }),
        _ => cl.map(|c| Page::Cluster { slice: s, cl: c }),
    }
}

fn show_results(app: &mut App, ui: &mut Ui) {
    let r = match &app.sem_ui.results {
        None if app.sem_ui.dir.is_some() => {
            ui.weak("Waiting for casemap.py semsearch… (its output is in the log panel)");
            return;
        }
        None => return,
        Some(Err(e)) => {
            ui.colored_label(Color32::LIGHT_RED, e);
            return;
        }
        Some(Ok(r)) => r.clone(),
    };
    ui.horizontal(|ui| {
        ui.weak(format!(
            "searched {} · court {} · {} window(s) × {} page(s) · {} request(s), {} from cache",
            r.searched,
            r.court.as_deref().unwrap_or("all"),
            r.windows.len(),
            r.pages,
            r.requests.fetched,
            r.requests.cached
        ));
        ui.label("show");
        ui.add(egui::DragValue::new(&mut app.sem_ui.top).range(5..=500)).on_hover_text("hits shown per paragraph");
    });
    let top = app.sem_ui.top;
    let mut go = None;
    egui::ScrollArea::vertical().id_salt("sem_results").auto_shrink([false, false]).show(ui, |ui| {
        for p in &r.paragraphs {
            let head: String = p.text.chars().take(110).collect();
            egui::CollapsingHeader::new(format!("¶{}  {}{}  ({} cases)", p.paragraph, head, if p.text.len() > 110 { "…" } else { "" }, p.hits.len()))
                .id_salt(("sem_p", p.paragraph))
                .default_open(p.paragraph == 1)
                .show(ui, |ui| {
                    if let Some(n) = &p.note {
                        ui.colored_label(Color32::YELLOW, n);
                    }
                    ui.label(RichText::new("By sub-cluster").strong());
                    egui::Grid::new(("sem_g", p.paragraph)).striped(true).num_columns(4).show(ui, |ui| {
                        ui.weak("hits");
                        ui.weak("best");
                        ui.weak("sub-cluster");
                        ui.weak("area");
                        ui.end_row();
                        for g in p.by_subcluster.iter().take(15) {
                            ui.label(g.hits.to_string()).on_hover_text(format!("sum of semantic scores {:.2}", g.score_sum));
                            ui.label(format!("#{}", g.best_rank));
                            let name = format!("{} {}", g.sub.as_deref().unwrap_or(""), g.name.as_deref().unwrap_or("?"));
                            match unit_page(g.slice_start, g.cl_code, g.sub_code) {
                                Some(pg) => {
                                    if ui.link(name).clicked() {
                                        go = Some(pg);
                                    }
                                }
                                None => {
                                    ui.label(name);
                                }
                            }
                            ui.label(g.area_name.as_deref().unwrap_or(""));
                            ui.end_row();
                        }
                    });
                    ui.add_space(6.0);
                    ui.label(RichText::new("Cases, by semantic score").strong());
                    egui::Grid::new(("sem_h", p.paragraph)).striped(true).num_columns(7).show(ui, |ui| {
                        for h in ["#", "score", "filed", "case", "court", "cited", "sub-cluster"] {
                            ui.weak(h);
                        }
                        ui.end_row();
                        for h in p.hits.iter().take(top) {
                            ui.label(h.rank.to_string()).on_hover_text(format!(
                                "server's rank in window {}: {}",
                                h.window,
                                h.server_rank.map(|x| x.to_string()).unwrap_or("?".into())
                            ));
                            ui.label(format!("{:.3}", h.semantic));
                            ui.label(&h.date);
                            let name: String = h.case_name.chars().take(80).collect();
                            if ui.link(name).on_hover_text(&h.case_name).clicked() {
                                app.api_names.insert(h.cluster_id, h.case_name.clone());
                                go = Some(Page::Case(h.cluster_id));
                            }
                            ui.label(&h.court);
                            ui.label(h.cite_count.to_string());
                            let unit = h.sub_name.as_deref().or(h.cluster_name.as_deref()).unwrap_or("");
                            match unit_page(h.slice_start, h.cl_code, h.sub_code) {
                                Some(pg) if !unit.is_empty() => {
                                    let r = ui.link(unit);
                                    let r = match &h.area_name {
                                        Some(a) => r.on_hover_text(format!("area: {a}")),
                                        None => r,
                                    };
                                    if r.clicked() {
                                        go = Some(pg);
                                    }
                                }
                                _ => {
                                    ui.weak("not in a slice");
                                }
                            }
                            ui.end_row();
                        }
                    });
                });
        }
    });
    if go.is_some() {
        app.goto = go;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The shape semsearch.py writes (one hit placed in a sub-cluster, one not in any slice).
    #[test]
    fn parses_script_output() {
        let j = r#"{"searched": "2026-10-24 22:00", "source": null, "court": "scotus", "pages": 2,
          "windows": [{"after": "1983-01-01", "before": "1983-12-31", "label": "1983-01-01..1983-12-31"}],
          "requests": {"fetched": 0, "cached": 2},
          "paragraphs": [{"paragraph": 1, "text": "q", "raw_hits": 2,
            "hits": [{"cluster_id": 110991, "case_name": "Motor Vehicle Mfrs.", "date": "1983-06-24", "court": "scotus",
                      "citation": "463 U.S. 29", "cite_count": 8635, "semantic": 7.4258, "bm25": 2.57, "window": "w",
                      "server_rank": 1, "also": [], "rank": 1, "slice": "2015-19", "cluster": "2015-19:4",
                      "cluster_name": "Agency review", "sub": "2015-19:4.2", "sub_name": "Arbitrary and capricious",
                      "sub_lineage": 77, "area": 3, "area_name": "Administrative law", "slice_start": 2015,
                      "cl_code": 4, "sub_code": 4002},
                     {"cluster_id": 1, "case_name": "X v. Y", "date": "1983-01-01", "court": "scotus", "citation": null,
                      "cite_count": 0, "semantic": 6.1, "bm25": null, "window": "w", "server_rank": 30, "also": [],
                      "rank": 2, "slice": null, "cluster": null, "cluster_name": null, "sub": null, "sub_name": null,
                      "sub_lineage": null, "area": null, "area_name": null, "slice_start": null, "cl_code": null,
                      "sub_code": null}],
            "by_subcluster": [{"sub": "2015-19:4.2", "name": "Arbitrary and capricious", "area_name": "Administrative law",
                      "slice_start": 2015, "cl_code": 4, "sub_code": 4002, "hits": 1, "score_sum": 7.4258,
                      "best_rank": 1, "cases": [110991]}]}]}"#;
        let r: Results = serde_json::from_str(j).unwrap();
        let p = &r.paragraphs[0];
        assert_eq!(p.hits[0].cluster_id, 110991);
        assert_eq!(unit_page(p.hits[0].slice_start, p.hits[0].cl_code, p.hits[0].sub_code), Some(Page::Sub { slice: 2015, sub: 4002 }));
        assert_eq!(unit_page(p.hits[1].slice_start, p.hits[1].cl_code, p.hits[1].sub_code), None);
        assert_eq!(unit_page(Some(2015), Some(4), Some(4000)), Some(Page::Cluster { slice: 2015, cl: 4 }));
    }

    #[test]
    fn stamp_and_tags() {
        assert_eq!(chrono_like_stamp().len(), 15);
        assert_eq!(view_tag("cocite-appellate-sentence-m0.2"), "appellate-sentence-m0.2");
        assert_eq!(view_tag("cocite"), "all");
        assert_eq!(paragraphs_in("a\n\n\nb\n\n"), 2);
    }
}
