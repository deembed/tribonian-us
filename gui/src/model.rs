//! What the views draw: plain data, filled by the worker thread from the database.

/// One place in the app. Navigation is a stack of these (back/forward).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Page {
    Home,
    Areas,
    Area(i64),
    Lineage(i64),
    Slice(i64),
    Cluster { slice: i64, cl: i64 },
    Sub { slice: i64, sub: i64 },
    Cases,
    Case(i64),
}

impl Page {
    /// Which start-screen section a page belongs to (drives the side panel).
    pub fn section(&self) -> Section {
        match self {
            Page::Home => Section::Home,
            Page::Cases => Section::Cases,
            _ => Section::Clusters,
        }
    }
    /// Pages that need the database (everything but the start screen and the case list).
    pub fn needs_db(&self) -> bool {
        !matches!(self, Page::Home | Page::Cases)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Section {
    Home,
    Clusters,
    Cases,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Cluster,
    Sub,
}

impl Level {
    pub fn from_db(s: &str) -> Level {
        if s == "sub" {
            Level::Sub
        } else {
            Level::Cluster
        }
    }
}

#[derive(Clone, Debug)]
pub struct SliceInfo {
    pub slice: i64,
    pub label: String,
    pub partial: bool,
}

/// A view file found in bulk_dir, with whether this app can read it.
#[derive(Clone, Debug)]
pub struct ViewInfo {
    pub stem: String,
    pub path: std::path::PathBuf,
    pub schema: String,
    pub ok: bool,
}

#[derive(Clone, Debug)]
pub struct ViewMeta {
    pub stem: String,
    pub slices: Vec<SliceInfo>,
    pub newest_full: i64,
}

/// A cluster or sub-cluster in one slice, as a row in a list.
#[derive(Clone, Debug)]
pub struct UnitRow {
    pub level: Level,
    pub slice: i64,
    pub k: i64,
    pub size: i64,
    pub name: String,
    pub lineage: i64,
}

impl UnitRow {
    pub fn page(&self) -> Page {
        match self.level {
            Level::Cluster => Page::Cluster { slice: self.slice, cl: self.k },
            Level::Sub => Page::Sub { slice: self.slice, sub: self.k },
        }
    }
    /// "2010-14:3" or "2010-14:3.2", as cocite.py prints it.
    pub fn code(&self, slice_label: &str) -> String {
        format!("{}:{}", slice_label, unit_code(self.level, self.k))
    }
}

pub fn unit_code(level: Level, k: i64) -> String {
    match level {
        Level::Cluster => format!("{k}"),
        Level::Sub if k % 1000 == 0 => format!("{}", k / 1000),
        Level::Sub => format!("{}.{}", k / 1000, k % 1000),
    }
}

/// Area numbers use the same cl*1000+j scheme as sub-clusters.
pub fn area_code(a: i64) -> String {
    unit_code(Level::Sub, a)
}

#[derive(Clone, Debug)]
pub struct Label {
    pub name: String,
    pub description: String,
    pub status: Option<String>,
}

#[derive(Clone, Debug)]
pub struct CaseRow {
    pub r#ref: i64,
    pub name: String,
    pub cite: String,
    pub year: Option<i64>,
    pub weight: f64,
    /// Extra column, e.g. "home" or "share 0.32".
    pub note: String,
}

#[derive(Clone, Debug)]
pub struct AreaRow {
    pub area: i64,
    pub name: String,
    pub labelled: bool,
    pub lineages: i64,
    pub weight: f64,
    pub first: i64,
    pub last: i64,
}

#[derive(Clone, Debug)]
pub struct LineageRow {
    pub lineage: i64,
    pub level: Level,
    pub name: String,
    pub labelled: bool,
    pub first: i64,
    pub last: i64,
    pub weight: f64,
    pub confidence: Option<f64>,
}

#[derive(Clone, Debug)]
pub struct LinkRow {
    pub other: UnitRow,
    pub here_slice: i64,
    pub jaccard: f64,
    pub shared: i64,
}

#[derive(Clone, Debug)]
pub struct UnitHead {
    pub unit: UnitRow,
    pub label: Option<Label>,
    pub lineage_first: Option<i64>,
    pub lineage_last: Option<i64>,
    pub area: Option<(i64, String)>,
    pub parent: Option<UnitRow>,
}

#[derive(Clone, Debug)]
pub struct CaseSlice {
    pub slice: i64,
    pub n: i64,
    pub home: Option<UnitRow>,
    pub sub: Option<UnitRow>,
    pub sub_weight: f64,
    /// Other clusters holding at least --share of its weight.
    pub also: Vec<(UnitRow, f64)>,
    pub home_share: Option<f64>,
}

#[derive(Clone, Debug)]
pub struct Partner {
    pub r#ref: i64,
    pub name: String,
    pub cite: String,
    pub year: Option<i64>,
    pub support: f64,
    pub lift: f64,
    pub cosine: f64,
}

#[derive(Clone, Debug, Default)]
pub struct GoodLaw {
    pub status: String,
    pub yellow_year: Option<i64>,
    pub red_year: Option<i64>,
    pub read: Option<i64>,
    pub of: Option<i64>,
    pub stamp: Option<String>,
}

#[derive(Clone, Debug)]
pub struct CaseData {
    pub r#ref: i64,
    pub name: String,
    pub cite: String,
    pub year: Option<i64>,
    pub slices: Vec<CaseSlice>,
    /// Sub-cluster lineage of its newest slice, and that lineage's area.
    pub lineage: Option<(i64, String)>,
    pub area: Option<(i64, String)>,
    pub partner_slice: Option<i64>,
    pub partners: Vec<Partner>,
    pub goodlaw: Option<GoodLaw>,
}

#[derive(Clone, Debug)]
pub enum PageData {
    Areas(Vec<AreaRow>),
    Area {
        area: i64,
        name: String,
        label: Option<Label>,
        first: i64,
        last: i64,
        lineages: Vec<LineageRow>,
        core: Vec<CaseRow>,
    },
    Lineage {
        lineage: i64,
        level: Level,
        name: String,
        label: Option<Label>,
        anchor: String,
        area: Option<(i64, String)>,
        cells: Vec<UnitRow>,
        merged_in: Vec<LinkRow>,
        split_out: Vec<LinkRow>,
    },
    Slice {
        slice: i64,
        clusters: Vec<(UnitRow, i64)>, // cluster, number of sub-clusters
    },
    Unit {
        head: UnitHead,
        children: Vec<UnitRow>,
        cases: Vec<CaseRow>,
    },
    Case(CaseData),
}

impl PageData {
    /// Short title for the breadcrumb trail.
    pub fn title(&self, slice_label: &dyn Fn(i64) -> String) -> String {
        match self {
            PageData::Areas(_) => "Areas".into(),
            PageData::Area { name, .. } => name.clone(),
            PageData::Lineage { name, .. } => name.clone(),
            PageData::Slice { slice, .. } => slice_label(*slice),
            PageData::Unit { head, .. } => format!("{} {}", head.unit.code(&slice_label(head.unit.slice)), head.unit.name),
            PageData::Case(c) => c.name.clone(),
        }
    }
}
