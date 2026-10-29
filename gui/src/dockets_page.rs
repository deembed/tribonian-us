//! The View Dockets section: Federal or State, then the court, then its dockets, then one docket.
//! Federal courts of appeals, district, bankruptcy and special courts read RECAP (tribonian case recap);
//! the Supreme Court is the SCOTUS section; state courts are laid out but not wired yet (TBD).
//! Every action queues a script through the build queue; the app writes nothing itself.

use crate::cases::{obsidian_url, open_external};
use crate::dockets::{self, Court, CourtTree, RecapDocket};
use crate::model::Page;
use crate::{App, Queued, QueuedJob};
use eframe::egui::{self, Color32, RichText, Ui};
use std::collections::BTreeSet;

#[derive(Clone, Copy, PartialEq, Eq, Default)]
pub enum Show {
    #[default]
    All,
    Open,
    Closed,
}

#[derive(Default)]
pub struct DocketsUi {
    /// the court the "Add a docket" box fetches from
    pub court: String,
    pub number: String,
    pub err: Option<String>,
    pub show: Show,
    /// documents ticked on a docket page: (docket id, document keys)
    pub sel: (i64, BTreeSet<String>),
    pub search_input: String,
    /// a court's opinions: pages shown per court (20 each); "Show more" adds one
    pub op_pages: std::collections::HashMap<String, u32>,
    /// federal court pages: the opinions section opened (it may spend an API request)
    pub op_open: BTreeSet<String>,
    /// CourtListener result pages shown per query (20 hits each); "Show more" adds one
    pub pages: std::collections::HashMap<String, u32>,
}

const TBD: &str = "TBD: not wired up yet";
const STATE_HELP: &str = "RECAP holds federal dockets only (it is built from PACER purchases). A state court's dockets are \
                          on that state's own portal, often paywalled or closed to scripts, so they come later. State \
                          appellate opinions are in the local database already; listing them here is next.";

fn shown(show: Show, d: &RecapDocket) -> bool {
    match show {
        Show::All => true,
        Show::Open => d.open(),
        Show::Closed => !d.open(),
    }
}

fn show_toggle(app: &mut App, ui: &mut Ui) {
    ui.horizontal(|ui| {
        let s = &mut app.dockets_ui.show;
        ui.selectable_value(s, Show::All, "All");
        ui.selectable_value(s, Show::Open, "Open").on_hover_text("live: no termination date");
        ui.selectable_value(s, Show::Closed, "Closed").on_hover_text("decided or otherwise terminated");
    });
}

fn link_text(ui: &Ui, t: String, strong: bool) -> RichText {
    let r = RichText::new(t).color(ui.visuals().hyperlink_color);
    if strong { r.strong() } else { r }
}

fn queue(app: &mut App, key: i64, title: String, steps: Vec<(Vec<String>, Option<String>)>) {
    app.enqueue(Queued { r#ref: key, title, job: QueuedJob::Steps(steps) });
}

fn s(x: &str) -> String {
    x.to_string()
}

/// Fetch a docket by court and case number; its page appears in the court's list once fetched.
fn queue_add(app: &mut App, court: &str, number: &str) {
    let args = vec![s("case"), s("recap"), s("fetch"), s("--court"), s(court), s("--number"), s(number)];
    queue(app, dockets::add_key(court, number), format!("{court} No. {number}: fetch"), vec![(args, Some(s("saved ")))]);
}

pub fn queue_courts(app: &mut App) {
    queue(app, dockets::COURTS_KEY, s("Court list"), vec![(vec![s("dockets"), s("courts")], None)]);
}

fn court_name(app: &App, id: &str) -> String {
    app.courts.as_ref().ok().and_then(|t| t.as_ref()).and_then(|t| t.court(id)).map(|c| c.name.clone()).unwrap_or_else(|| id.to_string())
}

/// A note (relative to the work folder) in Obsidian when the vault is set, else the file itself.
fn open_note(app: &App, rel: &str) {
    let Some(cfg) = &app.cfg else { return };
    let full = cfg.work_dir.join(rel);
    let note = app.files.folders.values().flatten().find(|n| n.path == full);
    match note.and_then(|n| obsidian_url(cfg, n)) {
        Some(u) => open_external(&u),
        None => open_external(&full.to_string_lossy()),
    }
}

fn open_file(app: &App, rel: &str) {
    if let Some(cfg) = &app.cfg {
        open_external(&cfg.work_dir.join(rel).to_string_lossy());
    }
}

/// The "Add a docket" box: a federal court (fixed on a court's page) and its case number.
fn add_box(app: &mut App, ui: &mut Ui, fixed: Option<&str>) {
    let courts: Vec<Court> = app.courts.as_ref().ok().and_then(|t| t.as_ref()).map(|t| t.recap_courts().cloned().collect()).unwrap_or_default();
    if let Some(c) = fixed {
        app.dockets_ui.court = c.to_string();
    }
    ui.horizontal(|ui| {
        if fixed.is_none() {
            if courts.is_empty() {
                ui.add(egui::TextEdit::singleline(&mut app.dockets_ui.court).hint_text("court id, e.g. ca4").desired_width(90.0))
                    .on_hover_text("CourtListener's court id: ca1–ca11, cadc, cafc; district courts like mdd (D. Md.), nysd (S.D.N.Y.)");
            } else {
                let cur = courts.iter().find(|c| c.id == app.dockets_ui.court).map(|c| c.cite.clone()).filter(|c| !c.is_empty());
                egui::ComboBox::from_id_salt("add-court")
                    .selected_text(cur.unwrap_or_else(|| if app.dockets_ui.court.is_empty() { s("court") } else { app.dockets_ui.court.clone() }))
                    .width(110.0)
                    .show_ui(ui, |ui| {
                        for c in &courts {
                            let label = if c.cite.is_empty() { c.name.clone() } else { format!("{} — {}", c.cite, c.name) };
                            ui.selectable_value(&mut app.dockets_ui.court, c.id.clone(), label);
                        }
                    });
            }
        }
        let r = ui.add(egui::TextEdit::singleline(&mut app.dockets_ui.number).hint_text("case no., e.g. 26-2230").desired_width(130.0));
        let enter = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
        if ui
            .button("Fetch")
            .on_hover_text("queues tribonian case recap fetch --court <court> --number <no.>: the docket's entries and the text of the court's orders and opinions RECAP holds")
            .clicked()
            || enter
        {
            let (court, number) = (app.dockets_ui.court.trim().to_lowercase(), app.dockets_ui.number.trim().to_string());
            if court.is_empty() || number.is_empty() {
                app.dockets_ui.err = Some(s("Pick a court and type its case number, as the court writes it."));
            } else {
                app.dockets_ui.err = None;
                app.dockets_ui.number.clear();
                queue_add(app, &court, &number);
                app.goto = Some(Page::CourtDockets(court));
            }
        }
    });
    if let Some(e) = &app.dockets_ui.err {
        ui.colored_label(Color32::LIGHT_RED, e);
    }
}

// ------------------------------------------------------------------ side panel

/// The "Search dockets" box: party or case name words, or a case number.
fn search_box(app: &mut App, ui: &mut Ui) {
    ui.horizontal(|ui| {
        let r = ui.add(egui::TextEdit::singleline(&mut app.dockets_ui.search_input).hint_text("party, case name or number").desired_width(200.0));
        let enter = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
        if (ui.button("Search").clicked() || enter) && !app.dockets_ui.search_input.trim().is_empty() {
            app.goto = Some(Page::DocketSearch(app.dockets_ui.search_input.trim().to_string()));
        }
    });
}

pub fn dockets_side(app: &mut App, ui: &mut Ui) {
    ui.heading("Dockets");
    ui.label("Search dockets");
    search_box(app, ui);
    ui.add_space(4.0);
    ui.label("Add a federal docket");
    add_box(app, ui, None);
    ui.separator();
    if ui.link("Federal or State").clicked() {
        app.goto = Some(Page::Dockets);
    }
    ui.add_space(4.0);
    show_toggle(app, ui);
    let here = match app.current() {
        Page::RecapDocket(id) => Some(*id),
        _ => None,
    };
    let show = app.dockets_ui.show;
    let list: Vec<(i64, String, String)> =
        app.recap.iter().filter(|d| shown(show, d)).map(|d| (d.id, format!("{} {}  {}", d.court, d.number, d.name), d.status())).collect();
    ui.strong(format!("Fetched dockets ({})", list.len()));
    let mut go = None;
    egui::ScrollArea::vertical().id_salt("recap-dockets").auto_shrink([false, false]).show(ui, |ui| {
        for (id, t, st) in list {
            let text = link_text(ui, t, here == Some(id));
            if ui.add(egui::Label::new(text).truncate().sense(egui::Sense::click())).on_hover_text(st).clicked() {
                go = Some(Page::RecapDocket(id));
            }
        }
    });
    if go.is_some() {
        app.goto = go;
    }
}

// ------------------------------------------------------------------ Federal or State

fn tree_status(app: &mut App, ui: &mut Ui) -> Option<CourtTree> {
    let tree = match &app.courts {
        Ok(Some(t)) => Some(t.clone()),
        Ok(None) => {
            ui.label("The court list isn't built yet: cases/dockets/_courts.json doesn't exist.");
            None
        }
        Err(e) => {
            ui.colored_label(Color32::LIGHT_RED, e);
            None
        }
    };
    ui.horizontal(|ui| {
        if let Some(t) = &tree {
            ui.weak(format!("court list from the {}, made {}", t.source, t.made));
        }
        let queued = app.is_queued(dockets::COURTS_KEY);
        if ui
            .add_enabled(!queued, egui::Button::new(if tree.is_some() { "Refresh court list" } else { "Build court list" }).small())
            .on_hover_text("queues tribonian dockets courts: every court from the local courts table (tribonian db load courts), else CourtListener's API")
            .on_disabled_hover_text("in the build queue")
            .clicked()
        {
            queue_courts(app);
        }
    });
    tree
}

pub fn dockets_home(app: &mut App, ui: &mut Ui) {
    ui.add_space(20.0);
    ui.vertical_centered(|ui| {
        ui.heading(RichText::new("View Dockets").size(26.0));
        ui.add_space(20.0);
        let size = egui::vec2(320.0, 48.0);
        if ui
            .add_sized(size, egui::Button::new(RichText::new("Federal").size(18.0)))
            .on_hover_text("Supreme Court, courts of appeals, district, bankruptcy and special courts")
            .clicked()
        {
            app.goto = Some(Page::DocketsFederal);
        }
        ui.add_space(8.0);
        if ui
            .add_sized(size, egui::Button::new(RichText::new("State").size(18.0)))
            .on_hover_text("each state's highest, appellate and trial courts")
            .clicked()
        {
            app.goto = Some(Page::DocketsState(None));
        }
        ui.add_space(20.0);
        tree_status(app, ui);
        ui.weak(format!("{} federal dockets fetched", app.recap.len()));
    });
}

fn fetched_in(app: &App, court: &str) -> usize {
    app.recap.iter().filter(|d| d.court == court).count()
}

fn court_links(app: &mut App, ui: &mut Ui, courts: &[Court]) {
    let mut go = None;
    egui::Grid::new(ui.next_auto_id()).striped(true).num_columns(3).show(ui, |ui| {
        for c in courts {
            ui.label(if c.cite.is_empty() { &c.id } else { &c.cite });
            if ui.link(&c.name).clicked() {
                go = Some(Page::CourtDockets(c.id.clone()));
            }
            let n = fetched_in(app, &c.id);
            ui.weak(if n > 0 { format!("{n} fetched") } else { String::new() });
            ui.end_row();
        }
    });
    if go.is_some() {
        app.goto = go;
    }
}

pub fn federal_page(app: &mut App, ui: &mut Ui) {
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        ui.heading("Federal courts");
        let Some(t) = tree_status(app, ui) else { return };
        ui.add_space(8.0);
        ui.strong("Supreme Court");
        if ui.link("Supreme Court of the United States").on_hover_text("the SCOTUS section: dockets fetched with tribonian scotus").clicked() {
            app.goto = Some(Page::Scotus);
        }
        ui.add_space(8.0);
        ui.strong(format!("Courts of Appeals ({})", t.federal.appeals.len()));
        court_links(app, ui, &t.federal.appeals);
        for (title, list, open) in [
            ("District Courts", &t.federal.district, true),
            ("Bankruptcy Courts", &t.federal.bankruptcy, false),
            ("Special Courts", &t.federal.special, false),
        ] {
            ui.add_space(6.0);
            egui::CollapsingHeader::new(RichText::new(format!("{title} ({})", list.len())).strong())
                .id_salt(title)
                .default_open(open)
                .show(ui, |ui| court_links(app, ui, list));
        }
    });
}

// ------------------------------------------------------------------ states

pub fn state_page(app: &mut App, ui: &mut Ui, code: &Option<String>) {
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        let Some(code) = code else {
            ui.heading("State courts");
            let Some(t) = tree_status(app, ui) else { return };
            ui.weak(STATE_HELP);
            ui.add_space(8.0);
            let mut go = None;
            egui::Grid::new("states").striped(true).num_columns(2).show(ui, |ui| {
                for (code, st) in t.states() {
                    if ui.link(&st.name).clicked() {
                        go = Some(Page::DocketsState(Some(code.clone())));
                    }
                    let n = st.highest.len() + st.appellate.len() + st.trial.len() + st.special.len();
                    ui.weak(format!("{n} courts"));
                    ui.end_row();
                }
            });
            if go.is_some() {
                app.goto = go;
            }
            return;
        };
        let Some(t) = tree_status(app, ui) else { return };
        let Some(st) = t.state.get(code) else {
            ui.label(format!("No state {code} in the court list."));
            return;
        };
        ui.heading(&st.name);
        ui.weak(STATE_HELP);
        for (title, list) in [
            ("Highest court", &st.highest),
            ("Appellate courts", &st.appellate),
            ("Trial courts (county, city, parish)", &st.trial),
            ("Special courts", &st.special),
        ] {
            ui.add_space(8.0);
            ui.strong(title);
            if list.is_empty() {
                ui.weak(format!("none in the court list · {TBD}"));
                continue;
            }
            court_links(app, ui, list);
        }
    });
}

// ------------------------------------------------------------------ one court

fn is_state_court(t: &CourtTree, id: &str) -> bool {
    t.state.values().any(|s| s.highest.iter().chain(&s.appellate).chain(&s.trial).chain(&s.special).any(|c| c.id == id))
}

pub fn court_page(app: &mut App, ui: &mut Ui, id: &str) {
    if id == "scotus" {
        app.goto = Some(Page::Scotus);
        return;
    }
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        ui.heading(court_name(app, id));
        let state = app.courts.as_ref().ok().and_then(|t| t.as_ref()).is_some_and(|t| is_state_court(t, id));
        if state {
            opinions_section(app, ui, id);
            ui.add_space(10.0);
            ui.strong("Dockets");
            ui.weak(format!("{TBD}. {STATE_HELP}"));
            return;
        }
        let open = app.dockets_ui.op_open.contains(id);
        let r = egui::CollapsingHeader::new(RichText::new("Opinions").strong()).id_salt(("ops", id)).open(Some(open)).show(ui, |ui| {
            opinions_section(app, ui, id)
        });
        if r.header_response.clicked() {
            if open {
                app.dockets_ui.op_open.remove(id);
            } else {
                app.dockets_ui.op_open.insert(id.to_string());
            }
        }
        ui.add_space(8.0);
        ui.label("Add a docket from this court");
        add_box(app, ui, Some(id));
        ui.add_space(8.0);
        show_toggle(app, ui);
        let show = app.dockets_ui.show;
        let rows: Vec<RecapDocket> = app.recap.iter().filter(|d| d.court == id && shown(show, d)).cloned().collect();
        ui.strong(format!("Fetched dockets ({})", rows.len()));
        if rows.is_empty() {
            ui.weak("None here yet. A fetch in the queue shows up once it finishes.");
            return;
        }
        let mut go = None;
        egui::Grid::new("court-dockets").striped(true).num_columns(5).show(ui, |ui| {
            for h in ["No.", "Case", "Status", "Documents", "Built"] {
                ui.strong(h);
            }
            ui.end_row();
            for d in &rows {
                ui.label(&d.number);
                if ui.link(&d.name).clicked() {
                    go = Some(Page::RecapDocket(d.id));
                }
                ui.label(d.status());
                ui.label(format!("{} with text · {} in RECAP · {} PACER only", d.count("text"), d.count("recap"), d.count("pacer")));
                ui.label(match (d.note.is_empty(), d.pdfs.is_empty()) {
                    (false, false) => "note + PDF",
                    (false, true) => "note",
                    (true, false) => "PDF",
                    _ => "–",
                });
                ui.end_row();
            }
        });
        if go.is_some() {
            app.goto = go;
        }
    });
}

// ------------------------------------------------------------------ one docket

fn mark(status: &str) -> (&'static str, &'static str) {
    match status {
        "text" => ("✓", "fetched: its text is saved, so Build can use it"),
        "recap" => ("○", "in RECAP, not fetched yet: Fetch selected gets it (free)"),
        _ => ("$", "not in RECAP: only on PACER, which charges per page"),
    }
}

pub fn docket_page(app: &mut App, ui: &mut Ui, id: i64) {
    let Some(d) = app.recap.iter().find(|d| d.id == id).cloned() else {
        ui.label(format!("Docket {id} isn't fetched: cases/recap/_index.json doesn't list it."));
        return;
    };
    if app.dockets_ui.sel.0 != id {
        app.dockets_ui.sel = (id, BTreeSet::new());
    }
    let key = dockets::docket_key(id);
    let queued = app.is_queued(key);
    egui::ScrollArea::both().auto_shrink([false, false]).show(ui, |ui| {
        ui.heading(format!("{}, No. {}", d.name, d.number));
        ui.label(format!("{} · {}{}", court_name(app, &d.court), d.status(), if d.nature.is_empty() { String::new() } else { format!(" · {}", d.nature) }));
        ui.horizontal(|ui| {
            if !d.courtlistener.is_empty() && ui.button("CourtListener").on_hover_text(&d.courtlistener).clicked() {
                open_external(&d.courtlistener);
            }
            if !d.note.is_empty() && ui.button("Open docket note").on_hover_text(&d.note).clicked() {
                open_note(app, &d.note);
            }
            if ui
                .add_enabled(!queued, egui::Button::new("Fetch updates"))
                .on_hover_text(format!("queues tribonian case recap fetch --docket {id}: new entries, and the text of the court's orders and opinions"))
                .on_disabled_hover_text("in the build queue")
                .clicked()
            {
                let args = vec![s("case"), s("recap"), s("fetch"), s("--docket"), id.to_string()];
                queue(app, key, format!("{} {}: fetch", d.court, d.number), vec![(args, Some(s("saved ")))]);
            }
        });
        if !d.pdfs.is_empty() {
            ui.horizontal_wrapped(|ui| {
                ui.label("Built PDFs:");
                for p in &d.pdfs {
                    let name = p.rsplit('/').next().unwrap_or(p).to_string();
                    if ui.link(name).on_hover_text(p).clicked() {
                        open_file(app, p);
                    }
                }
            });
        }
        ui.separator();

        // actions on the ticked documents
        let sel: Vec<String> = app.dockets_ui.sel.1.iter().cloned().collect();
        let law = app.law_args();
        ui.horizontal(|ui| {
            crate::pages::law_checkboxes(ui, app, "documents");
        });
        ui.horizontal(|ui| {
            let fetchable: Vec<String> = sel.iter().filter(|k| d.documents.iter().any(|x| &x.key == *k && x.status == "recap")).cloned().collect();
            let buildable: Vec<String> = sel.iter().filter(|k| d.documents.iter().any(|x| &x.key == *k && x.status == "text")).cloned().collect();
            if ui
                .add_enabled(!queued && !fetchable.is_empty(), egui::Button::new(format!("Fetch selected ({})", fetchable.len())))
                .on_hover_text("queues tribonian case recap fetch --docket <id> --docs <the ticked ○ documents>")
                .on_disabled_hover_text(if queued { "in the build queue" } else { "tick documents marked ○ (in RECAP, not fetched)" })
                .clicked()
            {
                let args = vec![s("case"), s("recap"), s("fetch"), s("--docket"), id.to_string(), s("--docs"), fetchable.join(",")];
                queue(app, key, format!("{} {}: fetch {}", d.court, d.number, fetchable.join(",")), vec![(args, Some(s("saved ")))]);
                app.dockets_ui.sel.1.clear();
            }
            if ui
                .add_enabled(!queued && !buildable.is_empty(), egui::Button::new(format!("Build selected ({})", buildable.len())))
                .on_hover_text("queues tribonian case recap build <file> --docs <the ticked ✓ documents>: one PDF and a note per document")
                .on_disabled_hover_text(if queued { "in the build queue" } else { "tick documents marked ✓ (text saved)" })
                .clicked()
            {
                let args = [vec![s("case"), s("recap"), s("build"), d.file.clone(), s("--docs"), buildable.join(",")], law.clone()].concat();
                queue(app, key, format!("{} {}: build {}", d.court, d.number, buildable.join(",")), vec![(args, Some(s("wrote ")))]);
                app.dockets_ui.sel.1.clear();
            }
            let n = d.count("text");
            if ui
                .add_enabled(!queued && n > 0, egui::Button::new(format!("Build all ({n})")))
                .on_hover_text("queues tribonian case recap build <file>: every document with text saved, in one PDF")
                .on_disabled_hover_text(if queued { "in the build queue" } else { "nothing fetched with text yet" })
                .clicked()
            {
                let args = [vec![s("case"), s("recap"), s("build"), d.file.clone()], law.clone()].concat();
                queue(app, key, format!("{} {}: build all", d.court, d.number), vec![(args, Some(s("wrote ")))]);
            }
        });
        ui.weak("✓ text saved · ○ in RECAP, not fetched (free) · $ PACER only (charged per page)");
        ui.add_space(4.0);

        let mut toggle: Option<(String, bool)> = None;
        let mut open: Option<(bool, String)> = None;
        egui::Grid::new("recap-docs").striped(true).num_columns(7).show(ui, |ui| {
            for h in ["", "", "ECF No.", "Filed", "Pages", "Description", "Note"] {
                ui.strong(h);
            }
            ui.end_row();
            for x in &d.documents {
                let (m, why) = mark(&x.status);
                if x.status == "pacer" {
                    ui.label("");
                } else {
                    let mut on = app.dockets_ui.sel.1.contains(&x.key);
                    if ui.checkbox(&mut on, "").changed() {
                        toggle = Some((x.key.clone(), on));
                    }
                }
                if x.status == "pacer" && !x.pacer.is_empty() {
                    if ui.link(RichText::new("$ PACER").color(Color32::from_rgb(200, 140, 40))).on_hover_text(format!("{why}\n{}", x.pacer)).clicked() {
                        open = Some((false, x.pacer.clone()));
                    }
                } else {
                    ui.label(m).on_hover_text(why);
                }
                let k = if x.order { RichText::new(&x.key).strong() } else { RichText::new(&x.key) };
                if !x.courtlistener.is_empty() {
                    if ui.link(k).on_hover_text(if x.order { "the court's own writing (order, opinion, judgment)" } else { "open on CourtListener" }).clicked() {
                        open = Some((false, x.courtlistener.clone()));
                    }
                } else {
                    ui.label(k);
                }
                ui.label(&x.filed);
                ui.label(x.pages.map(|p| p.to_string()).unwrap_or_default());
                let desc: String = x.description.chars().take(110).collect();
                ui.label(desc).on_hover_text(&x.description);
                if x.note.is_empty() {
                    ui.label("");
                } else if ui.link("note").on_hover_text(&x.note).clicked() {
                    open = Some((true, x.note.clone()));
                }
                ui.end_row();
            }
        });
        if let Some((k, on)) = toggle {
            if on {
                app.dockets_ui.sel.1.insert(k);
            } else {
                app.dockets_ui.sel.1.remove(&k);
            }
        }
        match open {
            Some((true, rel)) => open_note(app, &rel),
            Some((false, url)) => open_external(&url),
            None => {}
        }
    });
}

// ------------------------------------------------------------------ Search Dockets

pub fn search_page(app: &mut App, ui: &mut Ui, query: &str) {
    egui::ScrollArea::both().auto_shrink([false, false]).show(ui, |ui| {
        ui.heading("Search Dockets");
        if app.dockets_ui.search_input.is_empty() && !query.is_empty() {
            app.dockets_ui.search_input = query.to_string();
        }
        search_box(app, ui);
        ui.weak(if app.online() {
            "The dockets fetched here, then CourtListener's docket search (1 API request per search, reused for a day)."
        } else {
            "Offline mode (top bar): only the dockets fetched here are searched."
        });
        if query.is_empty() {
            return;
        }
        ui.add_space(8.0);
        let pages = app.dockets_ui.pages.get(query).copied().unwrap_or(1);
        app.request_docket_search(query, pages);
        // while more pages load, keep showing the ones already read
        let loading = !app.docket_search.contains_key(&(query.to_string(), pages));
        let shown = (1..=pages).rev().find_map(|p| app.docket_search.get(&(query.to_string(), p)).cloned());
        let Some(res) = shown else {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.label("searching…");
            });
            return;
        };
        let res = match res {
            Ok(r) => r,
            Err(e) => {
                ui.colored_label(Color32::LIGHT_RED, format!("Search failed: {e}"));
                return;
            }
        };
        let mut go = None;
        ui.strong(format!("Fetched here ({})", res.fetched.len()));
        if res.fetched.is_empty() {
            ui.weak("none match");
        }
        for h in &res.fetched {
            ui.horizontal(|ui| {
                ui.label(format!("{} {}", h.court_label(), h.number)).on_hover_text(&h.court);
                if ui.link(&h.name).clicked() {
                    go = Some(Page::RecapDocket(h.id));
                }
                ui.weak(if h.terminated.is_empty() { format!("open · filed {}", h.filed) } else { format!("closed {}", h.terminated) });
            });
        }
        ui.add_space(10.0);
        match &res.api {
            None => {
                ui.strong("CourtListener");
                ui.weak(if res.error.is_empty() { "not searched: Offline mode" } else { &res.error });
            }
            Some(hits) => {
                ui.strong(format!(
                    "CourtListener ({} of {}{})",
                    hits.len(),
                    res.count.map(|c| c.to_string()).unwrap_or_else(|| "?".into()),
                    if res.cached { ", from today's cache" } else { "" }
                ));
                let mut fetch = None;
                egui::Grid::new("docket-hits").striped(true).num_columns(5).show(ui, |ui| {
                    for h in hits {
                        ui.label(h.court_label()).on_hover_text(&h.court);
                        ui.label(&h.number);
                        if !h.courtlistener.is_empty() {
                            if ui.link(&h.name).on_hover_text("open on CourtListener").clicked() {
                                open_external(&h.courtlistener);
                            }
                        } else {
                            ui.label(&h.name);
                        }
                        ui.label(if h.terminated.is_empty() { format!("open · filed {}", h.filed) } else { format!("closed {}", h.terminated) });
                        if h.fetched {
                            if ui.small_button("open").clicked() {
                                go = Some(Page::RecapDocket(h.id));
                            }
                        } else {
                            let queued = app.is_queued(dockets::docket_key(h.id));
                            if ui
                                .add_enabled(!queued, egui::Button::new("Fetch").small())
                                .on_hover_text(format!("queues tribonian case recap fetch --docket {}", h.id))
                                .on_disabled_hover_text("in the build queue")
                                .clicked()
                            {
                                fetch = Some(h.clone());
                            }
                        }
                        ui.end_row();
                    }
                });
                let total = res.count.unwrap_or(0) as usize;
                if loading {
                    ui.horizontal(|ui| {
                        ui.spinner();
                        ui.label("reading the next page…");
                    });
                } else if hits.len() < total {
                    let more = (total - hits.len()).min(20);
                    if ui
                        .button(format!("Show {more} more"))
                        .on_hover_text("reads CourtListener's next page of results: 1 API request (reused for a day)")
                        .clicked()
                    {
                        app.dockets_ui.pages.insert(query.to_string(), pages + 1);
                    }
                }
                if let Some(h) = fetch {
                    let args = vec![s("case"), s("recap"), s("fetch"), s("--docket"), h.id.to_string()];
                    queue(app, dockets::docket_key(h.id), format!("{} {}: fetch", h.court, h.number), vec![(args, Some(s("saved ")))]);
                }
                if !res.error.is_empty() {
                    ui.colored_label(Color32::LIGHT_RED, &res.error);
                }
            }
        }
        if go.is_some() {
            app.goto = go;
        }
    });
}

// ------------------------------------------------------------------ a court's opinions

/// The court's opinions, newest first: the local database when it is indexed by court (tribonian db
/// courtindex), else CourtListener (not offline); Build queues the case's note and PDF, open shows it.
fn opinions_section(app: &mut App, ui: &mut Ui, court: &str) {
    let pages = app.dockets_ui.op_pages.get(court).copied().unwrap_or(1);
    app.request_court_opinions(court, pages);
    let loading = !app.court_opinions.contains_key(&(court.to_string(), pages));
    let shown = (1..=pages).rev().find_map(|p| app.court_opinions.get(&(court.to_string(), p)).cloned());
    ui.strong("Opinions, newest first");
    let Some(res) = shown else {
        ui.horizontal(|ui| {
            ui.spinner();
            ui.label("reading…");
        });
        return;
    };
    let res = match res {
        Ok(r) => r,
        Err(e) => {
            ui.colored_label(Color32::LIGHT_RED, format!("Could not list them: {e}"));
            return;
        }
    };
    if res.source.is_empty() {
        ui.weak(&res.error);
        return;
    }
    ui.weak(match res.source.as_str() {
        "local database" => "from the local database".to_string(),
        _ => format!(
            "from CourtListener (1 API request per page, reused for a day){}; tribonian db courtindex lists them from the local database",
            res.count.map(|n| format!(", {n} in all")).unwrap_or_default()
        ),
    });
    if res.hits.is_empty() {
        ui.weak("none");
        return;
    }
    let mut go = None;
    let mut build = None;
    egui::Grid::new(("court-ops", court)).striped(true).num_columns(5).show(ui, |ui| {
        for h in &res.hits {
            ui.label(&h.date);
            ui.label(&h.name);
            ui.weak(&h.cite);
            ui.weak(if h.cited > 0 { format!("cited {}", h.cited) } else { String::new() });
            if h.built {
                if ui.small_button("open").clicked() {
                    go = Some(Page::Case(h.cluster));
                }
            } else {
                let queued = app.is_queued(h.cluster);
                if ui
                    .add_enabled(!queued, egui::Button::new("Build").small())
                    .on_hover_text("queues the case's note and PDF with the quick good-law check (as Quick build on a case page)")
                    .on_disabled_hover_text("in the build queue")
                    .clicked()
                {
                    build = Some((h.cluster, h.name.clone()));
                }
            }
            ui.end_row();
        }
    });
    if loading {
        ui.horizontal(|ui| {
            ui.spinner();
            ui.label("reading the next page…");
        });
    } else if res.more && ui.button("Show 20 more").clicked() {
        app.dockets_ui.op_pages.insert(court.to_string(), pages + 1);
    }
    if let Some((r, name)) = build {
        app.enqueue_build(r, name, true);
    }
    if go.is_some() {
        app.goto = go;
    }
}
