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
    Search { query: String, mode: crate::search::Mode },
    /// Supreme Court dockets fetched by scotus.py, and its recent-activity summary
    Scotus,
    /// one docket, by number ("25-579")
    Docket(String),
}

impl Page {
    /// Which start-screen section a page belongs to (drives the side panel).
    pub fn section(&self) -> Section {
        match self {
            Page::Home => Section::Home,
            Page::Cases => Section::Cases,
            Page::Search { .. } => Section::Search,
            Page::Scotus | Page::Docket(_) => Section::Scotus,
            _ => Section::Clusters,
        }
    }
    /// Pages that need the database (everything but the start screen and the case list).
    pub fn needs_db(&self) -> bool {
        !matches!(self, Page::Home | Page::Cases | Page::Search { .. } | Page::Scotus | Page::Docket(_))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Section {
    Home,
    Clusters,
    Cases,
    Search,
    Scotus,
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
    /// Citing decisions in this slice (nodes.n), where the page has a slice.
    pub citing: Option<i64>,
    /// From cases/citedby/<ref>/profile.json, when casemap.py goodlaw has run for the case.
    pub goodlaw: Option<GoodLaw>,
    /// From the same profile's citations per year, when it is cached.
    pub dormant: Option<Dormancy>,
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

/// One warning casemap.py found about the case in a citing opinion (goodlaw "evidence").
#[derive(Clone, Debug, Default)]
pub struct Evidence {
    /// the citing case (CourtListener cluster)
    pub cluster: i64,
    pub name: String,
    pub court: String,
    pub year: String,
    /// true: from a court that could overrule the case (its level or above)
    pub above: bool,
    /// "strong" | "other grounds" | "weak"
    pub kind: String,
    pub word: String,
    /// the words around the citation; the citation itself is marked ⟦…⟧
    pub snippet: String,
}

#[derive(Clone, Debug, Default)]
pub struct GoodLaw {
    pub status: String,
    pub yellow_year: Option<i64>,
    pub red_year: Option<i64>,
    pub read: Option<i64>,
    pub of: Option<i64>,
    pub stamp: Option<String>,
    /// the case's court level: supreme | circuit | district | other
    pub level: String,
    /// casemap.py GOODLAW_VERSION the result was computed with
    pub version: Option<i64>,
    /// the first 25 (casemap.py keeps no more), strongest first
    pub evidence: Vec<Evidence>,
    pub n_evidence: i64,
}

/// casemap.py GOODLAW_MAX and GOODLAW_RECENT: what the good-law check reads.
pub const GOODLAW_MAX: i64 = 1500;
pub const GOODLAW_RECENT: i64 = 500;
/// case2md.py RECENT: "recent" = the last 5 full years before the bulk data's date.
pub const RECENT: i64 = 5;

/// Whether a case is still being cited (grey "dormant" when it is not).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dormancy {
    pub dormant: bool,
    /// newest year a decision cited it; None = never cited (or not known, when `dormant` is false)
    pub last: Option<i64>,
    /// first year of the recent window
    pub from: i64,
    /// where the answer comes from, for the hover text
    pub source: String,
}

impl Dormancy {
    /// From citations per year (a cached profile): exact. `data_year` is the bulk data's year;
    /// the window is the RECENT full years before it (a citation in the data's own, partial year counts too).
    pub fn from_years(years: &[(i64, i64)], data_year: i64, source: &str) -> Dormancy {
        let from = data_year - RECENT;
        let last = years.iter().filter(|y| y.1 > 0).map(|y| y.0).max();
        Dormancy { dormant: last.is_none_or(|l| l < from), last, from, source: source.into() }
    }
    /// "never cited" / "last cited 2007"
    pub fn reason(&self) -> String {
        match self.last {
            None => "never cited".into(),
            Some(y) => format!("last cited {y}"),
        }
    }
    pub fn hover(&self) -> String {
        format!(
            "Dormant: {}; no decision cited it in {}–{} or later (the last {RECENT} full years before the bulk data's date). Source: {}.",
            self.reason(),
            self.from,
            self.from + RECENT - 1,
            self.source
        )
    }
}

/// The year of a "2026-06-30" stamp.
pub fn stamp_year(stamp: &str) -> Option<i64> {
    stamp.get(..4)?.parse().ok()
}

#[derive(Clone, Debug)]
pub struct CaseData {
    pub r#ref: i64,
    pub name: String,
    pub cite: String,
    pub year: Option<i64>,
    /// CourtListener court id (from the main database), "" when unknown.
    pub court: String,
    pub slices: Vec<CaseSlice>,
    /// Sub-cluster lineage of its newest slice, and that lineage's area.
    pub lineage: Option<(i64, String)>,
    pub area: Option<(i64, String)>,
    pub partner_slice: Option<i64>,
    pub partners: Vec<Partner>,
    pub goodlaw: Option<GoodLaw>,
    /// Cached profile's years, else CourtListener's count (0), else a live check (small cases only).
    pub dormant: Option<Dormancy>,
    /// From the main database, for any case.
    pub details: Option<Details>,
    /// cases/citedby/<ref>/profile.json if a build cached it, else what can be read quickly.
    pub profile: Option<Profile>,
    /// Where the cases it cites sit (computed live, as cocite.draws_on does).
    pub draws: Option<DrawsOn>,
    /// cases/citedby/<ref>/coupling.json, written by `casemap.py coupling` (explicit only).
    pub coupling: Option<Coupling>,
}

/// One sub-cluster the case's references sit in, with how many of them.
#[derive(Clone, Debug)]
pub struct DrawsSub {
    pub unit: UnitRow,
    pub n: i64,
    pub area: Option<(i64, String)>,
}

/// "What it draws on": each cited case's home sub-cluster in the newest full slice (bibliographic
/// coupling's cheap half; the same definition as cocite.draws_on).
#[derive(Clone, Debug, Default)]
pub struct DrawsOn {
    pub slice: i64,
    /// distinct cases it cites
    pub refs: i64,
    /// of those, how many are in no sub-cluster of that slice
    pub unclustered: i64,
    pub subs: Vec<DrawsSub>,
    /// (area, name, references in it), largest first
    pub areas: Vec<(i64, String, i64)>,
}

#[derive(Clone, Debug)]
pub struct CoupledRef {
    pub r#ref: i64,
    pub name: String,
    /// decisions citing it (its document frequency)
    pub df: i64,
}

#[derive(Clone, Debug)]
pub struct Coupled {
    pub r#ref: i64,
    pub name: String,
    pub cite: String,
    pub year: String,
    pub court: String,
    pub score: f64,
    pub shared: i64,
    pub refs: Vec<CoupledRef>,
}

/// coupling.json: the cases sharing the most (rare) references with this one.
#[derive(Clone, Debug, Default)]
pub struct Coupling {
    pub computed: String,
    pub stamp: String,
    pub refs: i64,
    pub skipped: i64,
    pub max_df: i64,
    pub n: i64,
    pub results: Vec<Coupled>,
}

#[derive(Clone, Debug, Default)]
pub struct Details {
    pub citations: Vec<String>,
    pub docket: String,
    pub date_filed: String,
    pub judges: String,
    /// CourtListener's own count of citing cases (clusters.citation_count).
    pub cl_cited: Option<i64>,
    pub precedential: String,
    /// The published report's headmatter (parties, docket, dates, counsel) as plain text.
    pub headmatter: String,
    pub syllabus: String,
    pub summary: String,
}

#[derive(Clone, Debug)]
pub struct ParenGroup {
    pub n: i64,
    pub text: String,
    pub r#ref: Option<i64>,
    pub case: String,
    pub year: String,
    pub court: String,
}

#[derive(Clone, Debug, Default)]
pub struct Profile {
    /// true: read from profile.json (a build wrote it); false: computed just now, parentheticals only.
    pub cached: bool,
    pub stamp: String,
    /// citing cases per year (cached profiles only)
    pub years: Vec<(i64, i64)>,
    pub parens_total: i64,
    pub groups: Vec<ParenGroup>,
    /// cocite's likely successor: (slice, name, ref, share, citing)
    pub successor: Option<(String, String, Option<i64>, f64, i64)>,
}

/// An area's lineages slice by slice, for the history graph.
#[derive(Clone, Debug)]
pub struct GraphCell {
    pub lineage: i64,
    pub slice: i64,
    pub k: i64,
    pub size: i64,
}

#[derive(Clone, Debug)]
pub struct GraphEdge {
    pub from: (i64, i64),
    pub to: (i64, i64),
    pub jaccard: f64,
}

/// A `links` row with one end in the area and the other in a lineage outside it.
#[derive(Clone, Debug)]
pub struct OutEdge {
    pub here: (i64, i64),
    /// true: cases came in from the other lineage (it is in the earlier slice)
    pub incoming: bool,
    pub other_slice: i64,
    pub other_lineage: i64,
    pub other_name: String,
    pub other_area: Option<String>,
    pub jaccard: f64,
}

#[derive(Clone, Debug, Default)]
pub struct GraphData {
    pub cells: Vec<GraphCell>,
    pub edges: Vec<GraphEdge>,
    pub outside: Vec<OutEdge>,
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
        graph: GraphData,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dormancy_window() {
        // stamp 2026-06-30: the recent window is 2021-2025 (and the partial 2026)
        let d = Dormancy::from_years(&[(1999, 3), (2020, 1)], 2026, "t");
        assert!(d.dormant && d.last == Some(2020) && d.from == 2021);
        assert!(!Dormancy::from_years(&[(2021, 1)], 2026, "t").dormant);
        let never = Dormancy::from_years(&[], 2026, "t");
        assert!(never.dormant && never.reason() == "never cited");
    }
}
