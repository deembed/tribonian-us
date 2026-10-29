//! Drawing: the side panel and every page. Renderers never navigate directly; they set app.goto.

use crate::cases::{obsidian_url, open_external};
use crate::db::MAX_ROWS;
use crate::model::*;
use crate::App;
use eframe::egui::{self, Color32, RichText, Ui};

fn matches(filter: &str, text: &str) -> bool {
    filter.is_empty() || text.to_lowercase().contains(&filter.to_lowercase())
}

fn status_badge(ui: &mut Ui, label: &Option<Label>) {
    if let Some(s) = label.as_ref().and_then(|l| l.status.as_deref()) {
        if s != "stable" {
            ui.label(RichText::new(format!("label {s}")).small().color(Color32::from_rgb(200, 140, 40)))
                .on_hover_text("cocite.py label_status: the label's fingerprint matches this lineage less well after a rebuild");
        }
    }
}

fn year(y: Option<i64>) -> String {
    y.map(|y| y.to_string()).unwrap_or_default()
}

/// A small "note" tag on cases that already have a built note.
fn built_tag(app: &App, ui: &mut Ui, r: i64) {
    if app.files.note_of(r).is_some() {
        ui.label(RichText::new("note").small().color(Color32::from_rgb(110, 170, 110)))
            .on_hover_text("a note and PDF are built for this case");
    } else {
        ui.label("");
    }
}

fn cut_note(ui: &mut Ui, n: usize) {
    if n >= MAX_ROWS {
        ui.weak(format!("Showing the first {MAX_ROWS}."));
    }
}

// ================================================================== side panel

pub fn side_panel(app: &mut App, ui: &mut Ui, section: Section) {
    ui.add_space(6.0);
    match section {
        Section::Clusters => clusters_side(app, ui),
        Section::Cases => cases_side(app, ui),
        Section::Search => search_side(app, ui),
        Section::Scotus => crate::scotus_page::scotus_side(app, ui),
        Section::SemSearch => crate::semsearch_page::sem_side(app, ui),
        Section::Dockets => crate::dockets_page::dockets_side(app, ui),
        Section::Home => {}
    }
}

fn view_selector(app: &mut App, ui: &mut Ui) {
    let current = app.view_sel.map(|i| app.views[i].stem.clone()).unwrap_or_else(|| "(none)".into());
    let mut chosen = None;
    egui::ComboBox::from_label("View")
        .selected_text(current)
        .width(240.0)
        .show_ui(ui, |ui| {
            for (i, v) in app.views.iter().enumerate() {
                let text = if v.ok { v.stem.clone() } else { format!("{} (schema {})", v.stem, v.schema) };
                let r = ui.add_enabled(v.ok, egui::Button::selectable(app.view_sel == Some(i), text));
                if r.clicked() {
                    chosen = Some(i);
                }
            }
        });
    if let Some(i) = chosen {
        if app.view_sel != Some(i) {
            app.view_sel = Some(i);
            app.open_view();
        }
    }
    if let Some(e) = &app.view_err {
        ui.colored_label(Color32::LIGHT_RED, e);
    }
}

fn clusters_side(app: &mut App, ui: &mut Ui) {
    ui.heading("View Clusters");
    view_selector(app, ui);
    ui.separator();
    ui.label("Across time");
    if ui.button("Areas and lineages").clicked() {
        app.goto = Some(Page::Areas);
    }
    ui.add_space(6.0);
    ui.label("One slice");
    if let Some(m) = app.meta.clone() {
        if !m.slices.is_empty() {
            let max = m.slices.len() - 1;
            let labels: Vec<String> = m
                .slices
                .iter()
                .map(|s| if s.partial { format!("{} (partial)", s.label) } else { s.label.clone() })
                .collect();
            let text = labels[app.slice_idx.min(max)].clone();
            let r = ui.add(egui::Slider::new(&mut app.slice_idx, 0..=max).show_value(false).text(text));
            ui.horizontal(|ui| {
                if ui.button("Open slice").clicked() || r.drag_stopped() {
                    app.goto = Some(Page::Slice(m.slices[app.slice_idx.min(max)].slice));
                }
                if ui.small_button("newest full").clicked() {
                    if let Some(i) = m.slices.iter().position(|s| s.slice == m.newest_full) {
                        app.slice_idx = i;
                        app.goto = Some(Page::Slice(m.newest_full));
                    }
                }
            });
        }
    } else if app.view_err.is_none() {
        ui.horizontal(|ui| {
            ui.spinner();
            ui.label("opening view…");
        });
    }
    ui.separator();
    ui.horizontal(|ui| {
        ui.label("Filter");
        ui.text_edit_singleline(&mut app.filter);
    });
    ui.weak("Filters the lists on this page by name.");
}

fn cases_side(app: &mut App, ui: &mut Ui) {
    ui.heading("View Opinions");
    ui.horizontal(|ui| {
        ui.label("Filter");
        ui.text_edit_singleline(&mut app.filter);
    });
    ui.horizontal(|ui| {
        ui.weak(format!("{} notes", app.files.count()));
        if ui.small_button("Reload").clicked() {
            if let Some(cfg) = &app.cfg {
                app.files = crate::cases::CaseFiles::load(cfg);
            }
        }
    });
    if let Some(e) = &app.files.error {
        ui.colored_label(Color32::LIGHT_RED, e);
    }
    ui.separator();
    let filter = app.filter.clone();
    let mut go = None;
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        for (folder, notes) in &app.files.folders {
            let shown: Vec<_> = notes.iter().filter(|n| matches(&filter, &n.title) || matches(&filter, &n.stem)).collect();
            if shown.is_empty() {
                continue;
            }
            let head = if folder.is_empty() { "md-cases/".to_string() } else { format!("{folder}/") };
            egui::CollapsingHeader::new(format!("{head}  ({})", shown.len()))
                .default_open(true)
                .show(ui, |ui| {
                    for n in shown {
                        ui.horizontal(|ui| {
                            match n.r#ref {
                                Some(r) => {
                                    let link = egui::Label::new(RichText::new(&n.title).color(ui.visuals().hyperlink_color))
                                        .truncate()
                                        .sense(egui::Sense::click());
                                    let resp = ui.add(link).on_hover_text(&n.title);
                                    if resp.hovered() {
                                        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                                    }
                                    if resp.clicked() {
                                        go = Some(Page::Case(r));
                                    }
                                }
                                None => match &n.docket {
                                    // scotus.py's docket and filing notes open the docket page
                                    Some(d) => {
                                        let link = egui::Label::new(RichText::new(&n.title).color(ui.visuals().hyperlink_color))
                                            .truncate()
                                            .sense(egui::Sense::click());
                                        let resp = ui.add(link).on_hover_text(format!("{}\nSupreme Court No. {d}: opens the docket page", n.title));
                                        if resp.hovered() {
                                            ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                                        }
                                        if resp.clicked() {
                                            go = Some(Page::Docket(d.clone()));
                                        }
                                    }
                                    None => {
                                        ui.add(egui::Label::new(RichText::new(&n.title).weak()).truncate())
                                            .on_hover_text("not a case in _index.json (a docket, filing or cited-by note)");
                                    }
                                },
                            }
                        });
                    }
                });
        }
    });
    if go.is_some() {
        app.goto = go;
    }
}

// ================================================================== central

pub fn central(app: &mut App, ui: &mut Ui) {
    let page = app.current().clone();
    match &page {
        Page::Home => return home(app, ui),
        Page::Cases => return cases_home(app, ui),
        Page::Search { .. } => return search_page(app, ui, &page),
        Page::Scotus => return crate::scotus_page::scotus_home(app, ui),
        Page::Docket(n) => return crate::scotus_page::docket_page(app, ui, n),
        Page::SemSearch => return crate::semsearch_page::sem_page(app, ui),
        Page::Check => return crate::check_page::check_page(app, ui),
        Page::Dockets => return crate::dockets_page::dockets_home(app, ui),
        Page::DocketsFederal => return crate::dockets_page::federal_page(app, ui),
        Page::DocketsState(code) => return crate::dockets_page::state_page(app, ui, code),
        Page::CourtDockets(id) => return crate::dockets_page::court_page(app, ui, id),
        Page::RecapDocket(id) => return crate::dockets_page::docket_page(app, ui, *id),
        _ => {}
    }
    if let Some(e) = app.errors.get(&page) {
        ui.colored_label(Color32::LIGHT_RED, format!("Could not load this page: {e}"));
        return;
    }
    let Some(data) = app.cache.remove(&page) else {
        if let Some(e) = &app.view_err {
            ui.colored_label(Color32::LIGHT_RED, e);
            return;
        }
        skeleton(ui, &page);
        return;
    };
    egui::ScrollArea::both().auto_shrink([false, false]).show(ui, |ui| {
        ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Extend);
        match &data {
            PageData::Areas(rows) => areas(app, ui, rows),
            PageData::Area { area, name, label, first, last, lineages, core, graph } => {
                area_page(app, ui, *area, name, label, *first, *last, lineages, core, graph)
            }
            PageData::Lineage { lineage, level, name, label, anchor, area, cells, merged_in, split_out } => {
                lineage_page(app, ui, *lineage, *level, name, label, anchor, area, cells, merged_in, split_out)
            }
            PageData::Slice { slice, clusters } => slice_page(app, ui, *slice, clusters),
            PageData::Unit { head, children, cases } => unit_page(app, ui, head, children, cases),
            PageData::Case(c) => case_page(app, ui, c),
            PageData::CoCited(c) => cocited_page(app, ui, c),
        }
    });
    app.cache.insert(page, data);
}

fn home(app: &mut App, ui: &mut Ui) {
    ui.add_space(30.0);
    ui.vertical_centered(|ui| {
        ui.heading(RichText::new("What do you want to do?").size(26.0));
    });
    ui.add_space(24.0);
    let size = egui::vec2(300.0, 48.0);
    let button = |ui: &mut Ui, label: RichText, tip: &str| ui.add_sized(size, egui::Button::new(label)).on_hover_text(tip).clicked();
    let big = |t: &str| RichText::new(t).size(18.0);
    let mut go = None;
    let mut courts = false;
    ui.columns(3, |cols| {
        cols[0].vertical_centered(|ui| {
            ui.heading("Find and read");
            ui.add_space(12.0);
            if button(ui, big("Search for Cases"), "case names and courts' parentheticals in the local database, and CourtListener for newer cases") {
                go = Some(Page::Search { query: String::new(), mode: app.search_mode });
            }
            ui.add_space(8.0);
            if button(ui, big("View Opinions"), "the decided cases built so far: one note and PDF per opinion (tribonian case build)") {
                go = Some(Page::Cases);
            }
            ui.add_space(8.0);
            if button(
                ui,
                big("View Dockets"),
                "Federal or State, then the court: the Supreme Court (tribonian scotus), and dockets fetched from RECAP (tribonian case recap)",
            ) {
                go = Some(Page::Dockets);
            }
        });
        cols[1].vertical_centered(|ui| {
            ui.heading("Analysis");
            ui.add_space(12.0);
            if button(ui, big("View Clusters"), "co-citation areas, lineages, clusters and sub-clusters over time") {
                go = Some(Page::Areas);
            }
            ui.add_space(8.0);
            if button(ui, big("Sub-Cluster Search (draft)"), "paste a draft: CourtListener semantic search per paragraph, grouped by sub-cluster (tribonian map semsearch)") {
                go = Some(Page::SemSearch);
            }
        });
        cols[2].vertical_centered(|ui| {
            ui.heading("Setup");
            ui.add_space(12.0);
            let trouble = app.cfg_err.is_some() || app.view_err.is_some();
            let label = if trouble { big("Check setup (something is missing)").color(Color32::LIGHT_RED) } else { big("Check setup") };
            if button(ui, label, "is each tool and database there, and does it answer a query (tribonian check)") {
                go = Some(Page::Check);
            }
            ui.add_space(8.0);
            let queued = app.is_queued(crate::dockets::COURTS_KEY);
            let have = matches!(app.courts, Ok(Some(_)));
            let r = ui
                .add_enabled(!queued, egui::Button::new(big(if have { "Refresh court list" } else { "Build court list" })).min_size(size))
                .on_hover_text("queues tribonian dockets courts: every court from the local courts table (tribonian db load courts), else CourtListener's API; View Dockets reads it")
                .on_disabled_hover_text("in the build queue");
            courts = r.clicked();
        });
    });
    if courts {
        crate::dockets_page::queue_courts(app);
    }
    if go.is_some() {
        app.goto = go;
    }
    ui.vertical_centered(|ui| {
        ui.add_space(30.0);
        if let Some(e) = &app.cfg_err {
            ui.colored_label(Color32::LIGHT_RED, e);
        }
        if let Some(p) = &app.cfg_path {
            ui.weak(format!("config: {}", p.display()));
        }
        match (&app.meta, &app.view_err) {
            (Some(m), _) => {
                ui.weak(format!(
                    "view: {} · {} slices · newest full {}",
                    m.stem,
                    m.slices.len(),
                    app.slice_label(m.newest_full)
                ));
            }
            (None, Some(e)) => {
                ui.colored_label(Color32::LIGHT_RED, e);
            }
            _ => {
                ui.weak("opening view…");
            }
        }
        ui.weak(format!("{} built case notes", app.files.count()));
    });
}

fn cases_home(app: &mut App, ui: &mut Ui) {
    ui.heading("View Opinions");
    ui.label("Pick a case on the left. Cases come from cases/_index.json, laid out by the md-cases/ folders.");
    ui.add_space(8.0);
    let full = app.files.index.values().filter(|e| e.full).count();
    ui.label(format!(
        "{} notes in {} folders; {} cases in the index ({} with full text).",
        app.files.count(),
        app.files.folders.len(),
        app.files.index.len(),
        full
    ));
}

// ------------------------------------------------------------------ areas

fn areas(app: &mut App, ui: &mut Ui, rows: &[AreaRow]) {
    ui.heading("Areas");
    ui.label("Sub-cluster lineages pooled over all slices (tribonian cocite areas). Heaviest first.");
    ui.add_space(6.0);
    let f = app.filter.clone();
    egui::Grid::new("areas").striped(true).num_columns(4).show(ui, |ui| {
        ui.strong("Area");
        ui.strong("Lineages");
        ui.strong("Span");
        ui.strong("Weight");
        ui.end_row();
        for a in rows.iter().filter(|a| matches(&f, &a.name)) {
            let text = if a.labelled { RichText::new(&a.name) } else { RichText::new(&a.name).italics() };
            if ui.link(text).clicked() {
                app.goto = Some(Page::Area(a.area));
            }
            ui.label(a.lineages.to_string());
            ui.label(format!("{} – {}", app.slice_label(a.first), app.slice_label(a.last)));
            ui.label(format!("{:.0}", a.weight));
            ui.end_row();
        }
    });
}

#[allow(clippy::too_many_arguments)]
fn area_page(
    app: &mut App,
    ui: &mut Ui,
    area: i64,
    name: &str,
    label: &Option<Label>,
    first: i64,
    last: i64,
    lineages: &[LineageRow],
    core: &[CaseRow],
    graph: &GraphData,
) {
    ui.weak(format!("Area {}", area_code(area)));
    ui.heading(name);
    status_badge(ui, label);
    if let Some(l) = label {
        ui.add(egui::Label::new(&l.description).wrap());
    }
    ui.weak(format!("{} – {}", app.slice_label(first), app.slice_label(last)));
    ui.add_space(8.0);
    egui::CollapsingHeader::new(RichText::new("History graph").strong())
        .default_open(true)
        .show(ui, |ui| {
            let slices: Vec<i64> = app.meta.as_ref().map(|m| m.slices.iter().map(|s| s.slice).collect()).unwrap_or_default();
            let labels: std::collections::HashMap<i64, String> =
                slices.iter().map(|&s| (s, app.slice_label(s))).collect();
            let sl = move |s: i64| labels.get(&s).cloned().unwrap_or_default();
            let export = app.cfg.as_ref().map(|c| crate::graph::ExportInfo {
                dir: c.exports_dir(),
                area_code: area_code(area),
                area_name: name.to_string(),
            });
            if let Some(p) = crate::graph::area_graph(ui, &mut app.graph_opts, &slices, lineages, graph, &sl, export) {
                app.goto = Some(p);
            }
        });
    ui.add_space(8.0);
    let f = app.filter.clone();
    ui.strong(format!("Lineages ({})", lineages.len()));
    egui::Grid::new("lins").striped(true).num_columns(4).show(ui, |ui| {
        for l in lineages.iter().filter(|l| matches(&f, &l.name)) {
            let text = if l.labelled { RichText::new(&l.name) } else { RichText::new(&l.name).italics() };
            if ui.link(text).clicked() {
                app.goto = Some(Page::Lineage(l.lineage));
            }
            ui.label(format!("{} – {}", app.slice_label(l.first), app.slice_label(l.last)));
            ui.label(format!("{:.0}", l.weight));
            match l.confidence {
                Some(c) if c < 0.5 => {
                    ui.label(RichText::new(format!("boundary: {:.0}% of ties inside", c * 100.0)).small());
                }
                _ => {
                    ui.label("");
                }
            }
            ui.end_row();
        }
    });
    ui.add_space(8.0);
    ui.strong("Core cases (the area's fingerprint)");
    case_grid(app, ui, "core", core, None);
}

// ------------------------------------------------------------------ lineage

#[allow(clippy::too_many_arguments)]
fn lineage_page(
    app: &mut App,
    ui: &mut Ui,
    lineage: i64,
    level: Level,
    name: &str,
    label: &Option<Label>,
    anchor: &str,
    area: &Option<(i64, String)>,
    cells: &[UnitRow],
    merged_in: &[LinkRow],
    split_out: &[LinkRow],
) {
    ui.weak(format!(
        "Lineage {lineage} · {} level · founded on {anchor}",
        if level == Level::Sub { "sub-cluster" } else { "cluster" }
    ));
    ui.heading(name);
    status_badge(ui, label);
    if let Some(l) = label {
        ui.add(egui::Label::new(&l.description).wrap());
    }
    if let Some((a, an)) = area {
        ui.horizontal(|ui| {
            ui.label("Area:");
            if ui.link(an).clicked() {
                app.goto = Some(Page::Area(*a));
            }
        });
    }
    ui.add_space(8.0);
    ui.strong("Timeline");
    egui::ScrollArea::horizontal().id_salt("timeline").show(ui, |ui| {
        ui.horizontal(|ui| {
            for c in cells {
                let text = format!("{}\n{}\n{} cases", app.slice_label(c.slice), unit_code(c.level, c.k), c.size);
                if ui
                    .add(egui::Button::new(RichText::new(text).small()).min_size(egui::vec2(72.0, 52.0)))
                    .on_hover_text(&c.name)
                    .clicked()
                {
                    app.goto = Some(c.page());
                }
            }
        });
    });
    ui.add_space(8.0);
    link_list(app, ui, "Joined from other lineages", merged_in, "from");
    link_list(app, ui, "Went on to other lineages", split_out, "to");
}

fn link_list(app: &mut App, ui: &mut Ui, title: &str, rows: &[LinkRow], arrow: &str) {
    if rows.is_empty() {
        return;
    }
    ui.strong(title);
    egui::Grid::new(title).striped(true).num_columns(4).show(ui, |ui| {
        for l in rows {
            ui.label(format!("{} {arrow}", app.slice_label(l.here_slice)));
            if ui.link(l.other.code(&app.slice_label(l.other.slice))).clicked() {
                app.goto = Some(l.other.page());
            }
            if ui.link(&l.other.name).on_hover_text("open that lineage").clicked() {
                app.goto = Some(Page::Lineage(l.other.lineage));
            }
            ui.label(format!("{} shared · Jaccard {:.2}", l.shared, l.jaccard));
            ui.end_row();
        }
    });
    ui.add_space(6.0);
}

// ------------------------------------------------------------------ slice, cluster, sub-cluster

fn slice_page(app: &mut App, ui: &mut Ui, slice: i64, clusters: &[(UnitRow, i64)]) {
    let partial = app.meta.as_ref().and_then(|m| m.slices.iter().find(|s| s.slice == slice)).map(|s| s.partial).unwrap_or(false);
    ui.heading(format!("{}{}", app.slice_label(slice), if partial { " (partial)" } else { "" }));
    ui.label(format!("{} clusters, largest first.", clusters.len()));
    ui.add_space(6.0);
    let f = app.filter.clone();
    let sl = app.slice_label(slice);
    egui::Grid::new("slice").striped(true).num_columns(4).show(ui, |ui| {
        ui.strong("Cluster");
        ui.strong("Name");
        ui.strong("Cases");
        ui.strong("Sub-clusters");
        ui.end_row();
        for (c, nsub) in clusters.iter().filter(|(c, _)| matches(&f, &c.name)) {
            ui.label(c.code(&sl));
            if ui.link(&c.name).clicked() {
                app.goto = Some(c.page());
            }
            ui.label(c.size.to_string());
            ui.label(if *nsub > 0 { nsub.to_string() } else { "–".into() });
            ui.end_row();
        }
    });
}

fn unit_page(app: &mut App, ui: &mut Ui, head: &UnitHead, children: &[UnitRow], cases: &[CaseRow]) {
    let u = &head.unit;
    let sl = app.slice_label(u.slice);
    ui.horizontal(|ui| {
        ui.weak(format!("{} {}", if u.level == Level::Sub { "Sub-cluster" } else { "Cluster" }, u.code(&sl)));
        ui.weak("·");
        if ui.link(RichText::new(&sl).weak()).clicked() {
            app.goto = Some(Page::Slice(u.slice));
        }
    });
    ui.heading(&u.name);
    status_badge(ui, &head.label);
    if let Some(l) = &head.label {
        ui.add(egui::Label::new(&l.description).wrap());
    }
    ui.add_space(4.0);
    if let Some(p) = &head.parent {
        if p.k != u.k || u.k % 1000 != 0 {
            ui.horizontal(|ui| {
                ui.label("Within cluster:");
                if ui.link(format!("{} {}", p.code(&sl), p.name)).clicked() {
                    app.goto = Some(p.page());
                }
            });
        }
    }
    if u.lineage != 0 {
        ui.horizontal(|ui| {
            ui.label("Lineage:");
            let span = match (head.lineage_first, head.lineage_last) {
                (Some(a), Some(b)) => format!("{} – {}", app.slice_label(a), app.slice_label(b)),
                _ => String::new(),
            };
            if ui.link(format!("{} ({span})", u.lineage)).clicked() {
                app.goto = Some(Page::Lineage(u.lineage));
            }
        });
    }
    if let Some((a, an)) = &head.area {
        ui.horizontal(|ui| {
            ui.label("Area:");
            if ui.link(an).clicked() {
                app.goto = Some(Page::Area(*a));
            }
        });
    }
    if u.level == Level::Sub {
        ui.add_space(4.0);
        crate::subdoc::bar(app, ui, u.slice, u.k);
    } else if children.is_empty() {
        ui.add_space(4.0);
        crate::subdoc::bar(app, ui, u.slice, u.k * 1000); // an unsplit cluster is its own sub-cluster
    }
    ui.add_space(8.0);
    let f = app.filter.clone();
    if !children.is_empty() {
        ui.strong(format!("Sub-clusters ({})", children.len()));
        egui::Grid::new("children").striped(true).num_columns(3).show(ui, |ui| {
            for c in children.iter().filter(|c| matches(&f, &c.name)) {
                ui.label(c.code(&sl));
                if ui.link(&c.name).clicked() {
                    app.goto = Some(c.page());
                }
                ui.label(format!("{} cases", c.size));
                ui.end_row();
            }
        });
        ui.add_space(8.0);
    }
    let shown = if cases.len() >= MAX_ROWS { format!("; showing the first {}", cases.len()) } else { String::new() };
    ui.strong(format!(
        "Cases ({} in this {}{shown}){}. Click a column heading to sort.",
        u.size,
        if u.level == Level::Cluster { "cluster" } else { "sub-cluster" },
        if u.level == Level::Cluster { ". Home cluster, or another cluster holding a share of their ties" } else { "" }
    ));
    let help = if u.level == Level::Sub {
        "Ties inside this sub-cluster: the sum of the case's pair weights with the other cases in it \
         (decisions citing both; sentence-weighted in a sentence view). Higher = more central."
    } else {
        "The case's pair weight going to this cluster's members. Higher = more central."
    };
    case_grid(app, ui, "cases", cases, Some(help));
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SortKey {
    Name,
    Cite,
    Year,
    Weight,
    Citing,
    GoodLaw,
}

/// "467 U.S. 837" sorts by reporter, then volume, then page; anything else by its text.
fn cite_key(c: &str) -> (String, i64, i64) {
    let parts: Vec<&str> = c.split_whitespace().collect();
    if parts.len() >= 3 {
        if let (Ok(v), Ok(p)) = (parts[0].parse::<i64>(), parts[parts.len() - 1].parse::<i64>()) {
            return (parts[1..parts.len() - 1].join(" "), v, p);
        }
    }
    (c.to_string(), 0, 0)
}

const GREY: Color32 = Color32::from_rgb(140, 140, 140);

fn gl_color(status: &str) -> Color32 {
    match status {
        "red" => Color32::from_rgb(210, 70, 60),
        "yellow" => Color32::from_rgb(210, 170, 40),
        "green" => Color32::from_rgb(70, 160, 90),
        _ => GREY,
    }
}

/// Hover on the Good law column heading.
const GL_HELP: &str = "From tribonian map goodlaw (warning words in the opinions that cite the case; no language model), \
where it has been run for the case.\n\
Red: a court that could overrule the case used an overruling word about it (overruled, abrogated, superseded, reversed, \
vacated, …), or two lower courts did.\n\
Yellow: doubts: one such word short of red or one \"on other grounds\", three softer words (questioned, criticized, \
declined to follow, …), or two softer words from courts that could overrule it.\n\
Green: none of that in the opinions read.\n\
Grey (dormant): never cited, or not cited in the last 5 full years before the bulk data's date. A red or yellow case \
that is dormant keeps its colour and gets a grey \"dormant\" tag. Shown where a cached profile has the citations per \
year, or CourtListener's citing count is 0.";

/// Sort order: red, yellow, green, dormant, unknown.
fn goodlaw_rank(g: &Option<GoodLaw>, d: &Option<Dormancy>) -> i32 {
    let dormant = d.as_ref().is_some_and(|d| d.dormant);
    match g.as_ref().map(|g| g.status.as_str()) {
        Some("red") => 4,
        Some("yellow") => 3,
        _ if dormant => 1,
        Some("green") => 2,
        _ => 0,
    }
}

/// The citation mark ⟦…⟧ in a snippet as the \u{1}…\u{2} markers snippet_label highlights.
fn evidence_marks(snippet: &str) -> String {
    trim_snippet(snippet).replace('⟦', "\u{1}").replace('⟧', "\u{2}")
}

/// casemap.py cuts snippets at a fixed width: drop the stray punctuation that leaves at the start.
fn trim_snippet(s: &str) -> &str {
    s.trim_start_matches(|c: char| c == '.' || c == ',' || c == ';' || c == ')' || c.is_whitespace())
}

fn kind_label(kind: &str) -> &str {
    match kind {
        "other grounds" => "on other grounds",
        "weak" => "softer word",
        k => k,
    }
}

/// The first few warnings as plain text, for a table cell's hover.
fn evidence_hover(g: &GoodLaw) -> String {
    let mut out = Vec::new();
    for e in g.evidence.iter().take(3) {
        out.push(format!(
            "{} {}: {} “{}”{}\n{}",
            e.year,
            e.court,
            kind_label(&e.kind),
            e.word,
            if e.above { " (from a court above)" } else { "" },
            trim_snippet(&e.snippet).replace(['⟦', '⟧'], "")
        ));
    }
    if g.n_evidence > out.len() as i64 {
        out.push(format!("… {} more on the case page", g.n_evidence - out.len() as i64));
    }
    if out.is_empty() {
        out.push(format!("No warning words found in the {} citing opinions read.", g.read.unwrap_or(0)));
    }
    out.join("\n\n")
}

fn goodlaw_cell(ui: &mut Ui, g: &Option<GoodLaw>, d: &Option<Dormancy>) {
    let dormant = d.as_ref().filter(|d| d.dormant);
    ui.horizontal(|ui| {
        match (g, dormant) {
            (Some(g), _) if g.status == "red" || g.status == "yellow" => {
                ui.colored_label(gl_color(&g.status), g.short()).on_hover_text(evidence_hover(g));
                if let Some(d) = dormant {
                    ui.label(RichText::new("dormant").small().color(GREY)).on_hover_text(d.hover());
                }
            }
            (_, Some(d)) => {
                let mut h = d.hover();
                if let Some(g) = g {
                    h = format!("{h}\n\nGood-law check: {}. {}", g.status, evidence_hover(g));
                }
                ui.colored_label(GREY, "dormant").on_hover_text(h);
            }
            (Some(g), None) => {
                ui.colored_label(gl_color(&g.status), &g.status).on_hover_text(evidence_hover(g));
            }
            (None, None) => {
                ui.weak("–").on_hover_text("not computed (tribonian map goodlaw)");
            }
        }
    });
}

/// A sortable table of cases. `weight_help` names the weight column's meaning; None hides it.
fn case_grid(app: &mut App, ui: &mut Ui, id: &str, cases: &[CaseRow], weight_help: Option<&str>) {
    let f = app.filter.clone();
    let mut rows: Vec<&CaseRow> = cases.iter().filter(|c| matches(&f, &c.name) || matches(&f, &c.cite)).collect();
    let (key, desc) = app.case_sort;
    rows.sort_by(|a, b| {
        let o = match key {
            SortKey::Name => a.name.to_lowercase().cmp(&b.name.to_lowercase()),
            SortKey::Cite => cite_key(&a.cite).cmp(&cite_key(&b.cite)),
            SortKey::Year => a.year.cmp(&b.year),
            SortKey::Weight => a.weight.total_cmp(&b.weight),
            SortKey::Citing => a.citing.cmp(&b.citing),
            SortKey::GoodLaw => goodlaw_rank(&a.goodlaw, &a.dormant).cmp(&goodlaw_rank(&b.goodlaw, &b.dormant)),
        };
        if desc { o.reverse() } else { o }
    });
    let has_citing = cases.iter().any(|c| c.citing.is_some());
    let mut sort = app.case_sort;
    let mut go: Option<Page> = None;
    let mut header = |ui: &mut Ui, label: &str, k: SortKey, help: &str| {
        let arrow = if sort.0 == k { if sort.1 { " ⏷" } else { " ⏶" } } else { "" };
        let r = ui.add(egui::Button::new(RichText::new(format!("{label}{arrow}")).strong()).frame(false));
        let r = if help.is_empty() { r } else { r.on_hover_text(help) };
        if r.clicked() {
            // first click: the natural direction (text ascending, numbers descending); again: flip
            sort = if sort.0 == k {
                (k, !sort.1)
            } else {
                (k, !matches!(k, SortKey::Name | SortKey::Cite))
            };
        }
    };
    egui::Grid::new(id).striped(true).num_columns(8).show(ui, |ui| {
        header(ui, "Case", SortKey::Name, "");
        ui.label("");
        header(ui, "Cite", SortKey::Cite, "by reporter, then volume, then page");
        header(ui, "Year", SortKey::Year, "");
        if let Some(h) = weight_help {
            header(ui, "Weight", SortKey::Weight, h);
        } else {
            ui.label("");
        }
        if has_citing {
            header(ui, "Citing", SortKey::Citing, "decisions in this slice citing the case (appellate courts in this view)");
        } else {
            ui.label("");
        }
        header(ui, "Good law", SortKey::GoodLaw, GL_HELP);
        ui.label("");
        ui.end_row();
        for c in rows {
            if ui.link(&c.name).clicked() {
                go = Some(Page::Case(c.r#ref));
            }
            built_tag(app, ui, c.r#ref);
            ui.label(&c.cite);
            ui.label(year(c.year));
            if weight_help.is_some() {
                ui.label(format!("{:.1}", c.weight));
            } else {
                ui.label("");
            }
            ui.label(c.citing.map(|n| n.to_string()).unwrap_or_default());
            goodlaw_cell(ui, &c.goodlaw, &c.dormant);
            ui.label(RichText::new(&c.note).small());
            ui.end_row();
        }
    });
    app.case_sort = sort;
    if go.is_some() {
        app.goto = go;
    }
    cut_note(ui, cases.len());
}

// ------------------------------------------------------------------ case page

fn case_page(app: &mut App, ui: &mut Ui, c: &CaseData) {
    let entry = app.files.index.get(&c.r#ref).cloned();
    // the index has the full case name; the cocite names table has the short one
    match &entry {
        Some(e) if !e.name.is_empty() => {
            ui.heading(&e.name);
            if e.name != c.name {
                ui.weak(format!("short name: {}", c.name));
            }
        }
        _ => match app.api_names.get(&c.r#ref) {
            // not in the local database yet: the name CourtListener's search gave
            Some(n) if c.name.starts_with("cluster ") || c.name.is_empty() => {
                ui.heading(n);
                ui.weak("not in the local database: Quick build downloads it from CourtListener");
            }
            _ => {
                ui.heading(&c.name);
            }
        },
    }
    ui.label(format!("{} {}", c.cite, c.year.map(|y| format!("({y})")).unwrap_or_default()).trim());
    let court = entry.as_ref().map(|e| e.court.clone()).filter(|c| !c.is_empty()).unwrap_or_else(|| c.court.clone());
    if !court.is_empty() {
        ui.weak(format!("court: {court}"));
    }
    ui.horizontal(|ui| {
        ui.weak(format!("CourtListener cluster {}", c.r#ref));
        let name = entry.as_ref().map(|e| e.name.clone()).filter(|n| !n.is_empty()).unwrap_or_else(|| c.name.clone());
        together_button(app, ui, c.r#ref, &name);
        if ui
            .small_button("Cited by…")
            .on_hover_text("every decision citing this case (where used), newest first; add more cases with ＋ to narrow it")
            .clicked()
        {
            app.goto = Some(Page::CoCited(vec![c.r#ref]));
        }
    });
    ui.add_space(6.0);

    // files
    let note = app.files.note_of(c.r#ref).cloned();
    ui.horizontal(|ui| match &note {
        Some(n) => {
            if let Some(pdf) = &n.pdf {
                if ui.button("Open PDF").clicked() {
                    open_external(&pdf.to_string_lossy());
                }
            } else {
                ui.add_enabled(false, egui::Button::new("Open PDF")).on_disabled_hover_text("no PDF built");
            }
            let url = app.cfg.as_ref().and_then(|cfg| obsidian_url(cfg, n));
            match url {
                Some(u) => {
                    if ui.button("Open note in Obsidian").on_hover_text(&u).clicked() {
                        open_external(&u);
                    }
                }
                None => {
                    if ui.button("Open note file").clicked() {
                        open_external(&n.path.to_string_lossy());
                    }
                }
            }
            ui.weak(if n.folder.is_empty() { "md-cases/".into() } else { format!("md-cases/{}/", n.folder) });
        }
        None => {
            ui.weak("No note built for this case.");
        }
    });
    // Build: the repo's own scripts, queued; status and output in the panel at the bottom
    ui.horizontal(|ui| {
        let queued = app.is_queued(c.r#ref);
        let title = entry.as_ref().map(|e| e.name.clone()).filter(|n| !n.is_empty()).unwrap_or_else(|| c.name.clone());
        law_checkboxes(ui, app, "case");
        let quick = ui
            .add_enabled(!queued, egui::Button::new("Quick build"))
            .on_hover_text(format!(
                "note, PDF and case profile, with the quick good-law check: only the newest 100 citing opinions, so the status \
                 now but not the year it changed. Queues tribonian map get --cluster {0} (only if the case has no file yet), \
                 then tribonian case build --quick-goodlaw. A current full result is kept.",
                c.r#ref
            ));
        let full = ui
            .add_enabled(!queued, egui::Button::new("Build"))
            .on_hover_text(
                "everything, including the full good-law check, which reads up to ~2,000 citing opinions and takes a long time \
                 but finds the year it changed. Queues the same commands without --quick-goodlaw.",
            );
        if quick.clicked() {
            app.enqueue_build(c.r#ref, title.clone(), true);
        }
        let recent = ui
            .add_enabled(!queued && note.is_some(), egui::Button::new("Recent citers"))
            .on_hover_text(
                "ask CourtListener who has cited this case since the bulk data (tribonian map recent: 1 request per 20 \
                 citing cases, plus 1 per opinion read, up to 25), then Build: the good-law check reads them too. \
                 Only the cases the local database lacks are added.",
            )
            .on_disabled_hover_text("build the case first");
        if recent.clicked() {
            app.enqueue_recent_citers(c.r#ref, title.clone());
        }
        if full.clicked() {
            app.enqueue_build(c.r#ref, title, false);
        }
        if queued {
            ui.weak("in the build queue");
        } else if note.is_some() {
            ui.weak("tribonian sync-vault / sync-remarkable still copy it to Obsidian and the tablet");
        }
    });

    // good law, and whether it is still cited
    let dormant = c.dormant.as_ref().filter(|d| d.dormant);
    ui.horizontal(|ui| {
        ui.label("Good law:").on_hover_text(GL_HELP);
        let shown = c.goodlaw.as_ref().filter(|g| g.status == "red" || g.status == "yellow" || dormant.is_none());
        if let Some(g) = shown {
            ui.colored_label(gl_color(&g.status), RichText::new(&g.status).strong());
            if let Some(n) = g.quick {
                ui.label(RichText::new("quick check").small())
                    .on_hover_text(format!("Read only the newest {n} citing opinions: the status now, not the year it changed. Build runs the full check."));
            }
            if let Some(y) = g.red_year {
                ui.label(format!("red since {y}"));
            } else if let Some(y) = g.yellow_year {
                ui.label(format!("yellow since {y}"));
            }
        }
        if let Some(d) = dormant {
            let text = if shown.is_some() { RichText::new("dormant").small() } else { RichText::new("dormant").strong() };
            ui.colored_label(GREY, text).on_hover_text(d.hover());
            ui.label(RichText::new(d.reason()).color(GREY)).on_hover_text(d.hover());
            if let Some(g) = c.goodlaw.as_ref().filter(|_| shown.is_none()) {
                ui.weak(format!("good-law check: {}", g.status));
            }
        }
        match &c.goodlaw {
            Some(g) => {
                if let Some(s) = &g.stamp {
                    ui.weak(format!("data {s}"));
                }
            }
            None => {
                ui.weak(if dormant.is_some() { "good-law check not calculated" } else { "not calculated" }).on_hover_text(
                    "The check reads every citing opinion from a court that could overrule the case, plus the 500 newest \
                     (up to about 2,000), so it takes a long time. Build calculates it and caches it; Quick build runs the quick check (the newest 100 citing opinions).",
                );
            }
        }
    });
    if let Some(g) = &c.goodlaw {
        why_section(app, ui, c, g);
    }
    ui.add_space(6.0);
    case_details(app, ui, c, note.as_ref());
    ui.add_space(8.0);

    // where it sits
    if let Some((l, ln)) = &c.lineage {
        ui.horizontal(|ui| {
            ui.label("Sub-cluster lineage:");
            if ui.link(ln).clicked() {
                app.goto = Some(Page::Lineage(*l));
            }
        });
    }
    if let Some((a, an)) = &c.area {
        ui.horizontal(|ui| {
            ui.label("Area:");
            if ui.link(an).clicked() {
                app.goto = Some(Page::Area(*a));
            }
        });
    }
    ui.add_space(6.0);
    if c.slices.is_empty() {
        ui.label("Not a node in any slice of this view: cited too rarely to be clustered.");
    } else {
        ui.strong("Where it sits, slice by slice");
        egui::Grid::new("sits").striped(true).num_columns(5).show(ui, |ui| {
            ui.strong("Slice");
            ui.strong("Citing");
            ui.strong("Home cluster");
            ui.strong("Sub-cluster");
            ui.strong("Also in");
            ui.end_row();
            for s in &c.slices {
                let sl = app.slice_label(s.slice);
                if ui.link(&sl).clicked() {
                    app.goto = Some(Page::Slice(s.slice));
                }
                ui.label(s.n.to_string());
                match &s.home {
                    Some(h) => {
                        let share = s.home_share.map(|x| format!(" · {:.0}%", x * 100.0)).unwrap_or_default();
                        if ui.link(format!("{} {}{share}", h.code(&sl), h.name)).clicked() {
                            app.goto = Some(h.page());
                        }
                    }
                    None => {
                        ui.label("–");
                    }
                }
                match &s.sub {
                    Some(u) if u.k % 1000 != 0 => {
                        if ui.link(format!("{} {}", u.code(&sl), u.name)).clicked() {
                            app.goto = Some(u.page());
                        }
                    }
                    _ => {
                        ui.label("–");
                    }
                }
                ui.vertical(|ui| {
                    for (u, share) in &s.also {
                        if ui.link(format!("{} {} · {:.0}%", u.code(&sl), u.name, share * 100.0)).clicked() {
                            app.goto = Some(u.page());
                        }
                    }
                });
                ui.end_row();
            }
        });
    }
    ui.add_space(8.0);
    draws_section(app, ui, c);
    coupling_section(app, ui, c);
    ui.add_space(8.0);
    if let Some(ps) = c.partner_slice {
        ui.strong(format!("Partners in {} (most often cited with it, by cosine)", app.slice_label(ps)));
        egui::Grid::new("partners").striped(true).num_columns(7).show(ui, |ui| {
            ui.strong("Case");
            ui.label("");
            ui.strong("Cite");
            ui.strong("Year");
            ui.strong("Support");
            ui.strong("Lift");
            ui.strong("Cosine");
            ui.end_row();
            for p in &c.partners {
                let name = if p.name.is_empty() { format!("cluster {}", p.r#ref) } else { p.name.clone() };
                if ui.link(name).clicked() {
                    app.goto = Some(Page::Case(p.r#ref));
                }
                built_tag(app, ui, p.r#ref);
                ui.label(&p.cite);
                ui.label(year(p.year));
                ui.label(format!("{:.1}", p.support));
                ui.label(format!("{:.1}", p.lift));
                ui.label(format!("{:.3}", p.cosine));
                ui.end_row();
            }
        });
    }
}

/// casemap.py's rule for each colour, in one sentence.
fn rule_sentence(status: &str) -> &'static str {
    match status {
        "red" => "Red: a court that could overrule the case used an overruling word about it (overruled, abrogated, superseded, \
                  reversed, vacated, disapproved, overturned, repudiated, abandoned), next to its citation or in a sentence \
                  naming it, not \"on other grounds\" and not in a hypothetical or an argument; or two lower courts did.",
        "yellow" => "Yellow: doubts. One overruling word that falls short of red, or one \"on other grounds\"; or three softer \
                     words (questioned, criticized, called into question, declined to follow or extend, no longer good law, \
                     limited to its facts, eroded); or two softer words from courts that could overrule it.",
        _ => "Green: no overruling word, no \"on other grounds\", and too few softer words for yellow, in the citing opinions read.",
    }
}

/// Which courts "could overrule" a case at this level (casemap.py can_overrule).
fn level_phrase(level: &str) -> &'static str {
    match level {
        "supreme" => "the Supreme Court",
        "circuit" => "a federal court of appeals or the Supreme Court",
        "district" => "a federal district court, a court of appeals or the Supreme Court",
        _ => "the Supreme Court or the case's own court",
    }
}

/// "Why red/yellow/green": the rule, what was read, and each warning with its words.
fn why_section(app: &mut App, ui: &mut Ui, c: &CaseData, g: &GoodLaw) {
    egui::CollapsingHeader::new(RichText::new(format!("Why {}", g.status)).strong())
        .id_salt(("why", c.r#ref))
        .default_open(g.status != "green")
        .show(ui, |ui| {
            wrapped_weak(ui, rule_sentence(&g.status).to_string());
            if let (Some(n), Some(r), Some(o)) = (g.quick, g.read, g.of) {
                let when = g.window.map(|(a, b)| if a == b { format!(", filed {a}") } else { format!(", filed {a}–{b}") }).unwrap_or_default();
                ui.add(
                    egui::Label::new(format!(
                        "Quick check: read only the newest {} of {} citing opinions (at most {n}{when}). This is the status now; \
                         an older change falls outside what was read, so there is no year. Build runs the full check.",
                        thousands(r),
                        thousands(o)
                    ))
                    .wrap(),
                );
            } else if let (Some(r), Some(o)) = (g.read, g.of) {
                let level = if g.level.is_empty() { String::new() } else { format!(" (case level: {})", g.level) };
                ui.add(
                    egui::Label::new(format!(
                        "Read {} of {} citing opinions{level}: every one from a court that could overrule it ({}), up to {}, \
                         plus the {} newest from any court.",
                        thousands(r),
                        thousands(o),
                        level_phrase(&g.level),
                        thousands(GOODLAW_MAX),
                        GOODLAW_RECENT
                    ))
                    .wrap(),
                );
            }
            if let (Some(y), Some(p)) = (g.red_year, c.profile.as_ref().filter(|p| p.cached)) {
                let later: i64 = p.years.iter().filter(|x| x.0 > y).map(|x| x.1).sum();
                ui.label(format!("Still cited by {later} cases after {y}."));
            }
            if g.evidence.is_empty() {
                ui.label("No warning words were found in the opinions read.");
            }
            for e in &g.evidence {
                ui.add_space(4.0);
                ui.horizontal_wrapped(|ui| {
                    ui.label(RichText::new(&e.year).strong());
                    ui.label(&e.court);
                    ui.label(kind_label(&e.kind));
                    ui.label(RichText::new(format!("“{}”", e.word)).strong());
                    if e.above {
                        ui.weak("from a court above").on_hover_text("a court that could overrule the case (its level or above)");
                    }
                    let name = if e.name.is_empty() { format!("cluster {}", e.cluster) } else { e.name.clone() };
                    if e.cluster > 0 && ui.link(name).clicked() {
                        app.goto = Some(Page::Case(e.cluster));
                    }
                });
                let job = snippet_job(ui, &evidence_marks(&e.snippet));
                ui.add(egui::Label::new(job).wrap());
            }
            let more = g.n_evidence - g.evidence.len() as i64;
            if more > 0 {
                ui.weak(format!("… {more} more (the profile keeps the first {}).", g.evidence.len()));
            }
            if let Some(v) = g.version {
                ui.weak(format!("casemap.py good-law rules, version {v}. A flag for deciding what to read, not a citator."));
            }
        });
}

fn thousands(n: i64) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

/// "What it draws on": where the cases this one cites sit (each one's sub-cluster in the newest full
/// slice). Bibliographic coupling's cheap half, read live, so it places any case, even one the
/// co-citation map never holds.
fn draws_section(app: &mut App, ui: &mut Ui, c: &CaseData) {
    let Some(d) = &c.draws else { return };
    let sl = app.slice_label(d.slice);
    egui::CollapsingHeader::new(RichText::new(format!("What it draws on ({} cited cases)", d.refs)).strong())
        .default_open(true)
        .show(ui, |ui| {
            wrapped_weak(ui, format!(
                "Where the cases it cites sit: each one's sub-cluster in {sl}, the newest full slice. Counted from its citations."
            ));
            if c.slices.is_empty() {
                ui.label("Not in the co-citation map itself, so this is where its own citations place it.");
            }
            let max = d.subs.iter().map(|s| s.n).max().unwrap_or(1).max(1) as f32;
            let bar = ui.visuals().selection.bg_fill;
            egui::Grid::new("draws").striped(true).num_columns(4).show(ui, |ui| {
                ui.strong("Cites");
                ui.strong("Share");
                ui.strong("Sub-cluster");
                ui.strong("Area");
                ui.end_row();
                for s in &d.subs {
                    ui.label(s.n.to_string());
                    ui.horizontal(|ui| {
                        let (rect, _) = ui.allocate_exact_size(egui::vec2(90.0, 10.0), egui::Sense::hover());
                        let w = (rect.width() * s.n as f32 / max).max(1.0);
                        ui.painter().rect_filled(egui::Rect::from_min_size(rect.min, egui::vec2(w, rect.height())), 0.0, bar);
                        ui.label(format!("{:.0}%", 100.0 * s.n as f64 / d.refs.max(1) as f64));
                    });
                    if ui.link(format!("{} {}", s.unit.code(&sl), s.unit.name)).clicked() {
                        app.goto = Some(s.unit.page());
                    }
                    match &s.area {
                        Some((a, an)) => {
                            if ui.link(an).clicked() {
                                app.goto = Some(Page::Area(*a));
                            }
                        }
                        None => {
                            ui.label("–");
                        }
                    }
                    ui.end_row();
                }
            });
            if d.unclustered > 0 {
                ui.weak(format!(
                    "{} of the {} cited cases are in no sub-cluster of {sl} (cited too rarely then).",
                    d.unclustered, d.refs
                ));
            }
            if !d.areas.is_empty() {
                ui.horizontal_wrapped(|ui| {
                    ui.label("Areas:");
                    for (a, an, n) in d.areas.iter().take(6) {
                        if ui.link(an).clicked() {
                            app.goto = Some(Page::Area(*a));
                        }
                        ui.weak(format!("{:.0}%", 100.0 * *n as f64 / d.refs.max(1) as f64));
                    }
                });
            }
        });
}

/// A weak (grey) note that wraps at the panel's width instead of widening the page.
fn wrapped_weak(ui: &mut Ui, text: String) {
    ui.add(egui::Label::new(RichText::new(text).weak()).wrap());
}

/// "Coupled cases": what `casemap.py coupling` cached, and the button that queues it. Never run
/// automatically: it reads the whole citation cache.
fn coupling_section(app: &mut App, ui: &mut Ui, c: &CaseData) {
    let queued = app.is_queued(c.r#ref);
    let button = |app: &mut App, ui: &mut Ui, text: &str| {
        let b = ui.add_enabled(!queued, egui::Button::new(text)).on_hover_text(format!(
            "Finds the cases that cite the most of the same cases as this one, rare shared citations counting more \
             (bibliographic coupling). Queues tribonian map coupling --cluster {}, which reads the whole citation cache \
             (about 300 MB), so it takes seconds to a minute. Only on request: builds never run it. A later build then \
             shows the result in the note and the PDF.",
            c.r#ref
        ));
        if b.clicked() {
            app.enqueue_script(
                c.r#ref,
                format!("Coupled cases: {}", c.name),
                vec!["map".into(), "coupling".into(), "--cluster".into(), c.r#ref.to_string()],
            );
        }
    };
    match &c.coupling {
        None => {
            ui.horizontal(|ui| {
                button(app, ui, "Find coupled cases");
                if queued {
                    ui.weak("in the build queue");
                }
            });
        }
        Some(k) => {
            egui::CollapsingHeader::new(RichText::new(format!("Coupled cases ({})", k.results.len())).strong())
                .default_open(true)
                .show(ui, |ui| {
                    wrapped_weak(ui, format!(
                        "Cases citing the most of the same cases as this one; each shared cited case adds log(N/df), so rare ones \
                         count more. Computed {} from the bulk data of {}. {} of its {} cited cases were left out (cited by more \
                         than {} decisions). Hover a case for the cited cases it shares.",
                        k.computed, k.stamp, k.skipped, k.refs, k.max_df
                    ));
                    egui::Grid::new("coupled").striped(true).num_columns(6).show(ui, |ui| {
                        ui.strong("Case");
                        ui.label("");
                        ui.strong("Score");
                        ui.strong("Shared");
                        ui.strong("Cite");
                        ui.strong("Court, year");
                        ui.end_row();
                        for x in &k.results {
                            let shared: Vec<String> = x.refs.iter().map(|r| format!("{} (cited by {})", r.name, r.df)).collect();
                            let name = if x.name.chars().count() > 70 { format!("{}…", x.name.chars().take(69).collect::<String>()) } else { x.name.clone() };
                            if ui.link(name).on_hover_text(format!("Shared cited cases, rarest first:\n{}", shared.join("\n"))).clicked() {
                                app.goto = Some(Page::Case(x.r#ref));
                            }
                            built_tag(app, ui, x.r#ref);
                            ui.label(format!("{:.1}", x.score));
                            ui.label(x.shared.to_string());
                            ui.label(&x.cite);
                            ui.label(format!("{} {}", x.court, x.year).trim());
                            ui.end_row();
                        }
                    });
                    ui.horizontal(|ui| {
                        button(app, ui, "Find coupled cases again");
                        if queued {
                            ui.weak("in the build queue");
                        }
                    });
                });
        }
    }
}

// ================================================================== loading placeholders

/// Grey boxes shaped like the page that is loading, pulsing gently (no spinner).
fn skeleton(ui: &mut Ui, page: &Page) {
    let t = ui.input(|i| i.time) as f32;
    ui.ctx().request_repaint();
    let base = if ui.visuals().dark_mode { 60.0 } else { 215.0 };
    let pulse = 0.5 + 0.5 * (t * 3.0).sin();
    // shade: 0 = darkest bar, 1 = lightest; every bar breathes with the same pulse
    let bar = |ui: &mut Ui, w: f32, h: f32, shade: f32| {
        let v = (base + (shade * 18.0 + pulse * 14.0) * if ui.visuals().dark_mode { 1.0 } else { -1.0 }) as u8;
        let (r, _) = ui.allocate_exact_size(egui::vec2(w, h), egui::Sense::hover());
        ui.painter().rect_filled(r, 3.0, Color32::from_gray(v));
    };
    // fixed pseudo-random widths so rows differ but do not flicker
    let wid = |i: usize, lo: f32, span: f32| lo + span * (((i * 7919 + 13) % 97) as f32 / 97.0);
    let full = ui.available_width().min(900.0);
    ui.add_space(4.0);
    bar(ui, 120.0, 10.0, 0.2);
    ui.add_space(6.0);
    bar(ui, full * 0.45, 22.0, 0.6);
    ui.add_space(6.0);
    bar(ui, full * 0.9, 12.0, 0.3);
    ui.add_space(3.0);
    bar(ui, full * 0.7, 12.0, 0.3);
    ui.add_space(14.0);
    if matches!(page, Page::Area(_) | Page::Lineage(_)) {
        // the graph or timeline block
        bar(ui, full * 0.6, if matches!(page, Page::Area(_)) { 260.0 } else { 52.0 }, 0.1);
        ui.add_space(14.0);
    }
    if matches!(page, Page::Case(_)) {
        ui.horizontal(|ui| {
            bar(ui, 80.0, 20.0, 0.5);
            bar(ui, 150.0, 20.0, 0.5);
        });
        ui.add_space(14.0);
    }
    for i in 0..16 {
        ui.horizontal(|ui| {
            bar(ui, wid(i, 140.0, 260.0), 12.0, 0.4 + 0.3 * ((i % 3) as f32 / 2.0));
            ui.add_space(16.0);
            bar(ui, wid(i + 5, 50.0, 60.0), 12.0, 0.2);
            ui.add_space(16.0);
            bar(ui, 40.0, 12.0, 0.1);
        });
        ui.add_space(7.0);
    }
}

// ================================================================== search

fn search_side(app: &mut App, ui: &mut Ui) {
    use crate::search::Mode;
    ui.heading("Search for Cases");
    let r = ui.add(
        egui::TextEdit::singleline(&mut app.search_input)
            .hint_text(if app.search_mode == Mode::Name { "name or citation" } else { "what the case stands for" })
            .desired_width(f32::INFINITY),
    );
    if matches!(app.current(), Page::Search { query, .. } if query.is_empty()) && !r.has_focus() && app.search_input.is_empty() {
        r.request_focus();
        ui.ctx().request_repaint();
    }
    let enter = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
    let mut mode = app.search_mode;
    ui.horizontal(|ui| {
        ui.radio_value(&mut mode, Mode::Name, "Case name");
        ui.radio_value(&mut mode, Mode::Parenthetical, "Parentheticals");
    });
    let changed = mode != app.search_mode;
    app.search_mode = mode;
    ui.checkbox(&mut app.include_api, "Include CourtListener").on_hover_text(
        "also search CourtListener for cases filed after the local database's bulk data (less a month): \
         1 API request per search, reused for a day (tribonian case api-search). Cases already in the local \
         database are left to the local results.",
    );
    if (ui.button("Search").clicked() || enter || (changed && !app.search_input.trim().is_empty()))
        && !app.search_input.trim().is_empty()
    {
        app.goto = Some(Page::Search { query: app.search_input.trim().to_string(), mode });
    }
    cocite_box(app, ui);
    ui.add_space(8.0);
    ui.weak(match mode {
        Mode::Name => "Every word must match; the last one matches as a prefix (\"chev\" finds Chevron). \
                       A citation such as 463 U.S. 29 is looked up as a citation.",
        Mode::Parenthetical => "Searches what later courts wrote about a case in their parentheticals. \
                                Use \"quotes\" for a phrase. Cases are ranked by how many matching \
                                parentheticals describe them, then by how often they are cited.",
    });
    ui.add_space(6.0);
    ui.weak("The index is search.sqlite (casedb.py). On the HDD a first search can take seconds; later ones are fast.");
    if !app.search_history.is_empty() {
        ui.separator();
        ui.horizontal(|ui| {
            ui.strong("Recent searches");
            if ui.link("Clear").on_hover_text("forget every recent search").clicked() {
                app.search_history.clear();
                crate::recent::save(&app.search_history);
            }
        });
        let current = match app.current() {
            Page::Search { query, mode } => Some((query.clone(), *mode)),
            _ => None,
        };
        let mut go = None;
        egui::ScrollArea::vertical().id_salt("recent").auto_shrink([false, false]).show(ui, |ui| {
            for (q, m) in &app.search_history {
                ui.horizontal(|ui| {
                    let here = current.as_ref() == Some(&(q.clone(), *m));
                    let text = if here { RichText::new(q).strong() } else { RichText::new(q) };
                    if ui.link(text).clicked() {
                        go = Some(Page::Search { query: q.clone(), mode: *m });
                    }
                    ui.weak(match m {
                        Mode::Name => "names",
                        Mode::Parenthetical => "parentheticals",
                    });
                    if app.search_cache.contains_key(&Page::Search { query: q.clone(), mode: *m }) {
                        ui.weak("·").on_hover_text("results kept: opens instantly");
                    }
                });
            }
        });
        if go.is_some() {
            app.goto = go;
        }
    }
}

/// Draw text with the \u{1}…\u{2} match markers from FTS5 snippet() in bold.
fn snippet_label(ui: &mut Ui, text: &str) {
    let mut job = snippet_job(ui, text);
    job.wrap.max_width = 700.0;
    ui.label(job);
}

/// The \u{1}…\u{2} markers as bold text on a faint background.
fn snippet_job(ui: &Ui, text: &str) -> egui::text::LayoutJob {
    use egui::text::{LayoutJob, TextFormat};
    let mut job = LayoutJob::default();
    let normal = TextFormat { color: ui.visuals().text_color(), ..Default::default() };
    let strong = TextFormat { color: ui.visuals().strong_text_color(), background: ui.visuals().faint_bg_color, ..Default::default() };
    let mut bold = false;
    let mut buf = String::new();
    for ch in text.chars() {
        if ch == '\u{1}' || ch == '\u{2}' {
            if !buf.is_empty() {
                job.append(&buf, 0.0, if bold { strong.clone() } else { normal.clone() });
                buf.clear();
            }
            bold = ch == '\u{1}';
        } else {
            buf.push(ch);
        }
    }
    if !buf.is_empty() {
        job.append(&buf, 0.0, if bold { strong } else { normal });
    }
    job
}

fn search_page(app: &mut App, ui: &mut Ui, page: &Page) {
    use crate::search::Mode;
    let Page::Search { query, mode } = page else { return };
    if query.is_empty() {
        ui.heading("Search for Cases");
        ui.label("Type a case name, a citation, a Supreme Court docket number (25-579), or (with Parentheticals) what a case stands for, then press Enter.");
        return;
    }
    // pending Supreme Court cases aren't in the CourtListener database: match scotus.py's dockets first
    if *mode == Mode::Name {
        crate::scotus_page::docket_hits(app, ui, query);
    }
    if app.include_api {
        api_hits(app, ui, page);
    }
    let Some(res) = app.search_cache.get(page).cloned() else {
        skeleton(ui, page);
        return;
    };
    let res = match res {
        Ok(r) => r,
        Err(e) => {
            ui.colored_label(Color32::LIGHT_RED, format!("Search failed: {e}"));
            return;
        }
    };
    ui.heading(format!("Search: {query}"));
    let what = match (mode, res.as_citation) {
        (Mode::Name, true) => "looked up as a citation".to_string(),
        (Mode::Name, false) => format!(
            "{} names matched{}, most cited first",
            res.read,
            if res.capped { " (read stopped at the cap)" } else { "" }
        ),
        (Mode::Parenthetical, _) => format!(
            "{} parentheticals matched{}; cases ranked by how many describe them, then by how often they are cited",
            res.read,
            if res.capped { " (read stopped at the cap)" } else { "" }
        ),
    };
    ui.weak(format!("{} cases · {what} · {} ms", res.hits.len(), res.elapsed.as_millis()));
    ui.add_space(8.0);
    if res.hits.is_empty() {
        ui.label(match mode {
            Mode::Name => "No case found. Try fewer words, or Parentheticals.",
            Mode::Parenthetical => "No parenthetical matches. Try fewer words or drop the quotes.",
        });
        return;
    }
    let f = app.filter.clone();
    let mut go = None;
    egui::ScrollArea::both().auto_shrink([false, false]).show(ui, |ui| {
        ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Extend);
        egui::Grid::new("hits").striped(true).num_columns(9).show(ui, |ui| {
            ui.label("");
            ui.strong("Case");
            ui.label("");
            ui.strong("Cite");
            ui.strong("Year");
            ui.strong("Court");
            ui.strong("Citing").on_hover_text("decisions citing it, summed over every slice of this view; blank = cited too rarely to be a node");
            ui.strong("Good law").on_hover_text(GL_HELP);
            ui.strong(if *mode == Mode::Parenthetical { "Parentheticals" } else { "" });
            ui.end_row();
            for h in res.hits.iter().filter(|h| matches(&f, &h.name) || matches(&f, &h.cite)) {
                together_button(app, ui, h.r#ref, &h.name);
                ui.vertical(|ui| {
                    if ui.link(&h.name).clicked() {
                        go = Some(Page::Case(h.r#ref));
                    }
                    if let Some(sn) = &h.snippet {
                        snippet_label(ui, sn);
                    }
                });
                built_tag(app, ui, h.r#ref);
                ui.label(&h.cite);
                ui.label(year(h.year));
                ui.label(&h.court);
                ui.label(if h.citing > 0 { h.citing.to_string() } else { String::new() });
                goodlaw_cell(ui, &h.goodlaw, &h.dormant);
                if *mode == Mode::Parenthetical {
                    ui.label(h.count.to_string());
                } else {
                    ui.label("");
                }
                ui.end_row();
            }
        });
    });
    if go.is_some() {
        app.goto = go;
    }
}

/// "Include CourtListener": the API's matches that the local database doesn't have, above the local
/// results. A name opens the case page; Get downloads and builds it (Quick build: casemap.py get falls
/// back to the API for a case the database lacks).
fn api_hits(app: &mut App, ui: &mut Ui, page: &Page) {
    let Some(res) = app.api_cache.get(page).cloned() else {
        if app.api_pending(page) {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.weak("Searching CourtListener…");
            });
        }
        return;
    };
    let res = match res {
        Ok(r) => r,
        Err(e) => {
            ui.colored_label(Color32::LIGHT_RED, format!("CourtListener search failed: {e}"));
            ui.separator();
            return;
        }
    };
    let new: Vec<_> = res.hits.iter().filter(|h| !h.local).collect();
    ui.strong(format!("CourtListener: {} not in the local database", new.len()));
    ui.weak(format!(
        "filed {} · {} matches, first {} read ({} already local) · {}",
        if res.after.is_empty() { "any date".to_string() } else { format!("after {}", res.after) },
        res.count.map(|n| n.to_string()).unwrap_or_else(|| "?".into()),
        res.hits.len(),
        res.hits.len() - new.len(),
        if res.cached { format!("cached {}", res.fetched) } else { "1 API request".to_string() }
    ));
    let mut go = None;
    let mut get = None;
    if !new.is_empty() {
        egui::Grid::new("api_hits").striped(true).num_columns(6).show(ui, |ui| {
            for h in &new {
                ui.vertical(|ui| {
                    if ui.link(&h.name).clicked() {
                        go = Some(Page::Case(h.cluster));
                    }
                    if !h.snippet.is_empty() {
                        snippet_label(ui, &h.snippet);
                    }
                });
                built_tag(app, ui, h.cluster);
                ui.label(&h.cite);
                ui.label(&h.date);
                ui.label(&h.court);
                let queued = app.is_queued(h.cluster);
                if ui
                    .add_enabled(!queued && app.files.note_of(h.cluster).is_none(), egui::Button::new("Get"))
                    .on_hover_text(format!(
                        "download from CourtListener and build (about 3 API requests): tribonian map get --cluster {}, \
                         then tribonian case build --no-goodlaw",
                        h.cluster
                    ))
                    .clicked()
                {
                    get = Some((h.cluster, h.name.clone()));
                }
                ui.end_row();
            }
        });
    }
    ui.separator();
    if let Some((r, name)) = get {
        app.enqueue_build(r, name, true);
    }
    if go.is_some() {
        app.goto = go;
    }
}

// ------------------------------------------------------------------ case page: fast details

/// Everything that can be shown at once without a build: the main database's details, the cached
/// (or quickly read) profile, how later courts describe the case, and the note's front matter.
fn case_details(app: &mut App, ui: &mut Ui, c: &CaseData, note: Option<&crate::cases::Note>) {
    if let Some(d) = &c.details {
        egui::Grid::new("details").num_columns(2).spacing([12.0, 2.0]).show(ui, |ui| {
            let row = |ui: &mut Ui, k: &str, v: &str| {
                if !v.is_empty() {
                    ui.weak(k);
                    ui.add(egui::Label::new(v).wrap());
                    ui.end_row();
                }
            };
            row(ui, "Docket", &d.docket);
            row(ui, "Filed", &d.date_filed);
            row(ui, "Judges", &d.judges);
            row(ui, "Status", &d.precedential);
            if let Some(n) = d.cl_cited {
                row(ui, "Cited by", &format!("{n} cases (CourtListener's count)"));
            }
            row(ui, "Citations", &d.citations.join(" · "));
        });
    }
    // case profile
    match &c.profile {
        Some(p) if p.cached => {
            egui::CollapsingHeader::new(RichText::new("Case profile").strong()).default_open(true).show(ui, |ui| {
                if !p.years.is_empty() {
                    let total: i64 = p.years.iter().map(|y| y.1).sum();
                    let peak = p.years.iter().max_by_key(|y| y.1).copied().unwrap_or((0, 0));
                    ui.label(format!(
                        "Cited by {total} cases, {}–{} · busiest year {} ({})",
                        p.years[0].0,
                        p.years[p.years.len() - 1].0,
                        peak.0,
                        peak.1
                    ));
                    year_bars(ui, &p.years);
                }
                if let Some((slice, name, r, share, citing)) = &p.successor {
                    ui.horizontal_wrapped(|ui| {
                        ui.label(format!("Role changed {slice}: likely successor"));
                        if let Some(r) = r {
                            if ui.link(name).clicked() {
                                app.goto = Some(Page::Case(*r));
                            }
                        } else {
                            ui.label(name);
                        }
                        ui.weak(format!("({:.0}% of the {citing} decisions citing this case also cite it; co-citation, not the text)", share * 100.0));
                    });
                }
                if !p.stamp.is_empty() {
                    ui.weak(format!("cached by the last build · bulk data of {}", p.stamp));
                }
            });
        }
        _ => {
            ui.weak("Case profile (citations per year, successor): not cached; Quick build or Build writes it.");
        }
    }
    // how later courts describe it
    if let Some(p) = &c.profile {
        if !p.groups.is_empty() {
            egui::CollapsingHeader::new(RichText::new(format!("How later courts describe it ({} parentheticals)", p.parens_total)).strong())
                .default_open(true)
                .show(ui, |ui| {
                    for g in &p.groups {
                        ui.horizontal_wrapped(|ui| {
                            ui.label(RichText::new(format!("{}×", g.n)).strong());
                            ui.add(egui::Label::new(format!("“{}”", g.text)).wrap());
                            let who = format!("{} ({} {})", g.case, g.court, g.year);
                            match g.r#ref {
                                Some(r) => {
                                    if ui.link(RichText::new(who).small()).clicked() {
                                        app.goto = Some(Page::Case(r));
                                    }
                                }
                                None => {
                                    ui.weak(who);
                                }
                            }
                        });
                    }
                    if !p.cached {
                        ui.weak("Read just now from CourtListener's parentheticals, grouped by its similarity groups.");
                    }
                });
        }
    }
    if let Some(d) = &c.details {
        if !d.syllabus.is_empty() {
            egui::CollapsingHeader::new(RichText::new("Syllabus").strong()).default_open(true).show(ui, |ui| {
                ui.add(egui::Label::new(&d.syllabus).wrap());
            });
        }
        if !d.summary.is_empty() {
            egui::CollapsingHeader::new(RichText::new("Summary").strong()).default_open(true).show(ui, |ui| {
                ui.add(egui::Label::new(&d.summary).wrap());
            });
        }
        if d.syllabus.is_empty() && d.summary.is_empty() {
            ui.weak("No syllabus or questions presented in the bulk data for this case.");
        }
        if !d.headmatter.is_empty() {
            egui::CollapsingHeader::new(RichText::new("Headmatter (parties, docket, dates, counsel)").strong())
                .default_open(false)
                .show(ui, |ui| {
                    ui.add(egui::Label::new(&d.headmatter).wrap());
                });
        }
    }
    if let Some(n) = note {
        let fm = app.files.front_matter(&n.path);
        if !fm.is_empty() {
            egui::CollapsingHeader::new(RichText::new("Note front matter").strong()).default_open(false).show(ui, |ui| {
                egui::Grid::new("frontmatter").num_columns(2).spacing([12.0, 2.0]).show(ui, |ui| {
                    for (k, v) in fm.iter() {
                        ui.weak(k);
                        ui.add(egui::Label::new(v).wrap());
                        ui.end_row();
                    }
                });
            });
        }
    }
}

/// Citing cases per year as a strip of bars, the busiest year full height.
fn year_bars(ui: &mut Ui, years: &[(i64, i64)]) {
    let (first, last) = (years[0].0, years[years.len() - 1].0);
    let n = (last - first + 1).max(1) as f32;
    let w = (n * 6.0).clamp(120.0, 600.0);
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(w, 44.0), egui::Sense::hover());
    let max = years.iter().map(|y| y.1).max().unwrap_or(1).max(1) as f32;
    let col = ui.visuals().selection.bg_fill;
    let bw = rect.width() / n;
    for (y, c) in years {
        let x = rect.left() + (y - first) as f32 * bw;
        let h = rect.height() * (*c as f32 / max);
        ui.painter().rect_filled(
            egui::Rect::from_min_max(egui::pos2(x, rect.bottom() - h), egui::pos2(x + (bw - 1.0).max(1.0), rect.bottom())),
            0.0,
            col,
        );
    }
    if let Some(p) = resp.hover_pos() {
        let y = first + ((p.x - rect.left()) / bw).floor() as i64;
        if let Some((_, c)) = years.iter().find(|(yy, _)| *yy == y) {
            resp.on_hover_text(format!("{y}: {c} citing cases"));
        }
    }
}

// ================================================================== cited together ("where used")

/// "＋" adds a case to the cited-together filter; "✓" (already in it) takes it out.
fn together_button(app: &mut App, ui: &mut Ui, r: i64, name: &str) {
    let inside = app.cocite.iter().any(|c| c.0 == r);
    let (text, tip) = if inside {
        ("✓", "in the cited-together filter; click to take it out")
    } else {
        ("＋", "add to the cited-together filter: find decisions that cite this case and every other case in the filter")
    };
    if ui.small_button(text).on_hover_text(tip).clicked() {
        if inside {
            app.cocite.retain(|c| c.0 != r);
        } else {
            app.cocite.push((r, name.to_string()));
        }
    }
}

fn cocite_page_of(app: &App) -> Option<Page> {
    if app.cocite.is_empty() {
        return None;
    }
    let mut refs: Vec<i64> = app.cocite.iter().map(|c| c.0).collect();
    refs.sort();
    refs.dedup();
    Some(Page::CoCited(refs))
}

/// The filter in the search side panel.
fn cocite_box(app: &mut App, ui: &mut Ui) {
    ui.separator();
    ui.strong("Cited together");
    if app.cocite.is_empty() {
        ui.weak("Click ＋ beside a case (search results, case page) to add it. Find then lists the decisions that cite every case added: one case is plain where-used.");
        return;
    }
    let mut drop = None;
    for (r, name) in &app.cocite {
        ui.horizontal(|ui| {
            if ui.small_button("×").on_hover_text("take it out").clicked() {
                drop = Some(*r);
            }
            let t = if name.chars().count() > 40 { format!("{}…", name.chars().take(39).collect::<String>()) } else { name.clone() };
            if ui.link(t).on_hover_text(name).clicked() {
                app.goto = Some(Page::Case(*r));
            }
        });
    }
    if let Some(r) = drop {
        app.cocite.retain(|c| c.0 != r);
    }
    ui.horizontal(|ui| {
        let n = app.cocite.len();
        let label = if n == 1 { "Find decisions citing it".to_string() } else { format!("Find decisions citing all {n}") };
        if ui.button(label).clicked() {
            app.goto = cocite_page_of(app);
        }
        if ui.link("Clear").clicked() {
            app.cocite.clear();
        }
    });
}

fn cocited_page(app: &mut App, ui: &mut Ui, c: &CoCited) {
    ui.heading("Cited together");
    let mut go = None;
    let mut without: Option<i64> = None;
    for k in &c.cases {
        ui.horizontal(|ui| {
            if c.cases.len() > 1 && ui.small_button("×").on_hover_text("search again without this case").clicked() {
                without = Some(k.r#ref);
            }
            if ui.link(RichText::new(&k.name).strong()).clicked() {
                go = Some(Page::Case(k.r#ref));
            }
            ui.weak(&k.cite);
            if k.cache_citing > 0 {
                ui.weak(format!("· cited by {} decisions", k.cache_citing));
            }
        });
    }
    ui.weak(format!(
        "{} decisions cite {}{}. From cocite.py's edge cache (the edges the co-citation map is built from: a decision \
         counts once, whichever of its opinions cites), made from the bulk load of {}.",
        c.total,
        if c.cases.len() == 1 { "it".to_string() } else { format!("all {}", c.cases.len()) },
        if c.total > c.rows.len() { format!("; the newest {} are listed", c.rows.len()) } else { String::new() },
        c.stamp.split('|').filter_map(|p| p.split('\'').nth(3)).next().unwrap_or("?")
    ));
    ui.add_space(4.0);
    ui.horizontal(|ui| {
        ui.label("Filter");
        ui.text_edit_singleline(&mut app.filter).on_hover_text("name, citation or court (ca4, scotus, mdd…)");
        // the side-panel filter differs from this page: offer to load it
        if let Some(p) = cocite_page_of(app) {
            // the page's cases are in its (sorted) Page order
            if Page::CoCited(c.cases.iter().map(|k| k.r#ref).collect()) != p
                && ui.button("Search with the side-panel filter").clicked()
            {
                go = Some(p);
            }
        }
    });
    ui.add_space(6.0);
    let f = app.filter.clone();
    let mut rows: Vec<&CoCitedRow> = c.rows.iter().filter(|r| matches(&f, &r.name) || matches(&f, &r.cite) || matches(&f, &r.court)).collect();
    let (col, desc) = app.cocite_sort;
    rows.sort_by(|a, b| {
        let o = match col {
            0 => a.date.cmp(&b.date),
            1 => a.name.to_lowercase().cmp(&b.name.to_lowercase()),
            2 => a.court.cmp(&b.court),
            _ => a.cl_cited.cmp(&b.cl_cited),
        };
        if desc { o.reverse() } else { o }
    });
    let shown = rows.len().min(MAX_ROWS);
    if rows.len() > MAX_ROWS {
        ui.weak(format!("Showing {MAX_ROWS} of {}; filter or sort to see others.", rows.len()));
    } else if !f.is_empty() {
        ui.weak(format!("{} match the filter.", rows.len()));
    }
    let mut sort = None;
    let mut header = |ui: &mut Ui, t: &str, k: usize, tip: &str| {
        let mark = if col == k { if desc { " ▼" } else { " ▲" } } else { "" };
        if ui.link(RichText::new(format!("{t}{mark}")).strong()).on_hover_text(tip).clicked() {
            sort = Some(k);
        }
    };
    egui::Grid::new("cocited").striped(true).num_columns(6).show(ui, |ui| {
        header(ui, "Decision", 1, "sort by name");
        ui.label("");
        ui.strong("Cite");
        header(ui, "Filed", 0, "sort by date filed");
        header(ui, "Court", 2, "sort by court");
        header(ui, "Cited by", 3, "CourtListener's citing count for the decision");
        ui.end_row();
        for r in rows.iter().take(shown) {
            let t = if r.name.chars().count() > 110 { format!("{}…", r.name.chars().take(109).collect::<String>()) } else { r.name.clone() };
            if ui.link(t).on_hover_text(&r.name).clicked() {
                go = Some(Page::Case(r.r#ref));
            }
            built_tag(app, ui, r.r#ref);
            ui.label(&r.cite);
            ui.label(&r.date);
            ui.label(&r.court);
            ui.label(r.cl_cited.map(|n| n.to_string()).unwrap_or_default());
            ui.end_row();
        }
    });
    if let Some(k) = sort {
        app.cocite_sort = if col == k { (k, !desc) } else { (k, k != 1 && k != 2) };
    }
    if let Some(r) = without {
        let mut refs: Vec<i64> = c.cases.iter().map(|k| k.r#ref).filter(|x| *x != r).collect();
        refs.sort();
        go = Some(Page::CoCited(refs));
    }
    if go.is_some() {
        app.goto = go;
    }
}

/// The two law checkboxes beside every Build button (case, docket, RECAP), the same everywhere: "Print the law
/// cited" (off: --no-law) and "Fetch state law" (--fetch-state-law; greyed out when nothing is printed).
/// `what`: "case", "filings" or "documents", for the hover text.
pub fn law_checkboxes(ui: &mut egui::Ui, app: &mut App, what: &str) {
    ui.checkbox(&mut app.print_law, "Print the law cited").on_hover_text(format!(
        "end the PDF with The Law Cited: every statute, constitutional provision and regulation the {what} cite, with \
         its text where it is stored here; each cite links to it. Off: --no-law (no appendix, nothing fetched; the \
         note keeps its list)."
    ));
    ui.add_enabled(app.print_law, egui::Checkbox::new(&mut app.fetch_state_law, "Fetch state law")).on_hover_text(format!(
        "also download and build the state law the {what} cite that is not stored here (--fetch-state-law), so The Law \
         Cited prints its text. Each state's source is asked once per title or chapter, then it is local; codes with no \
         usable source (Purdon's, LexisNexis-only states) print a source error instead."
    ));
}
