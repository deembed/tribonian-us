//! The SCOTUS section: dockets scotus.py has fetched, one docket's page, the Court's recent
//! activity (scotus.py recent), and the docket hits shown above case-search results.
//! Every action queues scotus.py through the build queue; the app writes nothing itself.

use crate::cases::{obsidian_url, open_external};
use crate::model::Page;
use crate::scotus::{self, Docket};
use crate::App;
use eframe::egui::{self, Color32, RichText, Ui};

#[derive(Clone, Copy, PartialEq, Eq, Default)]
pub enum Tab {
    #[default]
    Decided,
    Granted,
    Orders,
}

#[derive(Default)]
pub struct ScotusUi {
    pub add_input: String,
    pub add_err: Option<String>,
    pub tab: Tab,
    pub filter: String,
    pub show_denied: bool,
    pub show_decided: bool,
}

const CERT_HELP: &str = "A petition for a writ of certiorari asks the Supreme Court to review a lower court's decision; \
                         the Court grants only a small share (its review is discretionary).";
const NO_PENDING_LIST: &str = "The Court publishes no list of all pending petitions for a writ of certiorari: a petition \
                               shows up in these lists only once the Court acts on it. To follow one, add its docket number.";

fn matches(f: &str, t: &[&str]) -> bool {
    let f = f.trim().to_lowercase();
    f.is_empty() || t.iter().any(|x| x.to_lowercase().contains(&f))
}

/// The "Add docket" box: fetch then build a docket by number.
fn add_box(app: &mut App, ui: &mut Ui) {
    ui.horizontal(|ui| {
        let r = ui.add(egui::TextEdit::singleline(&mut app.scotus_ui.add_input).hint_text("docket number, e.g. 25-579").desired_width(180.0));
        let enter = r.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
        if ui
            .button("Fetch & build")
            .on_hover_text("queues ./scotus.py fetch <docket> (the Court's docket and every filed PDF), then ./scotus.py build <docket> (PDF and notes)")
            .clicked()
            || enter
        {
            match scotus::norm_docket(&app.scotus_ui.add_input) {
                Some(n) => {
                    app.scotus_ui.add_err = None;
                    app.scotus_ui.add_input.clear();
                    app.enqueue_scotus(&n, &["fetch", "build"]);
                    app.goto = Some(Page::Docket(n));
                }
                None => {
                    app.scotus_ui.add_err = Some(
                        "Not a docket number like 25-579. Applications (25A374) and original cases aren't read by scotus.py fetch.".into(),
                    )
                }
            }
        }
    });
    if let Some(e) = &app.scotus_ui.add_err {
        ui.colored_label(Color32::LIGHT_RED, e);
    }
}

pub fn scotus_side(app: &mut App, ui: &mut Ui) {
    ui.heading("SCOTUS");
    ui.label("Add a docket");
    add_box(app, ui);
    ui.weak(NO_PENDING_LIST);
    ui.separator();
    ui.horizontal(|ui| {
        if ui.link("All dockets and recent activity").clicked() {
            app.goto = Some(Page::Scotus);
        }
    });
    ui.add_space(4.0);
    ui.strong(format!("Fetched dockets ({})", app.dockets.len()));
    let here = match app.current() {
        Page::Docket(n) => Some(n.clone()),
        _ => None,
    };
    let mut go = None;
    egui::ScrollArea::vertical().id_salt("dockets").auto_shrink([false, false]).show(ui, |ui| {
        for d in &app.dockets {
            let t = format!("{}  {}", d.number, d.title());
            let text = if here.as_deref() == Some(&d.number) { RichText::new(t).strong() } else { RichText::new(t) };
            if ui.add(egui::Label::new(text.color(ui.visuals().hyperlink_color)).truncate().sense(egui::Sense::click())).on_hover_text(d.status()).clicked() {
                go = Some(Page::Docket(d.number.clone()));
            }
        }
    });
    if go.is_some() {
        app.goto = go;
    }
}

// ------------------------------------------------------------------ the SCOTUS page

pub fn scotus_home(app: &mut App, ui: &mut Ui) {
    egui::ScrollArea::both().auto_shrink([false, false]).show(ui, |ui| {
        ui.heading("Supreme Court");
        ui.label("Dockets fetched with scotus.py, and what the Court has done in the last year.");
        ui.weak(CERT_HELP);
        ui.add_space(8.0);
        dockets_table(app, ui);
        for e in &app.docket_errors {
            ui.colored_label(Color32::LIGHT_RED, e);
        }
        ui.add_space(12.0);
        ui.separator();
        recent_panel(app, ui);
    });
}

fn note_and_pdf(app: &App, d: &Docket) -> (bool, bool) {
    let (note, _) = app.files.docket_notes(&d.number);
    (note.is_some(), note.and_then(|n| n.pdf.as_ref()).is_some())
}

fn dockets_table(app: &mut App, ui: &mut Ui) {
    ui.strong(format!("Fetched dockets ({})", app.dockets.len()));
    if app.dockets.is_empty() {
        ui.weak("None yet: cases/scotus/ holds no <docket>.json. Add a docket number on the left.");
        return;
    }
    let mut go = None;
    egui::Grid::new("dockets").striped(true).num_columns(6).show(ui, |ui| {
        for h in ["Docket", "Case", "Status", "Last entry", "Filings", "Built"] {
            ui.strong(h);
        }
        ui.end_row();
        for d in &app.dockets {
            ui.label(&d.number);
            if ui.link(d.title()).clicked() {
                go = Some(Page::Docket(d.number.clone()));
            }
            ui.label(d.status());
            ui.label(d.last_date());
            ui.label(d.filings().count().to_string());
            let (note, pdf) = note_and_pdf(app, d);
            ui.label(match (note, pdf) {
                (true, true) => "note + PDF",
                (true, false) => "note",
                (false, true) => "PDF",
                _ => "–",
            });
            ui.end_row();
        }
    });
    if go.is_some() {
        app.goto = go;
    }
}

/// A docket number in the recent lists: open it if fetched, else offer Fetch & build.
fn docket_action(app: &mut App, ui: &mut Ui, num: &str) {
    if app.docket(num).is_some() {
        if ui.link("open").on_hover_text("fetched: open its docket page").clicked() {
            app.goto = Some(Page::Docket(num.to_string()));
        }
        return;
    }
    match scotus::norm_docket(num) {
        Some(n) => {
            let queued = app.is_queued(scotus::docket_key(&n));
            if ui
                .add_enabled(!queued, egui::Button::new("Fetch & build").small())
                .on_hover_text(format!("queues ./scotus.py fetch {n} then ./scotus.py build {n}"))
                .on_disabled_hover_text("in the build queue")
                .clicked()
            {
                app.enqueue_scotus(&n, &["fetch", "build"]);
            }
        }
        None => {
            ui.weak("–").on_hover_text("an application or original case: scotus.py fetch reads numbered petitions only");
        }
    }
}

fn recent_panel(app: &mut App, ui: &mut Ui) {
    ui.horizontal(|ui| {
        ui.heading("Recent activity");
        let queued = app.is_queued(scotus::RECENT_KEY);
        if ui
            .add_enabled(!queued, egui::Button::new("Refresh"))
            .on_hover_text("queues ./scotus.py recent: reads supremecourt.gov's opinion pages, Granted & Noted lists and order lists of the last year")
            .on_disabled_hover_text("in the build queue")
            .clicked()
        {
            app.enqueue_recent();
        }
    });
    let rec = match &app.recent {
        Err(e) => {
            ui.colored_label(Color32::LIGHT_RED, e);
            return;
        }
        Ok(None) => {
            ui.label("Not fetched yet: cases/scotus/_recent.json doesn't exist. Refresh runs ./scotus.py recent.");
            ui.weak(NO_PENDING_LIST);
            return;
        }
        Ok(Some(r)) => r.clone(),
    };
    ui.weak(format!(
        "from supremecourt.gov, retrieved {} · since {} · October Terms {}",
        rec.retrieved,
        rec.since,
        rec.terms.iter().map(|t| format!("20{t}")).collect::<Vec<_>>().join(", ")
    ));
    if !rec.errors.is_empty() {
        egui::CollapsingHeader::new(RichText::new(format!("{} pages could not be read", rec.errors.len())).color(Color32::from_rgb(200, 140, 40)))
            .id_salt("recent-errors")
            .show(ui, |ui| {
                for e in &rec.errors {
                    ui.weak(e);
                }
            });
    }
    ui.add_space(4.0);
    let awaiting = rec.granted.iter().filter(|g| g.decided.is_empty()).count();
    let shown_orders = rec.orders.iter().filter(|o| o.kind != "denied").count();
    ui.horizontal(|ui| {
        let t = &mut app.scotus_ui.tab;
        ui.selectable_value(t, Tab::Decided, format!("Decided ({})", rec.decided.len()));
        ui.selectable_value(t, Tab::Granted, format!("Granted, awaiting decision ({awaiting})"));
        ui.selectable_value(t, Tab::Orders, format!("Orders ({shown_orders} + {} denials)", rec.orders.len() - shown_orders));
        ui.separator();
        ui.label("Filter");
        ui.add(egui::TextEdit::singleline(&mut app.scotus_ui.filter).desired_width(180.0));
    });
    ui.add_space(4.0);
    let f = app.scotus_ui.filter.clone();
    match app.scotus_ui.tab {
        Tab::Decided => {
            ui.weak("Opinions of the Court, from the slip-opinion pages (opinions/slipopinion/<term>).");
            egui::Grid::new("decided").striped(true).num_columns(6).show(ui, |ui| {
                for h in ["Date", "Docket", "Case", "J.", "Citation", ""] {
                    ui.strong(h);
                }
                ui.end_row();
                for d in rec.decided.iter().filter(|d| matches(&f, &[&d.name, &d.docket])) {
                    ui.label(&d.date);
                    ui.label(&d.docket);
                    if ui.link(&d.name).on_hover_text(format!("open the slip opinion: {}", d.pdf)).clicked() {
                        open_external(&d.pdf);
                    }
                    ui.label(&d.justice).on_hover_text("author's initials; PC = per curiam");
                    ui.label(&d.citation);
                    docket_action(app, ui, &d.docket);
                    ui.end_row();
                }
            });
        }
        Tab::Granted => {
            ui.horizontal(|ui| {
                ui.weak("Cases granted review, from the Granted & Noted lists (orders/<term>grantednotedlist.pdf).");
                ui.checkbox(&mut app.scotus_ui.show_decided, "show decided too");
            });
            ui.weak(NO_PENDING_LIST);
            let show_decided = app.scotus_ui.show_decided;
            egui::Grid::new("granted").striped(true).num_columns(7).show(ui, |ui| {
                for h in ["Docket", "Case", "Court below", "Granted", "Argument", "Decided", ""] {
                    ui.strong(h);
                }
                ui.end_row();
                for g in rec.granted.iter().filter(|g| (show_decided || g.decided.is_empty()) && matches(&f, &[&g.name, &g.docket])) {
                    ui.label(&g.docket);
                    ui.label(&g.name);
                    ui.label(&g.court);
                    ui.label(&g.granted);
                    ui.label(if g.argued.is_empty() { "–" } else { &g.argued })
                        .on_hover_text("the list's Argument Date: set or held; blank = not yet set");
                    ui.label(if g.decided.is_empty() { "–" } else { &g.decided });
                    docket_action(app, ui, &g.docket);
                    ui.end_row();
                }
            });
        }
        Tab::Orders => {
            ui.horizontal(|ui| {
                ui.weak("Order lists and miscellaneous orders (orders/ordersofthecourt/<term>): grants, summary dispositions, orders in pending cases.");
                ui.checkbox(&mut app.scotus_ui.show_denied, "show denials");
            });
            let show_denied = app.scotus_ui.show_denied;
            let mut shown = 0;
            egui::Grid::new("orders").striped(true).num_columns(5).show(ui, |ui| {
                for h in ["Date", "Docket", "Case", "Order", ""] {
                    ui.strong(h);
                }
                ui.end_row();
                for o in rec.orders.iter().filter(|o| (show_denied || o.kind != "denied") && matches(&f, &[&o.name, &o.docket, &o.text])) {
                    shown += 1;
                    if shown > 3000 {
                        break;
                    }
                    ui.label(&o.date);
                    ui.label(&o.docket);
                    ui.label(&o.name);
                    let color = match o.kind.as_str() {
                        "granted" => Color32::from_rgb(70, 160, 90),
                        "gvr" => Color32::from_rgb(200, 140, 40),
                        "denied" | "rehearing denied" => Color32::from_rgb(140, 140, 140),
                        _ => ui.visuals().text_color(),
                    };
                    let mut hover = format!("{} · {}", o.list, o.section);
                    if !o.text.is_empty() {
                        hover = format!("{hover}\n\n{}", o.text);
                    }
                    let r = ui.horizontal(|ui| {
                        ui.colored_label(color, kind_label(&o.kind));
                        let short: String = o.text.chars().take(50).collect();
                        if !short.is_empty() {
                            ui.weak(if o.text.chars().count() > 50 { format!("{short}…") } else { short });
                        }
                    });
                    if r.response.interact(egui::Sense::click()).on_hover_text(hover).clicked() && !o.url.is_empty() {
                        open_external(&o.url);
                    }
                    docket_action(app, ui, &o.docket);
                    ui.end_row();
                }
            });
            if shown > 3000 {
                ui.weak("Showing the first 3,000; narrow with the filter.");
            }
        }
    }
}

fn kind_label(k: &str) -> &str {
    match k {
        "granted" => "cert. granted",
        "gvr" => "summary disposition",
        "denied" => "denied",
        "rehearing denied" => "rehearing denied",
        "pending" => "order in pending case",
        "discipline" => "attorney discipline",
        _ => "order",
    }
}

// ------------------------------------------------------------------ one docket

pub fn docket_page(app: &mut App, ui: &mut Ui, num: &str) {
    let Some(d) = app.docket(num).cloned() else {
        ui.heading(format!("No. {num}"));
        if app.is_queued(scotus::docket_key(num)) {
            ui.horizontal(|ui| {
                ui.spinner();
                ui.label("Not fetched yet; scotus.py fetch is in the build queue. Output: the panel at the bottom.");
            });
        } else {
            ui.label(format!("cases/scotus/{num}.json doesn't exist yet."));
            if ui.button("Fetch & build").clicked() {
                app.enqueue_scotus(num, &["fetch", "build"]);
            }
        }
        return;
    };
    egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
        ui.heading(d.title());
        ui.label(format!("No. {} · docketed {} · {}", d.number, d.docketed, d.status()));
        if !d.lower_court.is_empty() {
            ui.weak(format!("Below: {}, No. {}, decided {}", d.lower_court, d.lower_numbers, d.lower_decision));
        }
        ui.weak(format!("Docket retrieved {} (scotus.py fetch)", d.retrieved));
        ui.add_space(6.0);

        // files and actions
        let (note, filing_notes) = app.files.docket_notes(&d.number);
        let note = note.cloned();
        let filing_notes: Vec<crate::cases::Note> = filing_notes.into_iter().cloned().collect();
        ui.horizontal(|ui| {
            match note.as_ref().and_then(|n| n.pdf.clone()) {
                Some(pdf) => {
                    if ui.button("Open PDF").clicked() {
                        open_external(&pdf.to_string_lossy());
                    }
                }
                None => {
                    ui.add_enabled(false, egui::Button::new("Open PDF")).on_disabled_hover_text("not built yet: Build writes it");
                }
            }
            match &note {
                Some(n) => match app.cfg.as_ref().and_then(|cfg| obsidian_url(cfg, n)) {
                    Some(u) => {
                        if ui.button("Open docket note in Obsidian").on_hover_text(&u).clicked() {
                            open_external(&u);
                        }
                    }
                    None => {
                        if ui.button("Open docket note").clicked() {
                            open_external(&n.path.to_string_lossy());
                        }
                    }
                },
                None => {
                    ui.add_enabled(false, egui::Button::new("Open docket note")).on_disabled_hover_text("not built yet: Build writes it");
                }
            }
            if ui.button("supremecourt.gov").on_hover_text(d.web_url()).clicked() {
                open_external(&d.web_url());
            }
        });
        ui.horizontal(|ui| {
            let queued = app.is_queued(scotus::docket_key(&d.number));
            if ui
                .add_enabled(!queued, egui::Button::new("Fetch updates"))
                .on_hover_text(format!("queues ./scotus.py fetch {}: the docket again, and any new filing's PDF", d.number))
                .clicked()
            {
                app.enqueue_scotus(&d.number, &["fetch"]);
            }
            if ui
                .add_enabled(!queued, egui::Button::new("Build"))
                .on_hover_text(format!(
                    "queues ./scotus.py build {}: one PDF (pdf/Pending SCOTUS/), the docket note and a note per filing, \
                     with the good-law check of every cited case (slow the first time, cached after)",
                    d.number
                ))
                .clicked()
            {
                app.enqueue_scotus(&d.number, &["build"]);
            }
            if ui.add_enabled(!queued, egui::Button::new("Fetch & build")).clicked() {
                app.enqueue_scotus(&d.number, &["fetch", "build"]);
            }
            if queued {
                ui.weak("in the build queue");
            } else if note.is_some() {
                ui.weak("sync_vault.sh / sync_remarkable.py still copy it to Obsidian and the tablet");
            }
        });
        ui.add_space(6.0);

        // decision below
        if let Some(cid) = d.below_cluster {
            ui.horizontal(|ui| {
                ui.label("Decision below:");
                let name = app
                    .files
                    .index
                    .get(&cid)
                    .map(|e| if e.cite.is_empty() { e.name.clone() } else { format!("{}, {}", e.name, e.cite) })
                    .unwrap_or_else(|| format!("CourtListener cluster {cid}"));
                if ui.link(name).on_hover_text("open its case page").clicked() {
                    app.goto = Some(Page::Case(cid));
                }
            });
        } else {
            ui.weak("Decision below: not resolved yet (scotus.py build looks it up in the local database).");
        }

        // question presented: the Clerk's PDF text, else the docket note's section
        let qp = if d.qp_text.is_empty() { note.as_ref().map(|n| note_section(&n.path, "Questions presented")).unwrap_or_default() } else { d.qp_text.clone() };
        ui.add_space(4.0);
        egui::CollapsingHeader::new(RichText::new("Question presented").strong()).default_open(true).show(ui, |ui| {
            if qp.is_empty() {
                ui.weak("Not in the fetched files.");
            } else {
                ui.add(egui::Label::new(&qp).wrap());
            }
            if !d.qp_url.is_empty() && ui.link("the Clerk's PDF").on_hover_text(&d.qp_url).clicked() {
                open_external(&d.qp_url);
            }
        });

        // filings
        let filings: Vec<scotus::Entry> = d.filings().cloned().collect();
        egui::CollapsingHeader::new(RichText::new(format!("Filings ({})", filings.len())).strong()).default_open(true).show(ui, |ui| {
            let repo = app.cfg.as_ref().map(|c| c.repo_dir.clone());
            egui::Grid::new("filings").striped(true).num_columns(5).show(ui, |ui| {
                for h in ["Filed", "Kind", "Stage", "Filed by", ""] {
                    ui.strong(h);
                }
                ui.end_row();
                for e in &filings {
                    let link = Docket::main_link(e);
                    ui.label(&e.date);
                    let side = if e.side.is_empty() { String::new() } else { format!(" ({})", e.side) };
                    ui.label(format!("{}{side}", e.kind)).on_hover_text(&e.text);
                    ui.label(&e.stage);
                    let who: String = match (e.filer.is_empty(), e.side.as_str()) {
                        (false, _) => e.filer.clone(),
                        (true, "petitioner") => scotus::party(&d.petitioner),
                        (true, "respondent") => scotus::party(&d.respondent),
                        _ => e.text.chars().take(60).collect(),
                    };
                    ui.label(who);
                    ui.horizontal(|ui| {
                        let fnote = link.and_then(|l| filing_notes.iter().find(|n| n.source.as_deref() == Some(l.url.as_str())));
                        match fnote.and_then(|n| app.cfg.as_ref().and_then(|cfg| obsidian_url(cfg, n)).map(|u| (u, n))) {
                            Some((u, _)) => {
                                if ui.small_button("note").on_hover_text(&u).clicked() {
                                    open_external(&u);
                                }
                            }
                            None => match fnote {
                                Some(n) => {
                                    if ui.small_button("note").clicked() {
                                        open_external(&n.path.to_string_lossy());
                                    }
                                }
                                None => {
                                    ui.add_enabled(false, egui::Button::new("note").small()).on_disabled_hover_text("no note: Build writes one per brief");
                                }
                            },
                        }
                        if let Some(l) = link {
                            let local = repo.as_ref().filter(|_| !l.pdf.is_empty()).map(|r| r.join(&l.pdf)).filter(|p| p.is_file());
                            match local {
                                Some(p) => {
                                    if ui.small_button("PDF").on_hover_text(p.to_string_lossy()).clicked() {
                                        open_external(&p.to_string_lossy());
                                    }
                                }
                                None => {
                                    if ui.small_button("PDF (web)").on_hover_text(&l.url).clicked() {
                                        open_external(&l.url);
                                    }
                                }
                            }
                        }
                    });
                    ui.end_row();
                }
            });
        });

        // proceedings and orders
        egui::CollapsingHeader::new(RichText::new(format!("Proceedings and orders ({})", d.entries.len())).strong())
            .default_open(true)
            .show(ui, |ui| {
                egui::Grid::new("proceedings").striped(true).num_columns(2).show(ui, |ui| {
                    for e in &d.entries {
                        ui.label(&e.date);
                        ui.horizontal_wrapped(|ui| {
                            ui.set_max_width(900.0);
                            ui.add(egui::Label::new(&e.text).wrap());
                            for l in e.links.iter().filter(|l| !l.url.is_empty() && !scotus::skip_link(&l.desc)) {
                                if ui.link(&l.desc).on_hover_text(&l.url).clicked() {
                                    open_external(&l.url);
                                }
                            }
                        });
                        ui.end_row();
                    }
                });
            });
    });
}

/// The text under a "## <heading>" in a note, up to the next heading.
fn note_section(path: &std::path::Path, heading: &str) -> String {
    let Ok(text) = std::fs::read_to_string(path) else { return String::new() };
    let mut on = false;
    let mut out = Vec::new();
    for l in text.lines() {
        if let Some(h) = l.strip_prefix("## ") {
            if on {
                break;
            }
            on = h.trim() == heading;
            continue;
        }
        if on && !l.trim().is_empty() {
            out.push(l.trim());
        }
    }
    out.join("\n\n")
}

// ------------------------------------------------------------------ search

/// Above case-search results: fetched dockets matching a docket-number query ("25-579",
/// "No. 25-579"), or whose title holds every word of the query. A docket number not fetched
/// yet gets a Fetch & build button.
pub fn docket_hits(app: &mut App, ui: &mut Ui, query: &str) {
    let num = scotus::norm_docket(query);
    let words: Vec<String> = query.split_whitespace().map(|w| w.to_lowercase()).filter(|w| w != "v." && w != "v" && w != "vs").collect();
    let hits: Vec<(String, String, String)> = app
        .dockets
        .iter()
        .filter(|d| {
            num.as_deref() == Some(d.number.as_str())
                || (num.is_none() && !words.is_empty() && {
                    let t = format!("{} {}", d.title(), d.respondent).to_lowercase();
                    words.iter().all(|w| t.contains(w.as_str()))
                })
        })
        .map(|d| (d.number.clone(), d.title(), d.status()))
        .collect();
    if hits.is_empty() && num.is_none() {
        return;
    }
    egui::Frame::group(ui.style()).show(ui, |ui| {
        ui.strong("Supreme Court dockets (scotus.py)");
        for (n, t, st) in &hits {
            ui.horizontal(|ui| {
                ui.label(format!("No. {n}"));
                if ui.link(t).clicked() {
                    app.goto = Some(Page::Docket(n.clone()));
                }
                ui.weak(st);
            });
        }
        if let (Some(n), true) = (&num, hits.is_empty()) {
            ui.horizontal(|ui| {
                ui.label(format!("No. {n} isn't fetched. Pending cases aren't in the CourtListener database."));
                let queued = app.is_queued(scotus::docket_key(n));
                if ui.add_enabled(!queued, egui::Button::new("Fetch & build")).on_hover_text(format!("queues ./scotus.py fetch {n} then build")).clicked() {
                    app.enqueue_scotus(n, &["fetch", "build"]);
                    app.goto = Some(Page::Docket(n.clone()));
                }
            });
        }
    });
    ui.add_space(6.0);
}
