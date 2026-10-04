//! lawgui: a read-only browser over what the LawFormatting scripts produced.
//! Start screen -> View Clusters (areas, lineages, slices, clusters, sub-clusters) and View Cases,
//! all leading to one shared case page. See claude/scope-gui.md in the Project.

mod cases;
mod config;
mod db;
mod graph;
mod model;
mod pages;
mod search;

use cases::CaseFiles;
use config::Config;
use eframe::egui;
use model::*;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::mpsc::{Receiver, Sender};
use std::time::Duration;

fn main() -> eframe::Result<()> {
    let opts = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1400.0, 900.0])
            .with_title("Law browser"),
        ..Default::default()
    };
    eframe::run_native("lawgui", opts, Box::new(|cc| Ok(Box::new(App::new(cc)))))
}

pub struct App {
    pub cfg: Option<Config>,
    pub cfg_path: Option<PathBuf>,
    pub cfg_err: Option<String>,
    pub views: Vec<ViewInfo>,
    pub view_sel: Option<usize>,
    pub meta: Option<ViewMeta>,
    pub view_err: Option<String>,
    tx: Option<Sender<db::Req>>,
    rx: Option<Receiver<db::Resp>>,
    pub nav: Vec<Page>,
    pub pos: usize,
    pub cache: HashMap<Page, PageData>,
    pub titles: HashMap<Page, String>,
    pub errors: HashMap<Page, String>,
    pub pending: Option<Page>,
    pub last_time: Option<Duration>,
    pub filter: String,
    pub slice_idx: usize,
    pub files: CaseFiles,
    /// Set by the page renderers; acted on after drawing (no borrow of self while drawing).
    pub goto: Option<Page>,
    pub graph_opts: graph::GraphOpts,
    /// Sort of every case table: column and descending.
    pub case_sort: (pages::SortKey, bool),
    stx: Option<Sender<search::Req>>,
    srx: Option<Receiver<search::Resp>>,
    pub search_cache: HashMap<Page, Result<search::Results, String>>,
    pub search_pending: Option<Page>,
    pub search_input: String,
    pub search_mode: search::Mode,
}

impl App {
    fn new(cc: &eframe::CreationContext<'_>) -> App {
        let mut app = App {
            cfg: None,
            cfg_path: None,
            cfg_err: None,
            views: Vec::new(),
            view_sel: None,
            meta: None,
            view_err: None,
            tx: None,
            rx: None,
            nav: vec![Page::Home],
            pos: 0,
            cache: HashMap::new(),
            titles: HashMap::new(),
            errors: HashMap::new(),
            pending: None,
            last_time: None,
            filter: String::new(),
            slice_idx: 0,
            files: CaseFiles::default(),
            goto: None,
            graph_opts: graph::GraphOpts::default(),
            case_sort: (pages::SortKey::Weight, true),
            stx: None,
            srx: None,
            search_cache: HashMap::new(),
            search_pending: None,
            search_input: String::new(),
            search_mode: search::Mode::Name,
        };
        match Config::load() {
            Ok((cfg, path)) => {
                let ctx = cc.egui_ctx.clone();
                let (tx, rx) = db::spawn(cfg.labels_file(), cfg.cases_dir(), cfg.main_db(), move || ctx.request_repaint());
                app.tx = Some(tx);
                app.rx = Some(rx);
                let ctx2 = cc.egui_ctx.clone();
                let (stx, srx) = search::spawn(cfg.search_db(), cfg.main_db(), cfg.cases_dir(), move || ctx2.request_repaint());
                app.stx = Some(stx);
                app.srx = Some(srx);
                app.views = db::list_views(&cfg.bulk_dir);
                app.files = CaseFiles::load(&cfg);
                app.view_sel = app
                    .views
                    .iter()
                    .position(|v| v.ok && v.stem == cfg.default_view)
                    .or_else(|| app.views.iter().position(|v| v.ok));
                if app.view_sel.is_none() {
                    app.view_err = Some(format!(
                        "No view with schema {} in {}",
                        db::SCHEMA,
                        cfg.bulk_dir.display()
                    ));
                }
                app.cfg = Some(cfg);
                app.cfg_path = Some(path);
                app.open_view();
            }
            Err(e) => app.cfg_err = Some(e),
        }
        app
    }

    pub fn open_view(&mut self) {
        let (Some(i), Some(tx)) = (self.view_sel, &self.tx) else { return };
        self.meta = None;
        self.view_err = None;
        self.cache.clear();
        self.errors.clear();
        self.search_cache.clear();
        let _ = tx.send(db::Req::Open(self.views[i].path.clone()));
    }

    pub fn slice_label(&self, s: i64) -> String {
        self.meta
            .as_ref()
            .and_then(|m| m.slices.iter().find(|x| x.slice == s))
            .map(|x| x.label.clone())
            .unwrap_or_else(|| format!("{}-{:02}", s, (s + 4) % 100))
    }

    pub fn current(&self) -> &Page {
        &self.nav[self.pos]
    }

    fn request(&mut self, page: &Page) {
        if let Page::Search { query, mode } = page {
            if query.trim().is_empty() || self.search_cache.contains_key(page) || self.search_pending.as_ref() == Some(page) {
                return;
            }
            if let (Some(tx), Some(i)) = (&self.stx, self.view_sel) {
                self.search_pending = Some(page.clone());
                let _ = tx.send(search::Req { page: page.clone(), query: query.clone(), mode: *mode, view: self.views[i].path.clone() });
            }
            return;
        }
        if !page.needs_db() || self.cache.contains_key(page) || self.meta.is_none() {
            return;
        }
        if self.pending.as_ref() == Some(page) {
            return;
        }
        if let Some(tx) = &self.tx {
            self.errors.remove(page);
            self.pending = Some(page.clone());
            let _ = tx.send(db::Req::Load(page.clone()));
        }
    }

    pub fn go(&mut self, page: Page) {
        if *self.current() == page {
            return;
        }
        self.nav.truncate(self.pos + 1);
        self.nav.push(page);
        self.pos = self.nav.len() - 1;
        self.filter.clear();
        let p = self.current().clone();
        if let Page::Search { query, mode } = &p {
            self.search_input = query.clone();
            self.search_mode = *mode;
        }
        self.request(&p);
    }

    fn back(&mut self) {
        if self.pos > 0 {
            self.pos -= 1;
            let p = self.current().clone();
            self.request(&p);
        }
    }

    fn forward(&mut self) {
        if self.pos + 1 < self.nav.len() {
            self.pos += 1;
            let p = self.current().clone();
            self.request(&p);
        }
    }

    fn poll(&mut self) {
        if let Some(srx) = &self.srx {
            let got: Vec<search::Resp> = srx.try_iter().collect();
            for r in got {
                if self.search_pending.as_ref() == Some(&r.page) {
                    self.search_pending = None;
                }
                if let Ok(res) = &r.result {
                    self.last_time = Some(res.elapsed);
                }
                self.search_cache.insert(r.page, r.result);
            }
        }
        let Some(rx) = &self.rx else { return };
        let msgs: Vec<db::Resp> = rx.try_iter().collect();
        for m in msgs {
            match m {
                db::Resp::Opened(Ok(meta)) => {
                    self.slice_idx = meta
                        .slices
                        .iter()
                        .position(|s| s.slice == meta.newest_full)
                        .unwrap_or(meta.slices.len().saturating_sub(1));
                    self.meta = Some(meta);
                    self.pending = None;
                    let p = self.current().clone();
                    self.request(&p);
                }
                db::Resp::Opened(Err(e)) => {
                    self.meta = None;
                    self.view_err = Some(e);
                }
                db::Resp::Loaded(page, res, t) => {
                    if self.pending.as_ref() == Some(&page) {
                        self.pending = None;
                    }
                    self.last_time = Some(t);
                    match res {
                        Ok(data) => {
                            let sl = |s| self.slice_label(s);
                            self.titles.insert(page.clone(), data.title(&sl));
                            self.cache.insert(page, data);
                        }
                        Err(e) => {
                            self.errors.insert(page, e);
                        }
                    }
                }
            }
        }
        // a page navigated to before the view opened, or evicted by a view change
        let p = self.current().clone();
        if self.pending.is_none() && !self.errors.contains_key(&p) {
            self.request(&p);
        }
    }

    pub fn title_of(&self, p: &Page) -> String {
        match p {
            Page::Home => "Start".into(),
            Page::Cases => "Built cases".into(),
            Page::Areas => "Areas".into(),
            Page::Search { query, .. } if query.is_empty() => "Search".into(),
            Page::Search { query, .. } => format!("Search: {query}"),
            _ => self.titles.get(p).cloned().unwrap_or_else(|| "…".into()),
        }
    }

    fn top_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            if ui.add_enabled(self.pos > 0, egui::Button::new("◀")).on_hover_text("Back (Alt+←)").clicked() {
                self.back();
            }
            if ui
                .add_enabled(self.pos + 1 < self.nav.len(), egui::Button::new("▶"))
                .on_hover_text("Forward (Alt+→)")
                .clicked()
            {
                self.forward();
            }
            if ui.button("Start").clicked() {
                self.goto = Some(Page::Home);
            }
            ui.separator();
            // breadcrumb: the last few places visited, oldest first
            let start = self.pos.saturating_sub(5);
            for i in start..=self.pos {
                let t = self.title_of(&self.nav[i]);
                let t = if t.chars().count() > 40 { format!("{}…", t.chars().take(39).collect::<String>()) } else { t };
                if i == self.pos {
                    ui.strong(t);
                } else {
                    if ui.link(t).clicked() {
                        self.pos = i;
                        let p = self.current().clone();
                        self.request(&p);
                    }
                    ui.weak("›");
                }
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if let Some(t) = self.last_time {
                    ui.weak(format!("{} ms", t.as_millis()));
                }
                if self.pending.is_some() || self.search_pending.is_some() {
                    ui.spinner();
                }
                if let Some(m) = &self.meta {
                    ui.weak(&m.stem);
                }
            });
        });
    }
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.poll();
        let ctx = ui.ctx().clone();
        if ctx.input(|i| i.modifiers.alt && i.key_pressed(egui::Key::ArrowLeft)) {
            self.back();
        }
        if ctx.input(|i| i.modifiers.alt && i.key_pressed(egui::Key::ArrowRight)) {
            self.forward();
        }
        egui::Panel::top("top").show(ui, |ui| {
            ui.add_space(4.0);
            self.top_bar(ui);
            ui.add_space(4.0);
        });
        // a case page keeps the side panel of the section it was reached from
        let section = self.nav[..=self.pos]
            .iter()
            .rev()
            .find(|p| !matches!(p, Page::Case(_)))
            .map(|p| p.section())
            .unwrap_or(Section::Clusters);
        if section != Section::Home {
            egui::Panel::left("side").default_size(340.0).min_size(220.0).max_size(600.0).show(ui, |ui| {
                pages::side_panel(self, ui, section);
            });
        }
        egui::CentralPanel::default().show(ui, |ui| {
            pages::central(self, ui);
        });
        if let Some(p) = self.goto.take() {
            self.go(p);
        }
    }
}
