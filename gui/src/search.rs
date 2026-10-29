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
use std::collections::HashMap;
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

fn run(search: &Connection, main: Option<&Connection>, view: &Connection, totals: &HashMap<i64, i64>, cases_dir: &Path, req: &Req) -> rusqlite::Result<Results> {
    let t = Instant::now();
    let mut hits: Vec<Hit> = Vec::new();
    let read;
    let mut capped = false;
    let mut as_citation = false;
    match req.mode {
        Mode::Name if looks_like_cite(&req.query) => {
            as_citation = true;
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
            let meta = names_of(view, &refs);
            for r in refs {
                let (name, cite, year) = meta.get(&r).cloned().unwrap_or_default();
                hits.push(Hit {
                    r#ref: r,
                    name,
                    cite,
                    year,
                    count: 0,
                    snippet: None,
                    goodlaw: None,
                    dormant: None,
                    citing: totals.get(&r).copied().unwrap_or(0),
                    court: String::new(),
                });
            }
            read = hits.len();
        }
        Mode::Name => {
            let Some(q) = fts_query(&req.query, true) else { return Ok(empty(t)) };
            let mut st = search.prepare("SELECT cluster_id, name FROM names WHERE names MATCH ? LIMIT ?")?;
            let mut rows: Vec<(i64, String)> = st
                .query_map(rusqlite::params![q, READ_CAP as i64], |x| {
                    Ok((x.get(0)?, x.get::<_, Option<String>>(1)?.unwrap_or_default()))
                })?
                .collect::<rusqlite::Result<_>>()?;
            read = rows.len();
            capped = rows.len() >= READ_CAP;
            rows.sort_by_key(|r| r.0);
            rows.dedup_by_key(|r| r.0);
            // most cited first; among the uncited, shorter names first (closer matches)
            rows.sort_by(|a, b| {
                let (ca, cb) = (totals.get(&a.0).copied().unwrap_or(0), totals.get(&b.0).copied().unwrap_or(0));
                cb.cmp(&ca).then(a.1.len().cmp(&b.1.len()))
            });
            rows.truncate(SHOW);
            let refs: Vec<i64> = rows.iter().map(|r| r.0).collect();
            let meta = names_of(view, &refs);
            for (r, name) in rows {
                let (short, cite, year) = meta.get(&r).cloned().unwrap_or_default();
                hits.push(Hit {
                    r#ref: r,
                    name: if name.is_empty() { short } else { name },
                    cite,
                    year,
                    count: 0,
                    snippet: None,
                    goodlaw: None,
                    dormant: None,
                    citing: totals.get(&r).copied().unwrap_or(0),
                    court: String::new(),
                });
            }
        }
        Mode::Parenthetical => {
            let Some(q) = fts_query(&req.query, false) else { return Ok(empty(t)) };
            // every matching parenthetical (up to the cap), counted by the case it describes
            let mut st = search.prepare("SELECT rowid, described FROM parens WHERE parens MATCH ? LIMIT ?")?;
            let rows: Vec<(i64, i64)> = st
                .query_map(rusqlite::params![q, READ_CAP as i64], |x| Ok((x.get(0)?, x.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?;
            read = rows.len();
            capped = rows.len() >= READ_CAP;
            let mut group: HashMap<i64, (usize, i64)> = HashMap::new(); // described -> (count, a rowid)
            for (rowid, r) in rows {
                group.entry(r).or_insert((0, rowid)).0 += 1;
            }
            let mut order: Vec<i64> = group.keys().copied().collect();
            order.sort_by(|a, b| {
                group[b].0.cmp(&group[a].0).then(totals.get(b).copied().unwrap_or(0).cmp(&totals.get(a).copied().unwrap_or(0)))
            });
            order.truncate(SHOW);
            let meta = names_of(view, &order);
            let mut snip = search.prepare(
                "SELECT snippet(parens, 1, char(1), char(2), '…', 24) FROM parens WHERE parens MATCH ? AND rowid = ?",
            )?;
            for r in order {
                let (count, rowid) = group[&r];
                let sn: Option<String> = snip.query_row(rusqlite::params![q, rowid], |x| x.get(0)).optional()?;
                let (name, cite, year) = meta.get(&r).cloned().unwrap_or_else(|| (format!("cluster {r}"), String::new(), None));
                hits.push(Hit {
                    r#ref: r,
                    name,
                    cite,
                    year,
                    count,
                    snippet: sn,
                    goodlaw: None,
                    dormant: None,
                    citing: totals.get(&r).copied().unwrap_or(0),
                    court: String::new(),
                });
            }
        }
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
            h.year = h.year.or(d.year);
            h.court = d.court;
        }
    }
    Ok(Results { hits, read, capped, as_citation, elapsed: t.elapsed() })
}

fn empty(t: Instant) -> Results {
    Results { hits: Vec::new(), read: 0, capped: false, as_citation: false, elapsed: t.elapsed() }
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
                let v = view.as_ref().unwrap();
                run(search.as_ref().unwrap(), main.as_ref(), &v.1, &v.2, &cases_dir, &req).map_err(|e| e.to_string())
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

    #[test]
    fn cites() {
        assert!(looks_like_cite("463 U.S. 29"));
        assert!(looks_like_cite("927 F.3d 382"));
        assert!(looks_like_cite("140 S. Ct. 1891"));
        assert!(!looks_like_cite("state farm"));
        assert!(!looks_like_cite("1983 civil rights"));
    }
}
