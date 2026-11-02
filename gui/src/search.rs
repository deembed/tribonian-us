//! Search Opinions: full-text search over bulk/search.sqlite (built by casedb.py), on its own
//! worker thread so a slow search (cold disk) never holds up page loads.
//!
//! search.sqlite has two FTS5 tables (FTS5 = SQLite's built-in full-text index):
//!   names(cluster_id UNINDEXED, name)          one row per case
//!   parens(described UNINDEXED, text)          one row per parenthetical a later court wrote about a case
//! Names, citations and years for the hits come from the open cocite view's `names` table.

use crate::db::open_ro;
use crate::model::{Dormancy, GoodLaw, Page, RECENT};
use rusqlite::{Connection, OptionalExtension};
use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Mode {
    /// Case names; a query that looks like a citation ("463 U.S. 29") is looked up as one.
    Name,
    /// What later courts said a case stands for (CourtListener parentheticals).
    Parenthetical,
}

#[derive(Clone, Debug)]
pub struct Hit {
    pub r#ref: i64,
    pub name: String,
    pub cite: String,
    pub year: Option<i64>,
    /// Parenthetical mode: matching parentheticals among those read, and the best one with
    /// matches wrapped in \u{1} ... \u{2}.
    pub count: usize,
    pub snippet: Option<String>,
    pub goodlaw: Option<GoodLaw>,
    /// From the cached profile, else CourtListener's citing count when it is 0.
    pub dormant: Option<Dormancy>,
    /// Citing decisions summed over every slice of the view (0 = never a node: rarely cited).
    pub citing: i64,
    pub court: String,
}

#[derive(Clone, Debug)]
pub struct Results {
    pub hits: Vec<Hit>,
    /// Rows read from the index (names: hits; parentheticals: parentheticals before grouping).
    pub read: usize,
    pub capped: bool,
    pub as_citation: bool,
    pub elapsed: Duration,
    /// Cases matched before the filters, and after them (`hits` is the first SHOW of these).
    pub total: usize,
    pub matched: usize,
    /// Every court among the matches, with its count inside the year range; "" = no court on record.
    pub courts: Vec<(String, usize)>,
}

/// A column the results can be sorted by. Default: the search's own ranking.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum SortKey {
    #[default]
    Default,
    Case,
    Cite,
    Year,
    Court,
    Citing,
    GoodLaw,
    Parentheticals,
}

/// The filters and sort of one search. They apply to every match, before the top SHOW are chosen.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Filter {
    /// The year boxes as typed; either may be blank. A box that isn't a number is ignored.
    pub from: String,
    pub to: String,
    /// Courts unticked, so that a court first seen in newer results starts ticked. "" = no court on record.
    pub excluded: BTreeSet<String>,
    /// The citing boxes, as typed; the count is the Citing column's (blank there = 0).
    pub citing_min: String,
    pub citing_max: String,
    /// Good-law groups unticked: `GOOD_LAW`'s ranks.
    pub law_excluded: BTreeSet<u8>,
    pub sort: SortKey,
    pub asc: bool,
}

/// The Good law list, by rank (`standing_rank`); NOT_COMPUTED stands for a case with no result.
pub const NOT_COMPUTED: u8 = 4;
pub const GOOD_LAW: [(u8, &str); 5] = [(0, "Red"), (1, "Yellow"), (2, "Green"), (3, "Dormant"), (NOT_COMPUTED, "Not computed")];

fn in_range(year: Option<i64>, from: Option<i64>, to: Option<i64>) -> bool {
    if from.is_none() && to.is_none() {
        return true; // no range: a case with no year stays
    }
    year.is_some_and(|y| from.is_none_or(|f| y >= f) && to.is_none_or(|t| y <= t))
}

impl Filter {
    fn years(&self) -> (Option<i64>, Option<i64>) {
        (self.from.trim().parse().ok(), self.to.trim().parse().ok())
    }

    /// `standing`: the good-law rank; read only once `needs_standing`, as it is for a sort by Good law.
    pub fn keeps(&self, year: Option<i64>, court: &str, citing: i64, standing: Option<u8>) -> bool {
        let (from, to) = self.years();
        in_range(year, from, to)
            && !self.excluded.contains(court)
            && self.citing_bounds().0.is_none_or(|m| citing >= m)
            && self.citing_bounds().1.is_none_or(|m| citing <= m)
            && !self.law_excluded.contains(&standing.unwrap_or(NOT_COMPUTED))
    }

    fn citing_bounds(&self) -> (Option<i64>, Option<i64>) {
        (self.citing_min.trim().parse().ok(), self.citing_max.trim().parse().ok())
    }

    pub fn needs_standing(&self) -> bool {
        self.sort == SortKey::GoodLaw || !self.law_excluded.is_empty()
    }

    /// Any filter that can hide a case (the sort can't).
    pub fn narrows(&self) -> bool {
        self.years() != (None, None) || !self.excluded.is_empty() || self.citing_bounds() != (None, None) || !self.law_excluded.is_empty()
    }

    /// A click on a column head: first its natural direction (text A-Z, good law worst first, numbers highest
    /// first), then each click reverses it.
    pub fn click(&mut self, key: SortKey) {
        if self.sort == key {
            self.asc = !self.asc;
        } else {
            self.sort = key;
            self.asc = matches!(key, SortKey::Case | SortKey::Cite | SortKey::Court | SortKey::GoodLaw);
        }
    }
}

/// Results shown.
pub const SHOW: usize = 200;
/// Matches read from the index before ranking. Ranking is by how often the case is cited,
/// which the index does not know, so every match (up to this cap) is read.
pub const READ_CAP: usize = 20_000;

/// Turn what the user typed into an FTS5 query: "quoted phrases" stay phrases, every other word
/// becomes a quoted term (so punctuation can't break the syntax), and in name mode the last word
/// matches as a prefix ("chev" finds Chevron). All terms must match.
pub fn fts_query(input: &str, prefix_last: bool) -> Option<String> {
    let mut parts: Vec<String> = Vec::new();
    let mut rest = input.trim();
    while !rest.is_empty() {
        if let Some(r) = rest.strip_prefix('"') {
            let end = r.find('"').unwrap_or(r.len());
            let phrase = r[..end].trim();
            if !phrase.is_empty() {
                parts.push(format!("\"{}\"", phrase.replace('"', "")));
            }
            rest = r.get(end + 1..).unwrap_or("").trim_start();
        } else {
            let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
            let word: String = rest[..end].chars().filter(|c| c.is_alphanumeric() || *c == '\'' || *c == '-').collect();
            let word = word.trim_matches(|c| c == '\'' || c == '-').to_string();
            if !word.is_empty() && !matches!(word.to_lowercase().as_str(), "v" | "vs") {
                parts.push(format!("\"{word}\""));
            }
            rest = rest[end..].trim_start();
        }
    }
    if parts.is_empty() {
        return None;
    }
    if prefix_last && !input.trim_end().ends_with('"') {
        if let Some(last) = parts.last_mut() {
            last.push('*');
        }
    }
    Some(parts.join(" "))
}

/// "463 U.S. 29", "550 U. S. 544", "927 F.3d 382": a number, a reporter, a number.
pub fn looks_like_cite(q: &str) -> bool {
    let t: Vec<&str> = q.split_whitespace().collect();
    t.len() >= 3
        && t[0].chars().all(|c| c.is_ascii_digit())
        && t[t.len() - 1].chars().all(|c| c.is_ascii_digit())
        && t[1..t.len() - 1].iter().any(|w| w.chars().any(|c| c.is_alphabetic()))
}

pub struct Req {
    pub page: Page,
    pub query: String,
    pub mode: Mode,
    pub view: PathBuf,
    pub filter: Filter,
}

pub struct Resp {
    pub page: Page,
    pub result: Result<Results, String>,
}

fn names_of(view: &Connection, refs: &[i64]) -> HashMap<i64, (String, String, Option<i64>)> {
    let mut out = HashMap::new();
    let Ok(mut st) = view.prepare("SELECT name, cite, year FROM names WHERE ref=?") else { return out };
    for &r in refs {
        if let Ok(Some(v)) = st
            .query_row([r], |x| {
                Ok((
                    x.get::<_, Option<String>>(0)?.unwrap_or_default(),
                    x.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    x.get::<_, Option<i64>>(2)?,
                ))
            })
            .optional()
        {
            out.insert(r, v);
        }
    }
    out
}

/// One matching case, everything known about it before the top SHOW are chosen.
#[derive(Clone, Debug)]
struct Row {
    r#ref: i64,
    /// Name mode: the name that matched. Empty otherwise.
    name: String,
    citing: i64,
    /// Parenthetical mode: matching parentheticals, and the rowid of one for the snippet.
    count: usize,
    rowid: i64,
    year: Option<i64>,
    court: String,
    /// The case name, lower case, for sorting; "" = none.
    sort_name: String,
    /// CourtListener's citing count is 0.
    uncited: bool,
    /// Read only when a sort first needs them (`add_sort_data`): (reporter, volume, page), and the good-law rank.
    cite_key: Option<(String, i64, i64)>,
    standing: Option<u8>,
}

/// What a search matched, kept by the worker so that changing a filter or sort doesn't search again.
struct Matches {
    rows: Vec<Row>,
    read: usize,
    capped: bool,
    as_citation: bool,
    has_cites: bool,
    has_standing: bool,
}

/// Every match's year and court, from the main database (else the year from the view's names table).
/// Asked in cluster order, which keeps a cold disk's seeks short.
fn add_year_court(main: Option<&Connection>, view: &Connection, rows: &mut [Row]) {
    let mut order: Vec<usize> = (0..rows.len()).collect();
    order.sort_by_key(|&i| rows[i].r#ref);
    if let Some(mut st) = main.and_then(|m| {
        m.prepare("SELECT c.date_filed, d.court_id, c.case_name, c.citation_count FROM clusters c LEFT JOIN dockets d ON d.id = c.docket_id WHERE c.id = ?").ok()
    }) {
        for &i in &order {
            if let Ok(Some((date, court, name, cited))) = st
                .query_row([rows[i].r#ref], |x| {
                    Ok((x.get::<_, Option<String>>(0)?, x.get::<_, Option<String>>(1)?, x.get::<_, Option<String>>(2)?, x.get::<_, Option<i64>>(3)?))
                })
                .optional()
            {
                rows[i].year = date.and_then(|d| d.get(..4).and_then(|y| y.parse().ok()));
                rows[i].court = court.unwrap_or_default();
                rows[i].sort_name = name.unwrap_or_default();
                rows[i].uncited = cited == Some(0);
            }
        }
    }
    let Ok(mut st) = view.prepare("SELECT year, name FROM names WHERE ref=?") else { return };
    for row in rows.iter_mut() {
        if !row.name.is_empty() {
            row.sort_name = row.name.clone(); // what the table shows in name mode
        }
        if row.year.is_none() || row.sort_name.is_empty() {
            if let Ok(Some((y, n))) = st.query_row([row.r#ref], |x| Ok((x.get::<_, Option<i64>>(0)?, x.get::<_, Option<String>>(1)?))).optional() {
                row.year = row.year.or(y);
                if row.sort_name.is_empty() {
                    row.sort_name = n.unwrap_or_default();
                }
            }
        }
        row.sort_name = row.sort_name.to_lowercase();
    }
}

/// "463 U.S. 29" -> ("U.S.", 463, 29), so that reporters group and volumes and pages sort as numbers.
fn cite_key(cite: &str) -> Option<(String, i64, i64)> {
    let t: Vec<&str> = cite.split_whitespace().collect();
    if t.len() < 3 {
        return None;
    }
    let number = |w: &str| w.chars().take_while(char::is_ascii_digit).collect::<String>().parse::<i64>().unwrap_or(0);
    Some((t[1..t.len() - 1].join(" "), number(t[0]), number(t[t.len() - 1])))
}

/// Good law, as it reads in the table: red, yellow, the rest, dormant; None = not computed.
fn standing_rank(g: Option<&str>, dormant: bool) -> Option<u8> {
    match (g, dormant) {
        (Some("red"), _) => Some(0),
        (Some("yellow"), _) => Some(1),
        (_, true) => Some(3),
        (Some(_), false) => Some(2),
        (None, false) => None,
    }
}

/// What only the Cite and Good law sorts need, read for every match the first time one of them is asked for.
fn add_sort_data(m: &mut Matches, f: &Filter, main: Option<&Connection>, view: &Connection, cases_dir: &Path) {
    if f.sort == SortKey::Cite && !m.has_cites {
        m.has_cites = true;
        let refs: Vec<i64> = m.rows.iter().map(|r| r.r#ref).collect();
        let shown = names_of(view, &refs);
        let mut st = main.and_then(|c| c.prepare("SELECT volume, reporter, page FROM cites WHERE cluster_id = ? ORDER BY type LIMIT 1").ok());
        for row in m.rows.iter_mut() {
            let mut cite = shown.get(&row.r#ref).map(|x| x.1.clone()).unwrap_or_default();
            if cite.is_empty() {
                if let Some(st) = &mut st {
                    cite = st
                        .query_row([row.r#ref], |x| Ok(format!("{} {} {}", x.get::<_, String>(0)?, x.get::<_, String>(1)?, x.get::<_, String>(2)?)))
                        .unwrap_or_default();
                }
            }
            row.cite_key = cite_key(&cite);
        }
    }
    if f.needs_standing() && !m.has_standing {
        m.has_standing = true;
        for row in m.rows.iter_mut() {
            let (g, d) = crate::db::read_standing(cases_dir, row.r#ref);
            let dormant = d.is_some_and(|d| d.dormant) || (row.uncited && g.is_none());
            row.standing = standing_rank(g.as_ref().map(|g| g.status.as_str()), dormant);
        }
    }
}

fn read_matches(search: &Connection, main: Option<&Connection>, view: &Connection, totals: &HashMap<i64, i64>, req: &Req) -> rusqlite::Result<Matches> {
    let citing = |r: i64| totals.get(&r).copied().unwrap_or(0);
    let row = |r: i64, name: String, count: usize, rowid: i64| Row {
        r#ref: r,
        name,
        citing: citing(r),
        count,
        rowid,
        year: None,
        court: String::new(),
        sort_name: String::new(),
        uncited: false,
        cite_key: None,
        standing: None,
    };
    let none = || Matches { rows: Vec::new(), read: 0, capped: false, as_citation: false, has_cites: false, has_standing: false };
    let mut m = match req.mode {
        Mode::Name if looks_like_cite(&req.query) => {
            let t: Vec<&str> = req.query.split_whitespace().collect();
            let (vol, page) = (t[0], t[t.len() - 1]);
            let reporter = t[1..t.len() - 1].join(" ");
            // the main database's cites table knows every reporter; the view's names table only
            // one citation per case
            let mut refs: Vec<i64> = main.map(|m| crate::db::cite_lookup(m, vol, &reporter, page)).unwrap_or_default();
            if refs.is_empty() && reporter == "U. S." {
                refs = main.map(|m| crate::db::cite_lookup(m, vol, "U.S.", page)).unwrap_or_default();
            }
            if refs.is_empty() {
                let want = format!("{vol} {} {page}", reporter.replace("U. S.", "U.S."));
                let mut st = view.prepare("SELECT ref FROM names WHERE replace(cite, 'U. S.', 'U.S.') = ?")?;
                refs = st.query_map([&want], |x| x.get(0))?.collect::<rusqlite::Result<_>>()?;
            }
            let rows: Vec<Row> = refs.into_iter().map(|r| row(r, String::new(), 0, 0)).collect();
            Matches { read: rows.len(), rows, as_citation: true, ..none() }
        }
        Mode::Name => {
            let Some(q) = fts_query(&req.query, true) else { return Ok(none()) };
            let mut st = search.prepare("SELECT cluster_id, name FROM names WHERE names MATCH ? LIMIT ?")?;
            let mut hits: Vec<(i64, String)> = st
                .query_map(rusqlite::params![q, READ_CAP as i64], |x| {
                    Ok((x.get(0)?, x.get::<_, Option<String>>(1)?.unwrap_or_default()))
                })?
                .collect::<rusqlite::Result<_>>()?;
            let read = hits.len();
            hits.sort_by_key(|r| r.0);
            hits.dedup_by_key(|r| r.0);
            Matches { rows: hits.into_iter().map(|(r, n)| row(r, n, 0, 0)).collect(), read, capped: read >= READ_CAP, ..none() }
        }
        Mode::Parenthetical => {
            let Some(q) = fts_query(&req.query, false) else { return Ok(none()) };
            // every matching parenthetical (up to the cap), counted by the case it describes
            let mut st = search.prepare("SELECT rowid, described FROM parens WHERE parens MATCH ? LIMIT ?")?;
            let hits: Vec<(i64, i64)> = st
                .query_map(rusqlite::params![q, READ_CAP as i64], |x| Ok((x.get(0)?, x.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?;
            let read = hits.len();
            let mut group: HashMap<i64, (usize, i64)> = HashMap::new(); // described -> (count, a rowid)
            for (rowid, r) in hits {
                group.entry(r).or_insert((0, rowid)).0 += 1;
            }
            Matches { rows: group.into_iter().map(|(r, (n, rowid))| row(r, String::new(), n, rowid)).collect(), read, capped: read >= READ_CAP, ..none() }
        }
    };
    add_year_court(main, view, &mut m.rows);
    Ok(m)
}

/// The courts among the matches (blank last), each with its count inside the year range.
fn court_counts(rows: &[Row], f: &Filter) -> Vec<(String, usize)> {
    let mut map: std::collections::BTreeMap<&str, usize> = Default::default();
    let (from, to) = f.years();
    for r in rows {
        let n = map.entry(&r.court).or_insert(0);
        if in_range(r.year, from, to) {
            *n += 1;
        }
    }
    let mut v: Vec<(String, usize)> = map.into_iter().map(|(c, n)| (c.to_string(), n)).collect();
    v.sort_by_key(|(c, _)| c.is_empty()); // stable: names stay in order
    v
}

/// The chosen order. Default: most cited, shorter names first among equals (Name); most parentheticals,
/// then most cited (Parenthetical). Those also break a column's ties.
fn sort_rows(rows: &mut [Row], f: &Filter, mode: Mode) {
    use std::cmp::Ordering;
    // a blank goes last whichever way the column sorts
    fn blank_last<T: Ord>(a: Option<T>, b: Option<T>, asc: bool) -> Ordering {
        match (a, b) {
            (Some(x), Some(y)) => {
                if asc {
                    x.cmp(&y)
                } else {
                    y.cmp(&x)
                }
            }
            (x, y) => y.is_some().cmp(&x.is_some()),
        }
    }
    let text = |s: &str| (!s.is_empty()).then(|| s.to_string());
    rows.sort_by(|a, b| {
        let primary = match f.sort {
            SortKey::Default => Ordering::Equal,
            SortKey::Case => blank_last(text(&a.sort_name), text(&b.sort_name), f.asc),
            SortKey::Cite => blank_last(a.cite_key.clone(), b.cite_key.clone(), f.asc),
            SortKey::Year => blank_last(a.year, b.year, f.asc),
            SortKey::Court => blank_last(text(&a.court), text(&b.court), f.asc),
            SortKey::Citing => blank_last(Some(a.citing), Some(b.citing), f.asc),
            SortKey::GoodLaw => blank_last(a.standing, b.standing, f.asc),
            SortKey::Parentheticals => blank_last(Some(a.count), Some(b.count), f.asc),
        };
        primary.then_with(|| match mode {
            Mode::Name => b.citing.cmp(&a.citing).then(a.name.len().cmp(&b.name.len())),
            Mode::Parenthetical => b.count.cmp(&a.count).then(b.citing.cmp(&a.citing)),
        })
    });
}

fn run(search: &Connection, main: Option<&Connection>, view: &Connection, m: &Matches, cases_dir: &Path, req: &Req, t: Instant) -> rusqlite::Result<Results> {
    // a citation lookup returns so few cases that it has no filters
    let default = Filter::default();
    let f = if m.as_citation { &default } else { &req.filter };
    let mut rows: Vec<Row> = m.rows.iter().filter(|r| f.keeps(r.year, &r.court, r.citing, r.standing)).cloned().collect();
    let matched = rows.len();
    sort_rows(&mut rows, f, req.mode);
    rows.truncate(SHOW);
    let refs: Vec<i64> = rows.iter().map(|r| r.r#ref).collect();
    let meta = names_of(view, &refs);
    let q = fts_query(&req.query, false).unwrap_or_default();
    let mut snip = match req.mode {
        Mode::Parenthetical => Some(search.prepare("SELECT snippet(parens, 1, char(1), char(2), '…', 24) FROM parens WHERE parens MATCH ? AND rowid = ?")?),
        Mode::Name => None,
    };
    let mut hits: Vec<Hit> = Vec::with_capacity(rows.len());
    for row in rows {
        let (short, cite, vyear) = meta.get(&row.r#ref).cloned().unwrap_or_default();
        let snippet: Option<String> = match &mut snip {
            Some(st) => st.query_row(rusqlite::params![q, row.rowid], |x| x.get(0)).optional()?,
            None => None,
        };
        let name = if !row.name.is_empty() {
            row.name
        } else if !short.is_empty() {
            short
        } else {
            format!("cluster {}", row.r#ref)
        };
        hits.push(Hit {
            r#ref: row.r#ref,
            name,
            cite,
            year: row.year.or(vyear),
            count: row.count,
            snippet,
            goodlaw: None,
            dormant: None,
            citing: row.citing,
            court: row.court,
        });
    }
    for h in hits.iter_mut() {
        (h.goodlaw, h.dormant) = crate::db::read_standing(cases_dir, h.r#ref);
        if let Some(d) = main.and_then(|m| crate::db::describe(m, h.r#ref)) {
            if h.dormant.is_none() && d.cited == Some(0) {
                let from = crate::db::current_year() - RECENT;
                h.dormant = Some(Dormancy { dormant: true, last: None, from, source: "CourtListener's citing count is 0".into() });
            }
            if h.name.is_empty() || h.name.starts_with("cluster ") {
                h.name = d.name;
            }
            if h.cite.is_empty() {
                h.cite = d.cite;
            }
        }
    }
    Ok(Results {
        hits,
        read: m.read,
        capped: m.capped,
        as_citation: m.as_citation,
        elapsed: t.elapsed(),
        total: m.rows.len(),
        matched,
        courts: court_counts(&m.rows, f),
    })
}

/// Spawn the search worker. It opens search.sqlite (and the view, for names) on first use.
pub fn spawn(search_db: PathBuf, main_db: PathBuf, cases_dir: PathBuf, wake: impl Fn() + Send + 'static) -> (Sender<Req>, Receiver<Resp>) {
    let (tx, rx) = channel::<Req>();
    let (tx_out, rx_out) = channel::<Resp>();
    std::thread::spawn(move || {
        let mut search: Option<Connection> = None;
        // optional: names, citations and courts for cases outside the view
        let main: Option<Connection> =
            open_ro(&main_db).ok().filter(|c| c.query_row("SELECT 1 FROM clusters LIMIT 1", [], |_| Ok(())).is_ok());
        let mut view: Option<(PathBuf, Connection, HashMap<i64, i64>)> = None;
        // the last search's matches: a change of filter or sort reads them again, not the index
        let mut last: Option<(String, Mode, PathBuf, Matches)> = None;
        while let Ok(mut req) = rx.recv() {
            // only the newest request matters: drop any that queued up behind a slow one
            while let Ok(newer) = rx.try_recv() {
                req = newer;
            }
            let result = (|| -> Result<Results, String> {
                if search.is_none() {
                    search = Some(open_ro(&search_db).map_err(|e| format!("{}: {e}", search_db.display()))?);
                }
                if view.as_ref().map(|v| &v.0) != Some(&req.view) {
                    let c = open_ro(&req.view).map_err(|e| format!("{}: {e}", req.view.display()))?;
                    // how often each case is cited, summed over slices: the search ranking (read once per view)
                    let mut totals = HashMap::new();
                    let mut st = c.prepare("SELECT ref, sum(n) FROM nodes GROUP BY ref").map_err(|e| e.to_string())?;
                    let rows = st.query_map([], |x| Ok((x.get::<_, i64>(0)?, x.get::<_, i64>(1)?))).map_err(|e| e.to_string())?;
                    for (r, n) in rows.flatten() {
                        totals.insert(r, n);
                    }
                    drop(st);
                    view = Some((req.view.clone(), c, totals));
                }
                let t = Instant::now();
                let v = view.as_ref().unwrap();
                let s = search.as_ref().unwrap();
                if !last.as_ref().is_some_and(|l| l.0 == req.query && l.1 == req.mode && l.2 == req.view) {
                    let m = read_matches(s, main.as_ref(), &v.1, &v.2, &req).map_err(|e| e.to_string())?;
                    last = Some((req.query.clone(), req.mode, req.view.clone(), m));
                }
                let m = &mut last.as_mut().unwrap().3;
                add_sort_data(m, &req.filter, main.as_ref(), &v.1, &cases_dir);
                run(s, main.as_ref(), &v.1, m, &cases_dir, &req, t).map_err(|e| e.to_string())
            })();
            if tx_out.send(Resp { page: req.page, result }).is_err() {
                break;
            }
            wake();
        }
    });
    (tx, rx_out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn queries() {
        assert_eq!(fts_query("chev", true).as_deref(), Some("\"chev\"*"));
        assert_eq!(fts_query("State Farm v. Motor", true).as_deref(), Some("\"State\" \"Farm\" \"Motor\"*"));
        assert_eq!(fts_query("\"hard look\" agency", false).as_deref(), Some("\"hard look\" \"agency\""));
        assert_eq!(fts_query("\"arbitrary and capricious\"", true).as_deref(), Some("\"arbitrary and capricious\""));
        assert_eq!(fts_query("  ", true), None);
        assert_eq!(fts_query("NOT OR (", false).as_deref(), Some("\"NOT\" \"OR\""));
    }

    fn row(r: i64, year: Option<i64>, court: &str, citing: i64) -> Row {
        Row { r#ref: r, name: String::new(), citing, count: 0, rowid: 0, year, court: court.into(), sort_name: String::new(), uncited: false, cite_key: None, standing: None }
    }

    #[test]
    fn filters() {
        let keeps = |f: &Filter, y, c| f.keeps(y, c, 0, None);
        let mut f = Filter::default();
        assert!(keeps(&f, None, ""), "no filter keeps everything");
        f.from = "1990".into();
        f.to = "1995".into();
        assert!(keeps(&f, Some(1990), "ca4") && keeps(&f, Some(1995), "ca4"));
        assert!(!keeps(&f, Some(1989), "ca4") && !keeps(&f, Some(1996), "ca4"));
        assert!(!keeps(&f, None, "ca4"), "a year range drops cases with no year");
        f.to.clear();
        assert!(keeps(&f, Some(2020), "ca4"), "an open end");
        f.from = "19x".into();
        assert!(keeps(&f, None, "ca4") && !f.narrows(), "a box that is not a number is ignored");
        f.excluded.insert("ca4".into());
        assert!(!keeps(&f, Some(2020), "ca4") && keeps(&f, Some(2020), "scotus"));
        f.excluded.insert(String::new());
        assert!(!keeps(&f, Some(2020), ""));

        let f = Filter { citing_min: "10".into(), citing_max: "20".into(), ..Default::default() };
        assert!(f.keeps(None, "", 10, None) && f.keeps(None, "", 20, None));
        assert!(!f.keeps(None, "", 9, None) && !f.keeps(None, "", 21, None) && f.narrows());
        assert!(!f.needs_standing());

        let f = Filter { law_excluded: [0, NOT_COMPUTED].into(), ..Default::default() };
        assert!(f.needs_standing() && f.narrows());
        assert!(!f.keeps(None, "", 0, Some(0)) && !f.keeps(None, "", 0, None));
        assert!(f.keeps(None, "", 0, Some(2)));
    }

    #[test]
    fn column_clicks() {
        let mut f = Filter::default();
        f.click(SortKey::Year);
        assert_eq!((f.sort, f.asc), (SortKey::Year, false));
        f.click(SortKey::Year);
        assert_eq!((f.sort, f.asc), (SortKey::Year, true));
        f.click(SortKey::Year);
        assert_eq!((f.sort, f.asc), (SortKey::Year, false), "a toggle: only the two directions");
        f.click(SortKey::Case);
        assert_eq!((f.sort, f.asc), (SortKey::Case, true));
        f.click(SortKey::Citing);
        assert_eq!((f.sort, f.asc), (SortKey::Citing, false));
    }

    #[test]
    fn cite_keys_and_good_law() {
        assert_eq!(cite_key("463 U.S. 29"), Some(("U.S.".to_string(), 463, 29)));
        assert_eq!(cite_key("927 F.3d 382"), Some(("F.3d".to_string(), 927, 382)));
        assert_eq!(cite_key("140 S. Ct. 1891"), Some(("S. Ct.".to_string(), 140, 1891)));
        assert_eq!(cite_key(""), None);
        assert!(cite_key("9 U.S. 200") < cite_key("10 U.S. 1"), "volumes sort as numbers");
        assert_eq!(standing_rank(Some("red"), true), Some(0));
        assert_eq!(standing_rank(Some("green"), true), Some(3), "the table shows dormant");
        assert_eq!(standing_rank(Some("green"), false), Some(2));
        assert_eq!(standing_rank(None, false), None);
    }

    #[test]
    fn sorting() {
        let rows = || vec![row(1, Some(2000), "scotus", 5), row(2, None, "", 50), row(3, Some(1990), "ca4", 10)];
        let order = |f: &Filter| {
            let mut r = rows();
            sort_rows(&mut r, f, Mode::Name);
            r.iter().map(|x| x.r#ref).collect::<Vec<_>>()
        };
        let mut f = Filter::default();
        assert_eq!(order(&f), [2, 3, 1], "default: most cited");
        f.click(SortKey::Year);
        assert_eq!(order(&f), [1, 3, 2], "newest first, blank last");
        f.click(SortKey::Year);
        assert_eq!(order(&f), [3, 1, 2], "oldest first, blank still last");
        let mut f = Filter::default();
        f.click(SortKey::Court);
        assert_eq!(order(&f), [3, 1, 2]);
        f.click(SortKey::Court);
        assert_eq!(order(&f), [1, 3, 2], "blank court still last");
        let courts = court_counts(&rows(), &Filter { from: "1995".into(), ..Default::default() });
        assert_eq!(courts, [("ca4".to_string(), 0), ("scotus".to_string(), 1), (String::new(), 0)], "every court, counted in the range");
    }

    /// The whole path on a small database: read every match, filter, sort, keep the top SHOW.
    #[test]
    fn filter_before_the_cut() {
        let search = Connection::open_in_memory().unwrap();
        search.execute_batch("CREATE VIRTUAL TABLE names USING fts5(cluster_id UNINDEXED, name)").unwrap();
        let main = Connection::open_in_memory().unwrap();
        main.execute_batch(
            "CREATE TABLE dockets(id, court_id, case_name); CREATE TABLE cites(cluster_id, volume, reporter, page, type);
             CREATE TABLE clusters(id, docket_id, case_name, case_name_short, case_name_full, date_filed, citation_count);
             INSERT INTO dockets VALUES (1, 'ca4', ''), (2, 'scotus', '');",
        )
        .unwrap();
        let view = Connection::open_in_memory().unwrap();
        view.execute_batch("CREATE TABLE names(ref, name, cite, year)").unwrap();
        let mut totals = HashMap::new();
        // the most cited cases are all scotus; the one ca4 case would be cut by a filter applied after the top SHOW
        for r in 0..=SHOW as i64 {
            search.execute("INSERT INTO names VALUES (?, ?)", rusqlite::params![r, format!("Smith {r}")]).unwrap();
            let (docket, year) = if r == SHOW as i64 { (1, 1992) } else { (2, 2000) };
            main.execute("INSERT INTO clusters VALUES (?, ?, 'Smith', '', '', ?, 1)", rusqlite::params![r, docket, format!("{year}-01-01")]).unwrap();
            totals.insert(r, 1000 - r);
        }
        let req = |filter: Filter| Req { page: Page::Search { query: "smith".into(), mode: Mode::Name }, query: "smith".into(), mode: Mode::Name, view: PathBuf::new(), filter };
        let m = read_matches(&search, Some(&main), &view, &totals, &req(Filter::default())).unwrap();
        assert_eq!(m.rows[1].sort_name, "smith 1");
        let go = |f: Filter| run(&search, Some(&main), &view, &m, Path::new("/nonexistent"), &req(f), Instant::now()).unwrap();

        let all = go(Filter::default());
        assert_eq!((all.total, all.matched, all.hits.len()), (SHOW + 1, SHOW + 1, SHOW));
        assert!(all.hits.iter().all(|h| h.court == "scotus"), "the ca4 case is below the cut");
        assert_eq!(all.courts, [("ca4".to_string(), 1), ("scotus".to_string(), SHOW)]);

        let f = Filter { excluded: ["scotus".to_string()].into(), ..Default::default() };
        let r = go(f);
        assert_eq!((r.matched, r.hits.len(), r.hits[0].r#ref), (1, 1, SHOW as i64));
        assert_eq!((r.hits[0].year, r.hits[0].court.as_str()), (Some(1992), "ca4"));

        let r = go(Filter { from: "1990".into(), to: "1995".into(), ..Default::default() });
        assert_eq!((r.matched, r.hits[0].r#ref), (1, SHOW as i64));

        let mut f = Filter::default();
        f.click(SortKey::Year);
        f.click(SortKey::Year); // oldest first
        assert_eq!(go(f).hits[0].r#ref, SHOW as i64, "a sort reaches below the cut too");

        let r = go(Filter { citing_min: "1000".into(), ..Default::default() });
        assert_eq!((r.matched, r.hits[0].r#ref), (1, 0), "only case 0 has 1000 citing");

        let mut f = Filter::default();
        f.click(SortKey::Case); // A-Z: "smith 0", "smith 1", "smith 10", ...
        assert_eq!(go(f).hits[2].r#ref, 10);
    }

    #[test]
    fn cites() {
        assert!(looks_like_cite("463 U.S. 29"));
        assert!(looks_like_cite("927 F.3d 382"));
        assert!(looks_like_cite("140 S. Ct. 1891"));
        assert!(!looks_like_cite("state farm"));
        assert!(!looks_like_cite("1983 civil rights"));
    }
}
