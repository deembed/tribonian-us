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
    ui.heading("View Cases");
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
                                None => {
                                    ui.add(egui::Label::new(RichText::new(&n.title).weak()).truncate())
                                        .on_hover_text("not a case in _index.json (a docket, filing or cited-by note)");
                                }
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
        _ => {}
    }
    if let Some(e) = app.errors.get(&page) {
        ui.colored_label(Color32::LIGHT_RED, format!("Could not load this page: {e}"));
        return;
    }
    let Some(data) = app.cache.remove(&page) else {
        ui.horizontal(|ui| {
            ui.spinner();
            ui.label(if app.meta.is_none() { "Opening the view…" } else { "Loading…" });
        });
        if let Some(e) = &app.view_err {
            ui.colored_label(Color32::LIGHT_RED, e);
        }
        return;
    };
    egui::ScrollArea::both().auto_shrink([false, false]).show(ui, |ui| {
        ui.style_mut().wrap_mode = Some(egui::TextWrapMode::Extend);
        match &data {
            PageData::Areas(rows) => areas(app, ui, rows),
            PageData::Area { area, name, label, first, last, lineages, core } => {
                area_page(app, ui, *area, name, label, *first, *last, lineages, core)
            }
            PageData::Lineage { lineage, level, name, label, anchor, area, cells, merged_in, split_out } => {
                lineage_page(app, ui, *lineage, *level, name, label, anchor, area, cells, merged_in, split_out)
            }
            PageData::Slice { slice, clusters } => slice_page(app, ui, *slice, clusters),
            PageData::Unit { head, children, cases } => unit_page(app, ui, head, children, cases),
            PageData::Case(c) => case_page(app, ui, c),
        }
    });
    app.cache.insert(page, data);
}

fn home(app: &mut App, ui: &mut Ui) {
    ui.add_space(30.0);
    ui.vertical_centered(|ui| {
        ui.heading(RichText::new("What do you want to do?").size(26.0));
        ui.add_space(24.0);
        let size = egui::vec2(320.0, 48.0);
        if ui.add_sized(size, egui::Button::new(RichText::new("View Clusters").size(18.0))).clicked() {
            app.goto = Some(Page::Areas);
        }
        ui.add_space(8.0);
        if ui.add_sized(size, egui::Button::new(RichText::new("View Cases").size(18.0))).clicked() {
            app.goto = Some(Page::Cases);
        }
        ui.add_space(8.0);
        ui.add_enabled(false, egui::Button::new(RichText::new("Search for Cases").size(18.0)).min_size(size))
            .on_disabled_hover_text("Planned for v1");
        ui.add_space(8.0);
        ui.add_enabled(false, egui::Button::new(RichText::new("Sub-Cluster Search (draft)").size(18.0)).min_size(size))
            .on_disabled_hover_text("TBD: find cases to cite from a draft document");
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
    ui.heading("View Cases");
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
    ui.label("Sub-cluster lineages pooled over all slices (cocite.py areas). Heaviest first.");
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
) {
    ui.weak(format!("Area {}", area_code(area)));
    ui.heading(name);
    status_badge(ui, label);
    if let Some(l) = label {
        ui.add(egui::Label::new(&l.description).wrap());
    }
    ui.weak(format!("{} – {}", app.slice_label(first), app.slice_label(last)));
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
    case_grid(app, ui, "core", core, false);
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
        "Cases ({} in this {}{shown}), by weight{}",
        u.size,
        if u.level == Level::Cluster { "cluster" } else { "sub-cluster" },
        if u.level == Level::Cluster { ". Home cluster, or another cluster holding a share of their ties" } else { "" }
    ));
    case_grid(app, ui, "cases", cases, true);
}

fn case_grid(app: &mut App, ui: &mut Ui, id: &str, cases: &[CaseRow], weights: bool) {
    let f = app.filter.clone();
    egui::Grid::new(id).striped(true).num_columns(6).show(ui, |ui| {
        for c in cases.iter().filter(|c| matches(&f, &c.name) || matches(&f, &c.cite)) {
            if ui.link(&c.name).clicked() {
                app.goto = Some(Page::Case(c.r#ref));
            }
            built_tag(app, ui, c.r#ref);
            ui.label(&c.cite);
            ui.label(year(c.year));
            if weights {
                ui.label(format!("{:.1}", c.weight));
            } else {
                ui.label("");
            }
            ui.label(RichText::new(&c.note).small());
            ui.end_row();
        }
    });
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
        _ => {
            ui.heading(&c.name);
        }
    }
    ui.label(format!("{} {}", c.cite, c.year.map(|y| format!("({y})")).unwrap_or_default()));
    if let Some(e) = &entry {
        if !e.court.is_empty() {
            ui.weak(format!("court: {}", e.court));
        }
    }
    ui.weak(format!("CourtListener cluster {}", c.r#ref));
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
            ui.weak("No note built for this case (building from here comes in v2).");
        }
    });

    // good law
    if let Some(g) = &c.goodlaw {
        let col = match g.status.as_str() {
            "green" => Color32::from_rgb(70, 160, 90),
            "yellow" => Color32::from_rgb(210, 170, 40),
            "red" => Color32::from_rgb(210, 70, 60),
            _ => Color32::GRAY,
        };
        ui.horizontal(|ui| {
            ui.label("Good law:");
            ui.colored_label(col, RichText::new(&g.status).strong());
            if let Some(y) = g.red_year {
                ui.label(format!("red since {y}"));
            } else if let Some(y) = g.yellow_year {
                ui.label(format!("yellow since {y}"));
            }
            if let (Some(r), Some(o)) = (g.read, g.of) {
                ui.weak(format!("read {r} of {o} citing opinions"));
            }
            if let Some(s) = &g.stamp {
                ui.weak(format!("data {s}"));
            }
        });
    } else {
        ui.weak("Good law: not computed (casemap.py goodlaw writes it).");
    }
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
