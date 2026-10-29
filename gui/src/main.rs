//! lawgui: a read-only browser over what the tribonian-us scripts produced.
//! Start screen -> View Clusters (areas, lineages, slices, clusters, sub-clusters) and View Opinions,
//! all leading to one shared case page. See claude/scope-gui.md in the Project.

mod apisearch;
mod cases;
mod check_page;
mod cocache;
mod config;
mod db;
mod dockets;
mod dockets_page;
mod graph;
mod jobs;
mod model;
mod pages;
mod recent;
mod scotus;
mod scotus_page;
mod subdoc;
mod search;
mod serve;
mod semsearch_page;

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

/// One entry in the build queue: a case's Build / Quick build, or a single script command.
pub struct Queued {
    pub r#ref: i64,
    pub title: String,
    pub job: QueuedJob,
}

pub enum QueuedJob {
    /// law: the law flags when it was queued (`App::law_args`: --no-law or --fetch-state-law)
    Build { quick: bool, law: Vec<String> },
    /// arguments after the interpreter, e.g. casemap.py coupling --cluster N
    Script(Vec<String>),
    /// several commands, stopping at the first failure; each with the line its success prints, if any
    Steps(Vec<(Vec<String>, Option<String>)>),
}

impl Queued {
    /// The name in the status line and the output panel; quick builds carry "(quick)".
    fn label(&self) -> String {
        match self.job {
            QueuedJob::Build { quick: true, .. } => format!("{} (quick)", self.title),
            _ => self.title.clone(),
        }
    }
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
    /// Searches, newest first, without repeats; kept between sessions (`recent.rs`).
    pub search_history: Vec<(String, search::Mode)>,
    pub show_history: bool,
    /// "Fetch state law" (on by default): case and docket builds also download and build the state law they cite
    /// that is not local (--fetch-state-law), so its text is printed at the end of the PDF.
    pub fetch_state_law: bool,
    /// "Print the law cited" (on by default): builds end with The Law Cited; off passes --no-law.
    pub print_law: bool,
    pub api_cache: HashMap<Page, Result<apisearch::ApiResults, String>>,
    api_pending: std::collections::HashSet<Page>,
    atx: Sender<apisearch::Resp>,
    arx: Receiver<apisearch::Resp>,
    /// Names of API hits, for the case page of a case not in the local database yet.
    pub api_names: HashMap<i64, String>,
    /// The running Build, if any, its output, and how the last one ended.
    pub job: Option<jobs::Running>,
    pub job_log: Vec<String>,
    pub queue: std::collections::VecDeque<Queued>,
    /// The current run: builds since the queue last emptied.
    pub run_total: usize,
    pub run_done: usize,
    pub run_failed: Vec<String>,
    pub show_log: bool,
    /// cases/scotus/*.json (tribonian scotus fetch), and _recent.json (scotus.py recent)
    pub dockets: Vec<scotus::Docket>,
    pub docket_errors: Vec<String>,
    pub recent: Result<Option<scotus::Recent>, String>,
    pub scotus_ui: scotus_page::ScotusUi,
    /// cases/recap/_index.json (tribonian dockets index) and cases/dockets/_courts.json (tribonian dockets courts)
    pub recap: Vec<dockets::RecapDocket>,
    pub recap_error: Option<String>,
    pub courts: Result<Option<dockets::CourtTree>, String>,
    pub dockets_ui: dockets_page::DocketsUi,
    /// Search Dockets results by query (cleared when the data mode changes)
    pub docket_search: HashMap<String, Result<dockets::SearchResult, String>>,
    docket_search_pending: std::collections::HashSet<String>,
    dstx: Sender<dockets::SearchResp>,
    dsrx: Receiver<dockets::SearchResp>,
    pub sem_ui: semsearch_page::SemUi,
    pub check_ui: check_page::CheckUi,
    egui_ctx: Option<egui::Context>,
    /// Database pages requested and not answered yet (the main window's and every pop-out's).
    inflight: std::collections::HashSet<Page>,
    /// Cited-together filter: (cluster, name), in the order added. "Find" opens Page::CoCited.
    pub cocite: Vec<(i64, String)>,
    /// Sort of the cited-together table: column (0 filed, 1 name, 2 court, 3 cited) and descending.
    pub cocite_sort: (usize, bool),
    /// Pages open in their own windows ("Pop out", or Ctrl+click on a link).
    popouts: Vec<PopOut>,
    next_popout: u64,
}

/// A window showing one page's content, with its own back/forward.
struct PopOut {
    id: u64,
    nav: Vec<Page>,
    pos: usize,
    filter: String,
    open: bool,
}

impl App {
    fn new(cc: &eframe::CreationContext<'_>) -> App {
        let (atx, arx) = std::sync::mpsc::channel();
        let (dstx, dsrx) = std::sync::mpsc::channel();
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
            search_history: recent::load(),
            show_history: false,
            fetch_state_law: true,
            print_law: true,
            api_cache: HashMap::new(),
            api_pending: Default::default(),
            atx,
            arx,
            api_names: HashMap::new(),
            job: None,
            job_log: Vec::new(),
            queue: std::collections::VecDeque::new(),
            run_total: 0,
            run_done: 0,
            run_failed: Vec::new(),
            show_log: false,
            dockets: Vec::new(),
            docket_errors: Vec::new(),
            recent: Ok(None),
            scotus_ui: Default::default(),
            recap: Vec::new(),
            recap_error: None,
            courts: Ok(None),
            dockets_ui: Default::default(),
            docket_search: HashMap::new(),
            docket_search_pending: Default::default(),
            dstx,
            dsrx,
            sem_ui: Default::default(),
            check_ui: Default::default(),
            egui_ctx: Some(cc.egui_ctx.clone()),
            inflight: Default::default(),
            cocite: Vec::new(),
            cocite_sort: (0, true),
            popouts: Vec::new(),
            next_popout: 0,
        };
        match Config::load() {
            Ok((cfg, path)) => {
                let ctx = cc.egui_ctx.clone();
                let (tx, rx) = db::spawn(cfg.labels_file(), cfg.cases_dir(), cfg.main_db(), cfg.opinions_db(), move || ctx.request_repaint());
                app.tx = Some(tx);
                app.rx = Some(rx);
                let ctx2 = cc.egui_ctx.clone();
                let (stx, srx) = search::spawn(cfg.search_db(), cfg.main_db(), cfg.cases_dir(), move || ctx2.request_repaint());
                app.stx = Some(stx);
                app.srx = Some(srx);
                app.views = db::list_views(&cfg.bulk_dir);
                app.files = CaseFiles::load(&cfg);
                app.cfg = Some(cfg.clone());
                app.load_scotus();
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
                app.cfg_path = path;
                app.open_view();
            }
            Err(e) => app.cfg_err = Some(e),
        }
        app
    }

    /// Reread scotus.py's files: at start and after every queued job.
    pub fn load_scotus(&mut self) {
        let Some(cfg) = &self.cfg else { return };
        (self.dockets, self.docket_errors) = scotus::load_dockets(&cfg.cases_dir(), &cfg.work_dir);
        self.recent = scotus::load_recent(&cfg.cases_dir());
        (self.recap, self.recap_error) = match dockets::load_index(&cfg.cases_dir()) {
            Ok(v) => (v, None),
            Err(e) => (Vec::new(), Some(e)),
        };
        self.courts = dockets::load_courts(&cfg.cases_dir());
    }

    pub fn docket(&self, num: &str) -> Option<&scotus::Docket> {
        self.dockets.iter().find(|d| d.number == num)
    }

    /// The law flags every build passes, from the two checkboxes: --no-law when The Law Cited is off (then
    /// nothing is fetched), else --fetch-state-law when "Fetch state law" is on.
    pub fn law_args(&self) -> Vec<String> {
        if !self.print_law {
            vec!["--no-law".into()]
        } else if self.fetch_state_law {
            vec!["--fetch-state-law".into()]
        } else {
            vec![]
        }
    }

    /// Queue scotus.py commands for a docket ("fetch", "build" or both, in that order) as one job.
    /// `build` counts as done only when it prints its "wrote pdf/..." line, as for case builds.
    pub fn enqueue_scotus(&mut self, num: &str, what: &[&str]) {
        let steps = what
            .iter()
            .map(|w| {
                let mut args = vec!["scotus".to_string(), w.to_string(), num.to_string()];
                if *w == "build" {
                    args.extend(self.law_args());
                }
                (args, (*w == "build").then(|| "wrote pdf/".to_string()))
            })
            .collect();
        let title = format!("No. {num}: {}", what.join(" + "));
        self.enqueue(Queued { r#ref: scotus::docket_key(num), title, job: QueuedJob::Steps(steps) });
    }

    /// Queue `scotus.py recent` (refreshes cases/scotus/_recent.json).
    pub fn enqueue_recent(&mut self) {
        self.enqueue(Queued {
            r#ref: scotus::RECENT_KEY,
            title: "Supreme Court recent activity".into(),
            job: QueuedJob::Script(vec!["scotus".into(), "recent".into()]),
        });
    }

    pub fn open_view(&mut self) {
        let (Some(i), Some(tx)) = (self.view_sel, &self.tx) else { return };
        self.meta = None;
        self.view_err = None;
        self.cache.clear();
        self.errors.clear();
        self.search_cache.clear();
        self.inflight.clear();
        let _ = tx.send(db::Req::Open(self.views[i].path.clone()));
    }

    /// Queue a Build of a case's note and PDF. A queue that has emptied starts a fresh run:
    /// counts and output begin again.
    pub fn enqueue_build(&mut self, r: i64, title: String, quick: bool) {
        let law = self.law_args();
        self.enqueue(Queued { r#ref: r, title, job: QueuedJob::Build { quick, law } });
    }

    /// Queue `casemap.py recent` (citing cases newer than the bulk data, from the API) and then a full
    /// build, whose good-law check reads them. Needs the case file; false when there is none yet.
    pub fn enqueue_recent_citers(&mut self, r: i64, title: String) -> bool {
        let Some(full) = self.cfg.as_ref().and_then(|cfg| jobs::full_file(&cfg.work_dir, r)) else { return false };
        let steps = vec![
            (vec!["map".into(), "recent".into(), "--cluster".into(), r.to_string()], None),
            (
                [vec!["case".into(), "build".into()], self.law_args(), vec![format!("cases/{full}")]]
                    .concat(),
                Some("wrote ".to_string()),
            ),
        ];
        self.enqueue(Queued { r#ref: r, title: format!("{title}: recent citers + build"), job: QueuedJob::Steps(steps) });
        true
    }

    /// Queue one script command for a case (e.g. Find coupled cases), through the same queue,
    /// status line and output panel as Build; the page reloads when it finishes.
    pub fn enqueue_script(&mut self, r: i64, title: String, args: Vec<String>) {
        self.enqueue(Queued { r#ref: r, title, job: QueuedJob::Script(args) });
    }

    /// Queue several script commands as one job under a key of the caller's (e.g. cocite.py subdoc).
    pub fn enqueue_steps(&mut self, r: i64, title: String, steps: Vec<(Vec<String>, Option<String>)>) {
        self.enqueue(Queued { r#ref: r, title, job: QueuedJob::Steps(steps) });
    }

    pub(crate) fn enqueue(&mut self, q: Queued) {
        if self.is_queued(q.r#ref) {
            return;
        }
        if self.job.is_none() && self.queue.is_empty() {
            self.run_total = 0;
            self.run_done = 0;
            self.run_failed.clear();
            self.job_log.clear();
        }
        self.queue.push_back(q);
        self.run_total += 1;
        self.start_next();
    }

    /// Anything for this case queued or running: its Build, Quick build and Find coupled cases
    /// buttons are disabled meanwhile.
    pub fn is_queued(&self, r: i64) -> bool {
        self.job.as_ref().is_some_and(|j| j.r#ref == r) || self.queue.iter().any(|q| q.r#ref == r)
    }

    fn start_next(&mut self) {
        if self.job.is_some() {
            return;
        }
        let Some(cfg) = &self.cfg else { return };
        let Some(q) = self.queue.pop_front() else { return };
        let ctx = self.egui_ctx.clone();
        let wake = move || {
            if let Some(c) = &ctx {
                c.request_repaint();
            }
        };
        let title = q.label();
        self.job_log.push(format!("── {title}"));
        let py = cfg.py();
        self.job = Some(match q.job {
            QueuedJob::Build { quick, law } => jobs::build_case(py, q.r#ref, title, quick, law, wake),
            QueuedJob::Script(args) => jobs::run_script(py, q.r#ref, title, args, wake),
            QueuedJob::Steps(steps) => jobs::run_steps(py, q.r#ref, title, steps, wake),
        });
    }

    fn poll_job(&mut self) {
        let Some(job) = &self.job else { return };
        let mut done = None;
        for m in job.rx.try_iter() {
            match m {
                jobs::Msg::Line(l) => {
                    self.job_log.push(l);
                }
                jobs::Msg::Done { ok } => done = Some(ok),
            }
        }
        if self.job_log.len() > 5000 {
            let cut = self.job_log.len() - 5000;
            self.job_log.drain(..cut);
        }
        if let Some(ok) = done {
            let job = self.job.take().unwrap();
            self.run_done += 1;
            if !ok {
                self.run_failed.push(job.title.clone());
            }
            // the scripts changed notes, PDFs and profiles: reread the files, reload the pages
            if let Some(cfg) = &self.cfg {
                self.files = CaseFiles::load(cfg);
            }
            self.load_scotus();
            self.cache.clear();
            self.errors.clear();
            let p = self.current().clone();
            self.request(&p);
            self.start_next();
        }
    }

    /// The status line at the very bottom: the queue while it runs, "Done" once it empties.
    fn status_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            match &self.job {
                Some(j) => {
                    ui.spinner();
                    let verb = if j.build { "Building" } else { "Running" };
                    ui.label(format!("{verb} {} of {}: {}", self.run_done + 1, self.run_total, j.title));
                    if !self.queue.is_empty() {
                        ui.weak(format!("· {} queued", self.queue.len()));
                    }
                }
                None => {
                    if self.run_failed.is_empty() {
                        ui.colored_label(egui::Color32::from_rgb(70, 160, 90), "Done");
                    } else {
                        ui.colored_label(
                            egui::Color32::LIGHT_RED,
                            format!("Done · {} failed: {}", self.run_failed.len(), self.run_failed.join(", ")),
                        );
                    }
                }
            }
            if !self.run_failed.is_empty() && self.job.is_some() {
                ui.colored_label(egui::Color32::LIGHT_RED, format!("· {} failed", self.run_failed.len()));
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.selectable_label(self.show_log, "Output").clicked() {
                    self.show_log = !self.show_log;
                }
            });
        });
    }

    fn log_panel(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.strong("Build output");
            if !self.queue.is_empty() {
                let names: Vec<String> = self.queue.iter().map(|q| q.label()).collect();
                ui.weak(format!("next: {}", names.join(", ")));
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.small_button("Hide").clicked() {
                    self.show_log = false;
                }
            });
        });
        ui.separator();
        egui::ScrollArea::vertical().stick_to_bottom(true).auto_shrink([false, false]).show(ui, |ui| {
            for l in &self.job_log {
                ui.label(egui::RichText::new(l).monospace().size(11.0));
            }
        });
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
            self.request_api(page);
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
        if self.pending.as_ref() == Some(page) || self.inflight.contains(page) {
            return;
        }
        if let Some(tx) = &self.tx {
            self.errors.remove(page);
            self.inflight.insert(page.clone());
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
            if !query.is_empty() {
                let entry = (query.clone(), *mode);
                self.search_history.retain(|e| *e != entry);
                self.search_history.insert(0, entry);
                self.search_history.truncate(recent::CAP);
                recent::save(&self.search_history);
            }
        }
        self.request(&p);
    }

    fn back(&mut self) {
        if self.pos > 0 {
            self.pos -= 1;
            self.arrive();
        }
    }

    fn forward(&mut self) {
        if self.pos + 1 < self.nav.len() {
            self.pos += 1;
            self.arrive();
        }
    }

    /// After moving through history: a search page puts its query back in the search box.
    fn arrive(&mut self) {
        let p = self.current().clone();
        if let Page::Search { query, mode } = &p {
            self.search_input = query.clone();
            self.search_mode = *mode;
        }
        self.request(&p);
    }

    /// The CourtListener half of a search, when "Include CourtListener" is on; once per page.
    fn request_api(&mut self, page: &Page) {
        let Page::Search { query, .. } = page else { return };
        if !self.online() || query.trim().is_empty() || self.api_cache.contains_key(page) || self.api_pending.contains(page) {
            return;
        }
        let Some(cfg) = &self.cfg else { return };
        let ctx = self.egui_ctx.clone();
        self.api_pending.insert(page.clone());
        apisearch::spawn(cfg.py(), page.clone(), self.atx.clone(), move || {
            if let Some(c) = &ctx {
                c.request_repaint();
            }
        });
    }

    /// May searches use the CourtListener API: every data mode but offline.
    pub fn online(&self) -> bool {
        self.cfg.as_ref().is_some_and(|c| c.mode != "offline")
    }

    /// Search Dockets: run `tribonian dockets search` once per query (it may make one API request).
    pub fn request_docket_search(&mut self, query: &str) {
        if self.docket_search.contains_key(query) || self.docket_search_pending.contains(query) {
            return;
        }
        let Some(cfg) = &self.cfg else { return };
        let ctx = self.egui_ctx.clone();
        self.docket_search_pending.insert(query.to_string());
        dockets::spawn_search(cfg.py(), query.to_string(), self.dstx.clone(), move || {
            if let Some(c) = &ctx {
                c.request_repaint();
            }
        });
    }

    /// The top bar's data mode: every script runs with --mode <it>; remembered between sessions.
    fn mode_picker(&mut self, ui: &mut egui::Ui) {
        let Some(cfg) = &self.cfg else { return };
        let (cur, set) = (cfg.mode.clone(), cfg.data_mode.clone());
        let mut pick = None;
        egui::ComboBox::from_id_salt("data-mode")
            .selected_text(format!("Data: {cur}"))
            .show_ui(ui, |ui| {
                for m in config::DATA_MODES {
                    let what = match m {
                        "offline" => "local data only, no CourtListener requests",
                        "full" => "full bulk data; the API only for what is newer",
                        "lite" => "small bulk tables; opinion text from the API",
                        _ => "everything from the CourtListener API",
                    };
                    let label = if m == set { format!("{m}  (config)") } else { m.to_string() };
                    match cfg.supports(m) {
                        Ok(()) => {
                            if ui.selectable_label(cur == m, label).on_hover_text(what).clicked() {
                                pick = Some(m.to_string());
                            }
                        }
                        Err(why) => {
                            ui.add_enabled(false, egui::Button::selectable(false, label)).on_disabled_hover_text(format!("{what}: {why}"));
                        }
                    }
                }
            })
            .response
            .on_hover_text(format!(
                "the data mode every script runs with (tribonian --mode {cur}); the config file says {set}. \
                 Offline: no CourtListener requests at all."
            ));
        if let Some(m) = pick {
            recent::save_mode(&m);
            if let Some(cfg) = &mut self.cfg {
                cfg.mode = m;
            }
            self.docket_search.clear();
        }
    }

    pub fn api_pending(&self, page: &Page) -> bool {
        self.api_pending.contains(page)
    }

    fn poll(&mut self) {
        for r in self.dsrx.try_iter().collect::<Vec<_>>() {
            self.docket_search_pending.remove(&r.query);
            self.docket_search.insert(r.query, r.result);
        }
        for r in self.arx.try_iter().collect::<Vec<_>>() {
            self.api_pending.remove(&r.page);
            if let Ok(res) = &r.result {
                for h in &res.hits {
                    self.api_names.insert(h.cluster, h.name.clone());
                }
            }
            self.api_cache.insert(r.page, r.result);
        }
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
                    self.inflight.remove(&page);
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
            Page::Cases => "Built opinions".into(),
            Page::Areas => "Areas".into(),
            Page::Search { query, .. } if query.is_empty() => "Search".into(),
            Page::Search { query, mode: search::Mode::Name } => format!("Search: {query}"),
            Page::Search { query, mode: search::Mode::Parenthetical } => format!("Parentheticals: {query}"),
            Page::Scotus => "SCOTUS".into(),
            Page::SemSearch => "Sub-Cluster Search".into(),
            Page::Check => "Check setup".into(),
            Page::Dockets => "View Dockets".into(),
            Page::DocketsFederal => "Federal".into(),
            Page::DocketsState(None) => "State".into(),
            Page::DocketsState(Some(code)) => self
                .courts
                .as_ref()
                .ok()
                .and_then(|t| t.as_ref())
                .and_then(|t| t.state.get(code))
                .map(|s| s.name.clone())
                .unwrap_or_else(|| code.clone()),
            Page::CourtDockets(id) => self
                .courts
                .as_ref()
                .ok()
                .and_then(|t| t.as_ref())
                .and_then(|t| t.court(id))
                .map(|c| if c.cite.is_empty() { c.name.clone() } else { c.cite.clone() })
                .unwrap_or_else(|| id.clone()),
            Page::DocketSearch(q) if q.is_empty() => "Search Dockets".into(),
            Page::DocketSearch(q) => format!("Dockets: {q}"),
            Page::RecapDocket(id) => match self.recap.iter().find(|d| d.id == *id) {
                Some(d) => format!("No. {} {}", d.number, d.name),
                None => format!("docket {id}"),
            },
            Page::CoCited(refs) if !self.cache.contains_key(p) && !self.titles.contains_key(p) => {
                let names: Vec<String> = refs
                    .iter()
                    .map(|r| self.cocite.iter().find(|c| c.0 == *r).map(|c| c.1.clone()).unwrap_or_else(|| format!("cluster {r}")))
                    .collect();
                format!("Cited together: {}", names.join(" + "))
            }
            Page::Docket(n) => match self.docket(n) {
                Some(d) => format!("No. {n} {}", d.title()),
                None => format!("No. {n}"),
            },
            _ => self.titles.get(p).cloned().unwrap_or_else(|| "…".into()),
        }
    }

    /// The section a page belongs to; a case page belongs to the section it was reached from.
    fn section_at(&self, pos: usize) -> Section {
        self.nav[..=pos]
            .iter()
            .rev()
            .find(|p| !matches!(p, Page::Case(_)))
            .map(|p| p.section())
            .unwrap_or(Section::Clusters)
    }

    /// Federal, or State › the state, for a court's breadcrumbs.
    fn court_crumbs(&self, id: &str) -> Vec<(String, Option<Page>)> {
        let tree = self.courts.as_ref().ok().and_then(|t| t.as_ref());
        match tree.and_then(|t| t.state.iter().find(|(_, s)| s.highest.iter().chain(&s.appellate).chain(&s.trial).chain(&s.special).any(|c| c.id == id))) {
            Some((code, s)) => vec![("State".into(), Some(Page::DocketsState(None))), (s.name.clone(), Some(Page::DocketsState(Some(code.clone()))))],
            None => vec![("Federal".into(), Some(Page::DocketsFederal))],
        }
    }

    /// The real path to the current page: Start › … › here. Parents come from the page's data
    /// (a sub-cluster knows its cluster, a lineage its area), so they show once it has loaded.
    fn crumbs(&self) -> Vec<(String, Option<Page>)> {
        let mut c: Vec<(String, Option<Page>)> = vec![("Start".into(), Some(Page::Home))];
        let here = self.current().clone();
        let data = self.cache.get(&here);
        let slice = |s: i64| (self.slice_label(s), Some(Page::Slice(s)));
        match &here {
            Page::Home => return c,
            Page::Areas => {}
            Page::Area(_) => c.push(("Areas".into(), Some(Page::Areas))),
            Page::Lineage(_) => {
                c.push(("Areas".into(), Some(Page::Areas)));
                if let Some(PageData::Lineage { area: Some((a, an)), .. }) = data {
                    c.push((an.clone(), Some(Page::Area(*a))));
                }
            }
            Page::Slice(_) => {}
            Page::Cluster { slice: s, .. } => c.push(slice(*s)),
            Page::Sub { slice: s, .. } => {
                c.push(slice(*s));
                if let Some(PageData::Unit { head, .. }) = data {
                    if let Some(p) = &head.parent {
                        c.push((format!("{} {}", p.code(&self.slice_label(p.slice)), p.name), Some(p.page())));
                    }
                }
            }
            Page::Cases => {}
            Page::Search { .. } => {}
            Page::CoCited(_) => {}
            Page::Scotus => {}
            Page::SemSearch => {}
            Page::Check => {}
            Page::Docket(_) => c.push(("SCOTUS".into(), Some(Page::Scotus))),
            Page::Dockets => {}
            Page::DocketSearch(_) => {}
            Page::DocketsFederal | Page::DocketsState(None) => c.push(("View Dockets".into(), Some(Page::Dockets))),
            Page::DocketsState(Some(_)) => {
                c.push(("View Dockets".into(), Some(Page::Dockets)));
                c.push(("State".into(), Some(Page::DocketsState(None))));
            }
            Page::CourtDockets(id) => {
                c.push(("View Dockets".into(), Some(Page::Dockets)));
                c.extend(self.court_crumbs(id));
            }
            Page::RecapDocket(id) => {
                c.push(("View Dockets".into(), Some(Page::Dockets)));
                if let Some(d) = self.recap.iter().find(|d| d.id == *id) {
                    let court = d.court.clone();
                    c.extend(self.court_crumbs(&court));
                    c.push((self.title_of(&Page::CourtDockets(court.clone())), Some(Page::CourtDockets(court))));
                }
            }
            Page::Case(_) => match self.section_at(self.pos) {
                Section::Cases => c.push(("Built opinions".into(), Some(Page::Cases))),
                Section::Search => {
                    // back to the search the case was opened from
                    let s = self.nav[..self.pos].iter().rev().find(|p| matches!(p, Page::Search { .. })).cloned();
                    if let Some(s) = s {
                        c.push((self.title_of(&s), Some(s)));
                    }
                }
                _ => {
                    let from = self.nav[..self.pos].iter().rev().find(|p| !matches!(p, Page::Case(_))).cloned();
                    if let Some(p) = from {
                        c.push((self.title_of(&p), Some(p)));
                    }
                }
            },
        }
        c.push((self.title_of(&here), Some(here)));
        c
    }

    fn history_panel(&mut self, ui: &mut egui::Ui) {
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.heading("History");
            if ui.small_button("Hide").clicked() {
                self.show_history = false;
            }
        });
        ui.weak("Every page visited, newest first. Click one to go back to it; Back/Forward walk this list.");
        ui.separator();
        let mut jump = None;
        egui::ScrollArea::vertical().id_salt("history").auto_shrink([false, false]).show(ui, |ui| {
            for i in (0..self.nav.len()).rev() {
                let p = &self.nav[i];
                let kind = match p {
                    Page::Home => "start",
                    Page::Areas => "areas",
                    Page::Area(_) => "area",
                    Page::Lineage(_) => "lineage",
                    Page::Slice(_) => "slice",
                    Page::Cluster { .. } => "cluster",
                    Page::Sub { .. } => "sub-cluster",
                    Page::Cases => "cases",
                    Page::Case(_) => "case",
                    Page::Search { .. } => "search",
                    Page::Scotus => "scotus",
                    Page::SemSearch => "semantic search",
                    Page::Check => "check setup",
                    Page::Docket(_) => "docket",
                    Page::Dockets => "dockets",
                    Page::DocketsFederal => "federal courts",
                    Page::DocketsState(_) => "state courts",
                    Page::CourtDockets(_) => "court",
                    Page::RecapDocket(_) => "docket",
                    Page::DocketSearch(_) => "docket search",
                    Page::CoCited(_) => "cited together",
                };
                let t = self.title_of(p);
                let t = if t.chars().count() > 46 { format!("{}…", t.chars().take(45).collect::<String>()) } else { t };
                ui.horizontal(|ui| {
                    ui.weak(kind);
                    let text = if i == self.pos { egui::RichText::new(t).strong() } else { egui::RichText::new(t) };
                    if i == self.pos {
                        ui.label(text);
                    } else if ui.link(text).clicked() {
                        jump = Some(i);
                    }
                });
            }
        });
        if let Some(i) = jump {
            self.pos = i;
            self.arrive();
        }
    }

    fn top_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            if ui.add_enabled(self.pos > 0, egui::Button::new("◀")).on_hover_text("Back (Alt+Left)").clicked() {
                self.back();
            }
            if ui
                .add_enabled(self.pos + 1 < self.nav.len(), egui::Button::new("▶"))
                .on_hover_text("Forward (Alt+Right)")
                .clicked()
            {
                self.forward();
            }
            ui.separator();
            // where this page sits (not where you have been: that is the History panel)
            let crumbs = self.crumbs();
            let n = crumbs.len();
            let mut go = None;
            for (k, (t, p)) in crumbs.into_iter().enumerate() {
                let t = if t.chars().count() > 40 { format!("{}…", t.chars().take(39).collect::<String>()) } else { t };
                if k + 1 == n {
                    ui.strong(t);
                } else {
                    match p {
                        Some(p) => {
                            if ui.link(t).clicked() {
                                go = Some(p);
                            }
                        }
                        None => {
                            ui.weak(t);
                        }
                    }
                    ui.weak("›");
                }
            }
            if go.is_some() {
                self.goto = go;
            }
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                self.mode_picker(ui);
                if ui
                    .button("⧉ Pop out")
                    .on_hover_text("open this page in a window of its own (Ctrl+click a link does the same for the link)")
                    .clicked()
                {
                    let p = self.current().clone();
                    self.pop_out(p);
                }
                if ui
                    .selectable_label(self.show_history, format!("History ({})", self.nav.len()))
                    .on_hover_text("every page visited, in order; click one to go back to it")
                    .clicked()
                {
                    self.show_history = !self.show_history;
                }
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
        self.poll_job();
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
        let section = self.section_at(self.pos);
        if self.run_total > 0 {
            egui::Panel::bottom("status").resizable(false).show(ui, |ui| {
                ui.add_space(2.0);
                self.status_bar(ui);
                ui.add_space(2.0);
            });
        }
        if self.show_log {
            egui::Panel::bottom("log").default_size(220.0).min_size(80.0).resizable(true).show(ui, |ui| {
                self.log_panel(ui);
            });
        }
        if self.show_history {
            egui::Panel::right("history").default_size(300.0).min_size(200.0).max_size(500.0).show(ui, |ui| {
                self.history_panel(ui);
            });
        }
        if section != Section::Home {
            egui::Panel::left("side").default_size(340.0).min_size(220.0).max_size(600.0).show(ui, |ui| {
                pages::side_panel(self, ui, section);
            });
        }
        egui::CentralPanel::default().show(ui, |ui| {
            pages::central(self, ui);
        });
        if let Some(p) = self.goto.take() {
            // Ctrl+click: the link opens in a window of its own
            if ctx.input(|i| i.modifiers.command) {
                self.pop_out(p);
            } else {
                self.go(p);
            }
        }
        self.show_popouts(&ctx);
    }
}

impl App {
    /// Open a page in a new window.
    pub fn pop_out(&mut self, page: Page) {
        self.next_popout += 1;
        self.request(&page);
        self.popouts.push(PopOut { id: self.next_popout, nav: vec![page], pos: 0, filter: String::new(), open: true });
    }

    /// Every pop-out window, drawn by the same page renderers: the window's own history is swapped
    /// into `nav`/`pos` (and its own filter into `filter`) while it draws, so app.current() is its page.
    fn show_popouts(&mut self, ctx: &egui::Context) {
        let mut pops = std::mem::take(&mut self.popouts);
        let mut more = Vec::new();
        for p in pops.iter_mut() {
            let page = p.nav[p.pos].clone();
            if !self.errors.contains_key(&page) {
                self.request(&page);
            }
            let title = self.title_of(&page);
            let vid = egui::ViewportId::from_hash_of(("popout", p.id));
            let builder = egui::ViewportBuilder::default().with_title(&title).with_inner_size([1000.0, 850.0]);
            ctx.show_viewport_immediate(vid, builder, |ui, _class| {
                if ui.ctx().input(|i| i.viewport().close_requested()) {
                    p.open = false;
                }
                let mut nav_to: Option<usize> = None;
                let mut to_main = false;
                egui::Panel::top(egui::Id::new(("popout-top", p.id))).show(ui, |ui| {
                    ui.horizontal(|ui| {
                        if ui.add_enabled(p.pos > 0, egui::Button::new("◀")).on_hover_text("Back (Alt+Left)").clicked() {
                            nav_to = Some(p.pos - 1);
                        }
                        if ui.add_enabled(p.pos + 1 < p.nav.len(), egui::Button::new("▶")).on_hover_text("Forward (Alt+Right)").clicked() {
                            nav_to = Some(p.pos + 1);
                        }
                        ui.strong(&title);
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui.button("Open in main window").clicked() {
                                to_main = true;
                            }
                        });
                    });
                });
                let (alt_l, alt_r, ctrl) = ui.ctx().input(|i| {
                    (i.modifiers.alt && i.key_pressed(egui::Key::ArrowLeft), i.modifiers.alt && i.key_pressed(egui::Key::ArrowRight), i.modifiers.command)
                });
                if alt_l && p.pos > 0 {
                    nav_to = Some(p.pos - 1);
                }
                if alt_r && p.pos + 1 < p.nav.len() {
                    nav_to = Some(p.pos + 1);
                }
                // draw the page as if it were the main window's current page
                let saved_goto = self.goto.take();
                std::mem::swap(&mut self.nav, &mut p.nav);
                std::mem::swap(&mut self.pos, &mut p.pos);
                std::mem::swap(&mut self.filter, &mut p.filter);
                egui::CentralPanel::default().show(ui, |ui| {
                    ui.push_id(("popout", p.id), |ui| pages::central(self, ui));
                });
                std::mem::swap(&mut self.nav, &mut p.nav);
                std::mem::swap(&mut self.pos, &mut p.pos);
                std::mem::swap(&mut self.filter, &mut p.filter);
                let clicked = std::mem::replace(&mut self.goto, saved_goto);
                if let Some(i) = nav_to {
                    p.pos = i;
                } else if let Some(target) = clicked {
                    if ctrl {
                        more.push(target);
                    } else if p.nav[p.pos] != target {
                        p.nav.truncate(p.pos + 1);
                        p.nav.push(target);
                        p.pos = p.nav.len() - 1;
                        p.filter.clear();
                    }
                }
                if to_main {
                    self.goto = Some(p.nav[p.pos].clone());
                }
            });
        }
        pops.retain(|p| p.open);
        // a pop-out opened while these were drawing (pop_out pushes onto self.popouts)
        pops.append(&mut self.popouts);
        self.popouts = pops;
        for t in more {
            self.pop_out(t);
        }
    }
}
