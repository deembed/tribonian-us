//! Read-only access to one cocite view, on a worker thread so the UI never waits on the disk.
//!
//! Every query mirrors something cocite.py already prints (cl_title, unit_name, label_of,
//! area_name, cmd_tree, cmd_lineage); nothing here computes new results.

use crate::model::*;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{channel, Receiver, Sender};

/// The cocite.py results schema this app was written for (cocite.py SCHEMA_VERSION).
pub const SCHEMA: &str = "4";

/// Partners and case lists are capped; the pages say so when they are cut.
pub const MAX_ROWS: usize = 2000;

pub fn open_ro(path: &Path) -> rusqlite::Result<Connection> {
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX | OpenFlags::SQLITE_OPEN_URI,
    )
}

fn schema_of(con: &Connection) -> String {
    con.query_row("SELECT value FROM meta WHERE key='schema'", [], |r| r.get::<_, String>(0))
        .unwrap_or_else(|_| "1".into())
}

/// Every cocite*.sqlite in bulk_dir, with its schema.
pub fn list_views(bulk: &Path) -> Vec<ViewInfo> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir(bulk) else { return out };
    for e in rd.flatten() {
        let p = e.path();
        let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
        if !(name.starts_with("cocite") && name.ends_with(".sqlite")) {
            continue;
        }
        let stem = name.trim_end_matches(".sqlite").to_string();
        let schema = open_ro(&p).map(|c| schema_of(&c)).unwrap_or_else(|e| format!("unreadable: {e}"));
        out.push(ViewInfo { ok: schema == SCHEMA, stem, path: p, schema });
    }
    out.sort_by(|a, b| a.stem.cmp(&b.stem));
    out
}

// ------------------------------------------------------------------ the main database

/// A case as casedb.py's main database knows it (every case, not just the cited ones).
#[derive(Clone, Debug, Default)]
pub struct Described {
    pub name: String,
    pub cite: String,
    pub year: Option<i64>,
    pub court: String,
    /// CourtListener's count of citing cases (clusters.citation_count)
    pub cited: Option<i64>,
}

pub fn describe(main: &Connection, r: i64) -> Option<Described> {
    let (name, short, date, court, cited): (Option<String>, Option<String>, Option<String>, Option<String>, Option<i64>) = main
        .query_row(
            "SELECT c.case_name, c.case_name_short, c.date_filed, d.court_id, c.citation_count FROM clusters c
             LEFT JOIN dockets d ON d.id = c.docket_id WHERE c.id = ?",
            [r],
            |x| Ok((x.get(0)?, x.get(1)?, x.get(2)?, x.get(3)?, x.get(4)?)),
        )
        .ok()?;
    let cite: Option<(String, String, String)> = main
        .query_row("SELECT volume, reporter, page FROM cites WHERE cluster_id = ? ORDER BY type LIMIT 1", [r], |x| {
            Ok((x.get(0)?, x.get(1)?, x.get(2)?))
        })
        .ok();
    Some(Described {
        name: name.filter(|n| !n.is_empty()).or(short).unwrap_or_else(|| format!("cluster {r}")),
        cite: cite.map(|(v, rep, p)| format!("{v} {rep} {p}")).unwrap_or_default(),
        year: date.and_then(|d| d.get(..4).and_then(|y| y.parse().ok())),
        court: court.unwrap_or_default(),
        cited,
    })
}

/// Docket, dates, judges, every citation and the published headmatter, from the main database.
pub fn details(main: &Connection, r: i64) -> Option<Details> {
    let row: (Option<String>, Option<String>, Option<i64>, Option<String>, Option<String>, Option<String>, Option<Vec<u8>>, Option<String>) = main
        .query_row(
            "SELECT c.date_filed, c.judges, c.citation_count, c.precedential_status, c.syllabus, c.summary, c.headmatter, d.docket_number
             FROM clusters c LEFT JOIN dockets d ON d.id = c.docket_id WHERE c.id = ?",
            [r],
            |x| Ok((x.get(0)?, x.get(1)?, x.get(2)?, x.get(3)?, x.get(4)?, x.get(5)?, x.get(6).ok().flatten(), x.get(7)?)),
        )
        .ok()?;
    let mut citations = Vec::new();
    if let Ok(mut st) = main.prepare("SELECT volume, reporter, page FROM cites WHERE cluster_id = ? ORDER BY type") {
        if let Ok(rows) = st.query_map([r], |x| Ok(format!("{} {} {}", x.get::<_, String>(0)?, x.get::<_, String>(1)?, x.get::<_, String>(2)?))) {
            citations = rows.flatten().collect();
        }
    }
    let headmatter = row.6.map(|b| {
        let mut out = String::new();
        let mut z = flate2::read::ZlibDecoder::new(&b[..]);
        if std::io::Read::read_to_string(&mut z, &mut out).is_err() {
            out = String::from_utf8_lossy(&b).into_owned();
        }
        html_text(&out)
    });
    Some(Details {
        citations,
        docket: row.7.unwrap_or_default(),
        date_filed: row.0.unwrap_or_default(),
        judges: row.1.unwrap_or_default(),
        cl_cited: row.2,
        precedential: row.3.unwrap_or_default(),
        headmatter: headmatter.unwrap_or_default(),
        syllabus: html_text(&row.4.unwrap_or_default()),
        summary: html_text(&row.5.unwrap_or_default()),
    })
}

/// HTML to plain text: block ends become line breaks, tags go, the common entities are decoded,
/// and each line's spaces are collapsed.
pub fn html_text(h: &str) -> String {
    let mut out = String::new();
    let mut tag = String::new();
    let mut in_tag = false;
    for ch in h.chars() {
        match (in_tag, ch) {
            (false, '<') => {
                in_tag = true;
                tag.clear();
            }
            (true, '>') => {
                in_tag = false;
                let t = tag.trim_start_matches('/').split_whitespace().next().unwrap_or("").to_lowercase();
                if matches!(t.as_str(), "br" | "p" | "div" | "parties" | "docketnumber" | "otherdate" | "decisiondate" | "attorneys" | "court" | "judges" | "syllabus" | "summary" | "li")
                {
                    out.push('\n');
                }
            }
            (true, c) => tag.push(c),
            (false, c) => out.push(c),
        }
    }
    let out = out
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
        .replace("&#8212;", "—")
        .replace("&#167;", "§");
    out.lines()
        .map(|l| l.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

/// cases/citedby/<ref>/profile.json, written by case2md.py (a build). Read only if it is there.
pub fn read_profile(cases_dir: &Path, r: i64) -> Option<Profile> {
    let p = cases_dir.join("citedby").join(r.to_string()).join("profile.json");
    let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(p).ok()?).ok()?;
    let mut years: Vec<(i64, i64)> = v["years"]
        .as_object()
        .map(|o| o.iter().filter_map(|(k, n)| Some((k.parse().ok()?, n.as_i64()?))).collect())
        .unwrap_or_default();
    years.sort();
    let pg = &v["parens"];
    let groups = pg["groups"]
        .as_array()
        .map(|a| {
            a.iter()
                .map(|g| ParenGroup {
                    n: g["n"].as_i64().unwrap_or(0),
                    text: g["text"].as_str().unwrap_or("").to_string(),
                    r#ref: g["cluster"].as_i64(),
                    case: g["case"].as_str().unwrap_or("").to_string(),
                    year: g["year"].as_str().map(String::from).or_else(|| g["year"].as_i64().map(|y| y.to_string())).unwrap_or_default(),
                    court: g["court"].as_str().unwrap_or("").to_string(),
                })
                .collect()
        })
        .unwrap_or_default();
    let sc = &v["successor"];
    let successor = sc["name"].as_str().map(|n| {
        (
            sc["slice"].as_str().unwrap_or("").to_string(),
            n.to_string(),
            sc["ref"].as_i64(),
            sc["share"].as_f64().unwrap_or(0.0),
            sc["citing"].as_i64().unwrap_or(0),
        )
    });
    Some(Profile {
        cached: true,
        stamp: v["stamp"].as_str().unwrap_or("").to_string(),
        years,
        parens_total: pg["total"].as_i64().unwrap_or(0),
        groups,
        successor,
    })
}

/// cases/citedby/<ref>/coupling.json, written by `casemap.py coupling`. Read only if it is there.
pub fn read_coupling(cases_dir: &Path, r: i64) -> Option<Coupling> {
    let p = cases_dir.join("citedby").join(r.to_string()).join("coupling.json");
    let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(p).ok()?).ok()?;
    let s = |x: &serde_json::Value| x.as_str().map(String::from).or_else(|| x.as_i64().map(|n| n.to_string())).unwrap_or_default();
    let results = v["results"]
        .as_array()
        .map(|a| {
            a.iter()
                .map(|x| Coupled {
                    r#ref: x["cluster"].as_i64().unwrap_or(0),
                    name: s(&x["name"]),
                    cite: s(&x["cite"]),
                    year: s(&x["year"]),
                    court: s(&x["court"]),
                    score: x["score"].as_f64().unwrap_or(0.0),
                    shared: x["shared"].as_i64().unwrap_or(0),
                    refs: x["refs"]
                        .as_array()
                        .map(|rs| {
                            rs.iter()
                                .map(|y| CoupledRef { r#ref: y["cluster"].as_i64().unwrap_or(0), name: s(&y["name"]), df: y["df"].as_i64().unwrap_or(0) })
                                .collect()
                        })
                        .unwrap_or_default(),
                })
                .collect()
        })
        .unwrap_or_default();
    Some(Coupling {
        computed: s(&v["computed"]),
        stamp: s(&v["stamp"]),
        refs: v["refs"].as_i64().unwrap_or(0),
        skipped: v["skipped"].as_i64().unwrap_or(0),
        max_df: v["max_df"].as_i64().unwrap_or(0),
        n: v["N"].as_i64().unwrap_or(0),
        results,
    })
}

/// "1,2,3" for an IN (...) list of ids (integers only, so nothing to escape).
fn id_list(ids: &[i64]) -> String {
    ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",")
}

/// Cases with exactly this citation ("463 U.S. 29": volume, reporter, page).
pub fn cite_lookup(main: &Connection, volume: &str, reporter: &str, page: &str) -> Vec<i64> {
    let Ok(mut st) = main.prepare("SELECT DISTINCT cluster_id FROM cites WHERE volume = ? AND reporter = ? AND page = ?") else {
        return Vec::new();
    };
    st.query_map(params![volume, reporter, page], |x| x.get(0))
        .map(|rows| rows.flatten().collect())
        .unwrap_or_default()
}

// ------------------------------------------------------------------ labels

#[derive(Clone)]
struct RawLabel {
    name: String,
    description: String,
}

pub fn load_labels(path: &Path) -> HashMap<String, (String, String)> {
    let mut out = HashMap::new();
    let Ok(text) = std::fs::read_to_string(path) else { return out };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) else { return out };
    for lab in v.as_array().into_iter().flatten() {
        let key = lab["key"].as_str().unwrap_or("").to_string();
        if key.is_empty() {
            continue;
        }
        out.insert(
            key,
            (
                lab["name"].as_str().unwrap_or("").to_string(),
                lab["description"].as_str().unwrap_or("").to_string(),
            ),
        );
    }
    out
}

// ------------------------------------------------------------------ the view

pub struct Db {
    con: Connection,
    labels: HashMap<String, RawLabel>,
    lineage_key: HashMap<i64, String>,
    area_key: HashMap<i64, String>,
    key_status: HashMap<String, String>,
    pub meta: ViewMeta,
    cases_dir: PathBuf,
    main: Option<Connection>,
    ops: Option<Connection>,
}

pub fn current_year() -> i64 {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    1970 + secs / 31_556_952
}

impl Db {
    pub fn open(path: &Path, labels_file: &Path, cases_dir: &Path, main_db: &Path, ops_db: &Path) -> Result<Db, String> {
        let con = open_ro(path).map_err(|e| format!("{}: {e}", path.display()))?;
        let schema = schema_of(&con);
        if schema != SCHEMA {
            return Err(format!(
                "{} has results schema {schema}; this app reads schema {SCHEMA}. Rebuild it with ./cocite.py build, or update the app.",
                path.display()
            ));
        }
        let raw = load_labels(labels_file);
        let labels = raw
            .into_iter()
            .map(|(k, (name, description))| (k, RawLabel { name, description }))
            .collect();
        let mut lineage_key = HashMap::new();
        let mut area_key = HashMap::new();
        let mut key_status = HashMap::new();
        // label tables are optional: a view whose labels were never matched still opens
        if let Ok(mut st) = con.prepare("SELECT lineage, key FROM label_map") {
            let rows = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)));
            for (l, k) in rows.into_iter().flatten().flatten() {
                lineage_key.insert(l, k);
            }
        }
        if let Ok(mut st) = con.prepare("SELECT area, key FROM area_label") {
            let rows = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)));
            for (a, k) in rows.into_iter().flatten().flatten() {
                area_key.insert(a, k);
            }
        }
        if let Ok(mut st) = con.prepare("SELECT key, status FROM label_status") {
            let rows = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)));
            for (k, s) in rows.into_iter().flatten().flatten() {
                key_status.insert(k, s);
            }
        }
        let year = current_year();
        let mut slices = Vec::new();
        {
            let mut st = con
                .prepare("SELECT slice, label FROM slices ORDER BY slice")
                .map_err(|e| e.to_string())?;
            let rows = st
                .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
                .map_err(|e| e.to_string())?;
            for (s, label) in rows.flatten() {
                slices.push(SliceInfo { slice: s, label, partial: s + 5 > year });
            }
        }
        let newest_full = slices.iter().filter(|s| !s.partial).map(|s| s.slice).max().unwrap_or(0);
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("").to_string();
        Ok(Db {
            con,
            labels,
            lineage_key,
            area_key,
            key_status,
            meta: ViewMeta { stem, slices, newest_full },
            cases_dir: cases_dir.to_path_buf(),
            // optional: without it, cases outside the view show as "cluster N"
            main: open_ro(main_db).ok().filter(|c| c.query_row("SELECT 1 FROM clusters LIMIT 1", [], |_| Ok(())).is_ok()),
            ops: open_ro(ops_db).ok().filter(|c| c.query_row("SELECT 1 FROM opinions LIMIT 1", [], |_| Ok(())).is_ok()),
        })
    }

    // -------------------------------------------------------------- names

    fn case_name(&self, r: i64) -> (String, String, Option<i64>) {
        self.con
            .query_row("SELECT name, cite, year FROM names WHERE ref=?", [r], |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?.unwrap_or_default(),
                    row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    row.get::<_, Option<i64>>(2)?,
                ))
            })
            .unwrap_or_else(|_| (format!("cluster {r}"), String::new(), None))
    }

    fn short(&self, r: i64) -> String {
        self.case_name(r).0
    }

    fn label_for_key(&self, key: &str) -> Option<Label> {
        self.labels.get(key).map(|l| Label {
            name: l.name.clone(),
            description: l.description.clone(),
            status: self.key_status.get(key).cloned(),
        })
    }

    pub fn lineage_label(&self, lin: i64) -> Option<Label> {
        self.lineage_key.get(&lin).and_then(|k| self.label_for_key(k))
    }

    /// Like cocite.unit_name: the lineage's label, else "around <anchor>".
    fn unit_name(&self, lineage: i64, anchor: i64) -> String {
        match self.lineage_label(lineage) {
            Some(l) => l.name,
            None => format!("around {}", self.short(anchor)),
        }
    }

    fn lineage_name(&self, lin: i64) -> String {
        if let Some(l) = self.lineage_label(lin) {
            return l.name;
        }
        let anchor: Option<i64> = self
            .con
            .query_row("SELECT anchor FROM lineages WHERE lineage=?", [lin], |r| r.get(0))
            .optional()
            .ok()
            .flatten();
        match anchor {
            Some(a) => format!("around {}", self.short(a)),
            None => format!("lineage {lin}"),
        }
    }

    /// Like cocite.area_name.
    fn area_name(&self, area: i64) -> (String, Option<Label>) {
        if let Some(l) = self.area_key.get(&area).and_then(|k| self.label_for_key(k)) {
            return (l.name.clone(), Some(l));
        }
        let anchor: Option<i64> = self
            .con
            .query_row("SELECT anchor_lineage FROM areas WHERE area=?", [area], |r| r.get(0))
            .optional()
            .ok()
            .flatten();
        let around = anchor.map(|a| self.lineage_name(a)).unwrap_or_else(|| "?".into());
        (format!("area {} ({})", area_code(area), around), None)
    }

    fn area_of(&self, lin: i64) -> Option<(i64, String)> {
        let a: Option<i64> = self
            .con
            .query_row("SELECT area FROM lineage_area WHERE lineage=?", [lin], |r| r.get(0))
            .optional()
            .ok()
            .flatten();
        a.map(|a| (a, self.area_name(a).0))
    }

    fn unit(&self, level: Level, slice: i64, k: i64) -> Option<UnitRow> {
        let sql = match level {
            Level::Cluster => "SELECT size, anchor, lineage FROM clusters WHERE slice=? AND cl=?",
            Level::Sub => "SELECT size, anchor, lineage FROM subs WHERE slice=? AND sub=?",
        };
        let r = self
            .con
            .query_row(sql, params![slice, k], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?, r.get::<_, Option<i64>>(2)?.unwrap_or(0)))
            })
            .ok()?;
        Some(UnitRow { level, slice, k, size: r.0, name: self.unit_name(r.2, r.1), lineage: r.2 })
    }

    fn units(&self, sql: &str, level: Level, p: impl rusqlite::Params) -> rusqlite::Result<Vec<UnitRow>> {
        // rows: slice, k, size, anchor, lineage
        let mut st = self.con.prepare(sql)?;
        let raw: Vec<(i64, i64, i64, i64, i64)> = st
            .query_map(p, |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get::<_, Option<i64>>(4)?.unwrap_or(0)))
            })?
            .collect::<rusqlite::Result<_>>()?;
        Ok(raw
            .into_iter()
            .map(|(slice, k, size, anchor, lineage)| UnitRow {
                level,
                slice,
                k,
                size,
                name: self.unit_name(lineage, anchor),
                lineage,
            })
            .collect())
    }

    fn cases(&self, sql: &str, p: impl rusqlite::Params, note: impl Fn(&rusqlite::Row) -> rusqlite::Result<String>) -> rusqlite::Result<Vec<CaseRow>> {
        // rows: ref, weight, then whatever `note` reads; names joined
        let mut st = self.con.prepare(sql)?;
        let rows = st.query_map(p, |r| {
            Ok(CaseRow {
                r#ref: r.get(0)?,
                weight: r.get::<_, Option<f64>>(1)?.unwrap_or(0.0),
                citing: r.get::<_, Option<i64>>("citing").ok().flatten(),
                goodlaw: None,
                dormant: None,
                name: r.get::<_, Option<String>>("name")?.unwrap_or_default(),
                cite: r.get::<_, Option<String>>("cite")?.unwrap_or_default(),
                year: r.get::<_, Option<i64>>("year")?,
                note: note(r)?,
            })
        })?;
        let mut out: Vec<CaseRow> = rows.take(MAX_ROWS).collect::<rusqlite::Result<_>>()?;
        for c in out.iter_mut() {
            if c.name.is_empty() {
                c.name = format!("cluster {}", c.r#ref);
            }
            (c.goodlaw, c.dormant) = read_standing(&self.cases_dir, c.r#ref);
        }
        Ok(out)
    }

    // -------------------------------------------------------------- pages

    pub fn load(&self, page: &Page) -> Result<PageData, String> {
        self.load_inner(page).map_err(|e| e.to_string())
    }

    fn load_inner(&self, page: &Page) -> rusqlite::Result<PageData> {
        Ok(match page {
            Page::Areas => {
                let mut st = self.con.prepare(
                    "SELECT area, lineages, weight, first_slice, last_slice FROM areas ORDER BY weight DESC",
                )?;
                let raw: Vec<(i64, i64, f64, i64, i64)> = st
                    .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?
                    .collect::<rusqlite::Result<_>>()?;
                PageData::Areas(
                    raw.into_iter()
                        .map(|(area, lineages, weight, first, last)| {
                            let (name, lab) = self.area_name(area);
                            AreaRow { area, name, labelled: lab.is_some(), lineages, weight, first, last }
                        })
                        .collect(),
                )
            }
            Page::Area(area) => {
                let (name, label) = self.area_name(*area);
                let (first, last): (i64, i64) = self.con.query_row(
                    "SELECT first_slice, last_slice FROM areas WHERE area=?",
                    [area],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )?;
                let has_conf = self
                    .con
                    .query_row("SELECT 1 FROM sqlite_master WHERE name='lineage_area_conf'", [], |_| Ok(()))
                    .is_ok();
                let sql = if has_conf {
                    "SELECT la.lineage, l.level, l.first_slice, l.last_slice, la.weight, c.confidence
                     FROM lineage_area la JOIN lineages l USING(lineage)
                     LEFT JOIN lineage_area_conf c USING(lineage) WHERE la.area=? ORDER BY la.weight DESC"
                } else {
                    "SELECT la.lineage, l.level, l.first_slice, l.last_slice, la.weight, NULL
                     FROM lineage_area la JOIN lineages l USING(lineage) WHERE la.area=? ORDER BY la.weight DESC"
                };
                let mut st = self.con.prepare(sql)?;
                let raw: Vec<(i64, String, i64, i64, f64, Option<f64>)> = st
                    .query_map([area], |r| {
                        Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?))
                    })?
                    .collect::<rusqlite::Result<_>>()?;
                let lineages = raw
                    .into_iter()
                    .map(|(lineage, level, first, last, weight, confidence)| LineageRow {
                        lineage,
                        level: Level::from_db(&level),
                        labelled: self.lineage_key.contains_key(&lineage),
                        name: self.lineage_name(lineage),
                        first,
                        last,
                        weight,
                        confidence,
                    })
                    .collect();
                let core = self.cases(
                    "SELECT f.ref, f.weight, n.name, n.cite, n.year FROM area_fp f LEFT JOIN names n USING(ref)
                     WHERE f.area=? ORDER BY f.rank",
                    [area],
                    |_| Ok(String::new()),
                )?;
                PageData::Area { area: *area, name, label, first, last, lineages, core, graph: self.area_graph(*area)? }
            }
            Page::Lineage(lin) => {
                let (level, anchor): (String, i64) = self.con.query_row(
                    "SELECT level, anchor FROM lineages WHERE lineage=?",
                    [lin],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )?;
                let level = Level::from_db(&level);
                let sql = match level {
                    Level::Sub => "SELECT slice, sub, size, anchor, lineage FROM subs WHERE lineage=? ORDER BY slice",
                    Level::Cluster => {
                        "SELECT slice, cl, size, anchor, lineage FROM clusters WHERE lineage=? ORDER BY slice"
                    }
                };
                let cells = self.units(sql, level, [lin])?;
                let lv = if level == Level::Sub { "sub" } else { "cluster" };
                let mut merged_in = Vec::new();
                let mut split_out = Vec::new();
                let mut st_in = self.con.prepare(
                    "SELECT slice_from, cl_from, shared, jaccard FROM links WHERE level=? AND slice_to=? AND cl_to=?",
                )?;
                let mut st_out = self.con.prepare(
                    "SELECT slice_to, cl_to, shared, jaccard FROM links WHERE level=? AND slice_from=? AND cl_from=?",
                )?;
                for c in &cells {
                    for (st, dest) in [(&mut st_in, &mut merged_in), (&mut st_out, &mut split_out)] {
                        let rows: Vec<(i64, i64, i64, f64)> = st
                            .query_map(params![lv, c.slice, c.k], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?
                            .collect::<rusqlite::Result<_>>()?;
                        for (s, k, shared, jaccard) in rows {
                            if let Some(other) = self.unit(level, s, k) {
                                if other.lineage != *lin {
                                    dest.push(LinkRow { other, here_slice: c.slice, jaccard, shared });
                                }
                            }
                        }
                    }
                }
                PageData::Lineage {
                    lineage: *lin,
                    level,
                    name: self.lineage_name(*lin),
                    label: self.lineage_label(*lin),
                    anchor: self.short(anchor),
                    area: self.area_of(*lin),
                    cells,
                    merged_in,
                    split_out,
                }
            }
            Page::Slice(s) => {
                let clusters = self.units(
                    "SELECT slice, cl, size, anchor, lineage FROM clusters WHERE slice=? ORDER BY size DESC",
                    Level::Cluster,
                    [s],
                )?;
                let mut subs_of: HashMap<i64, i64> = HashMap::new();
                let mut st = self.con.prepare("SELECT sub/1000, count(*) FROM subs WHERE slice=? AND sub%1000>0 GROUP BY 1")?;
                for r in st.query_map([s], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))? {
                    let (k, n) = r?;
                    subs_of.insert(k, n);
                }
                PageData::Slice {
                    slice: *s,
                    clusters: clusters.into_iter().map(|c| {
                        let n = subs_of.get(&c.k).copied().unwrap_or(0);
                        (c, n)
                    }).collect(),
                }
            }
            Page::Cluster { slice, cl } => {
                let unit = self
                    .unit(Level::Cluster, *slice, *cl)
                    .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
                let head = self.head(unit, None);
                let children = self.units(
                    "SELECT slice, sub, size, anchor, lineage FROM subs WHERE slice=? AND sub/1000=? AND sub%1000>0 ORDER BY size DESC",
                    Level::Sub,
                    params![slice, cl],
                )?;
                let cases = self.cases(
                    "SELECT m.ref, m.weight, n.name, n.cite, n.year, m.home, m.share, x.n AS citing FROM members m
                     LEFT JOIN names n USING(ref) LEFT JOIN nodes x ON x.slice = m.slice AND x.ref = m.ref
                     WHERE m.slice=? AND m.cl=? ORDER BY m.weight DESC",
                    params![slice, cl],
                    |r| {
                        let home: i64 = r.get::<_, Option<i64>>(5)?.unwrap_or(0);
                        let share: f64 = r.get::<_, Option<f64>>(6)?.unwrap_or(0.0);
                        Ok(if home != 0 { format!("home · {:.0}%", share * 100.0) } else { format!("also · {:.0}%", share * 100.0) })
                    },
                )?;
                PageData::Unit { head, children, cases }
            }
            Page::Sub { slice, sub } => {
                let unit = self.unit(Level::Sub, *slice, *sub).ok_or(rusqlite::Error::QueryReturnedNoRows)?;
                let parent = self.unit(Level::Cluster, *slice, sub / 1000);
                let head = self.head(unit, parent);
                let cases = self.cases(
                    "SELECT x.ref, x.subw, n.name, n.cite, n.year, x.n AS citing FROM nodes x LEFT JOIN names n USING(ref)
                     WHERE x.slice=? AND x.sub=? ORDER BY x.subw DESC",
                    params![slice, sub],
                    |_| Ok(String::new()),
                )?;
                PageData::Unit { head, children: Vec::new(), cases }
            }
            Page::Case(r) => PageData::Case(self.case(*r)?),
            Page::Home | Page::Cases | Page::Search { .. } | Page::Scotus | Page::Docket(_) => unreachable!("not a database page"),
        })
    }

    /// Every slice of every sub-cluster lineage in the area, and the `links` rows between them.
    fn area_graph(&self, area: i64) -> rusqlite::Result<GraphData> {
        let mut st = self.con.prepare(
            "SELECT s.lineage, s.slice, s.sub, s.size FROM lineage_area la JOIN subs s ON s.lineage = la.lineage
             WHERE la.area=?",
        )?;
        let cells: Vec<GraphCell> = st
            .query_map([area], |r| Ok(GraphCell { lineage: r.get(0)?, slice: r.get(1)?, k: r.get(2)?, size: r.get(3)? }))?
            .collect::<rusqlite::Result<_>>()?;
        let here: std::collections::HashSet<(i64, i64)> = cells.iter().map(|c| (c.slice, c.k)).collect();
        let mut st = self.con.prepare(
            "SELECT slice_from, cl_from, slice_to, cl_to, jaccard FROM links WHERE level='sub'",
        )?;
        let mut edges = Vec::new();
        let mut outside = Vec::new();
        for r in st.query_map([], |r| {
            Ok(GraphEdge { from: (r.get(0)?, r.get(1)?), to: (r.get(2)?, r.get(3)?), jaccard: r.get(4)? })
        })? {
            let e = r?;
            match (here.contains(&e.from), here.contains(&e.to)) {
                (true, true) => edges.push(e),
                (false, true) | (true, false) => {
                    let incoming = here.contains(&e.to);
                    let (h, o) = if incoming { (e.to, e.from) } else { (e.from, e.to) };
                    if let Some(u) = self.unit(Level::Sub, o.0, o.1) {
                        outside.push(OutEdge {
                            here: h,
                            incoming,
                            other_slice: o.0,
                            other_lineage: u.lineage,
                            other_area: self.area_of(u.lineage).map(|a| a.1),
                            other_name: u.name,
                            jaccard: e.jaccard,
                        });
                    }
                }
                _ => {}
            }
        }
        Ok(GraphData { cells, edges, outside })
    }

    /// What can be read in well under a second without a build: CourtListener's parentheticals
    /// about the case's opinions, in its similarity groups (as case2md.parens_data does).
    fn live_profile(&self, r: i64) -> Option<Profile> {
        let (main, ops) = (self.main.as_ref()?, self.ops.as_ref()?);
        let ids: Vec<i64> = ops
            .prepare("SELECT id FROM opinions WHERE cluster_id = ?")
            .ok()?
            .query_map([r], |x| x.get(0))
            .ok()?
            .flatten()
            .collect();
        if ids.is_empty() {
            return None;
        }
        let list = ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(",");
        let total: i64 = main
            .query_row(&format!("SELECT count(*) FROM parens WHERE described IN ({list})"), [], |x| x.get(0))
            .ok()?;
        let mut groups = Vec::new();
        let mut st = main
            .prepare(&format!(
                "SELECT group_id, count(*) AS n FROM parens WHERE described IN ({list}) AND group_id IS NOT NULL \
                 GROUP BY group_id ORDER BY n DESC LIMIT 12"
            ))
            .ok()?;
        let gs: Vec<(i64, i64)> = st.query_map([], |x| Ok((x.get(0)?, x.get(1)?))).ok()?.flatten().collect();
        for (gid, n) in gs {
            let best: Option<(String, i64)> = main
                .query_row(
                    &format!("SELECT text, describing FROM parens WHERE described IN ({list}) AND group_id = ? ORDER BY score DESC LIMIT 1"),
                    [gid],
                    |x| Ok((x.get::<_, Option<String>>(0)?.unwrap_or_default(), x.get::<_, Option<i64>>(1)?.unwrap_or(0))),
                )
                .ok();
            let Some((text, describing)) = best else { continue };
            let by: Option<i64> = ops.query_row("SELECT cluster_id FROM opinions WHERE id = ?", [describing], |x| x.get(0)).ok();
            let d = by.and_then(|c| describe(main, c)).unwrap_or_default();
            groups.push(ParenGroup {
                n,
                text: text.split_whitespace().collect::<Vec<_>>().join(" "),
                r#ref: by,
                case: d.name,
                year: d.year.map(|y| y.to_string()).unwrap_or_default(),
                court: d.court,
            });
        }
        Some(Profile { cached: false, parens_total: total, groups, ..Default::default() })
    }

    /// What it draws on, live: the case's opinions (opinions database) -> edges_out (main database)
    /// -> the cited opinions' cases, then each cited case's sub-cluster in the newest full slice.
    /// The same definition as cocite.draws_on, which the notes and PDFs use. A handful of indexed
    /// queries; None without the main and opinions databases or when the case cites nothing.
    fn draws_on(&self, r: i64) -> Option<DrawsOn> {
        let (main, ops) = (self.main.as_ref()?, self.ops.as_ref()?);
        let own: Vec<i64> = ops.prepare("SELECT id FROM opinions WHERE cluster_id = ?").ok()?.query_map([r], |x| x.get(0)).ok()?.flatten().collect();
        if own.is_empty() {
            return None;
        }
        let cited_ops: Vec<i64> = main
            .prepare(&format!("SELECT DISTINCT cited FROM edges WHERE citing IN ({})", id_list(&own)))
            .ok()?
            .query_map([], |x| x.get::<_, Option<i64>>(0))
            .ok()?
            .flatten()
            .flatten()
            .collect();
        let mut refs = std::collections::BTreeSet::new();
        for ch in cited_ops.chunks(500) {
            let mut st = ops.prepare(&format!("SELECT DISTINCT cluster_id FROM opinions WHERE id IN ({})", id_list(ch))).ok()?;
            refs.extend(st.query_map([], |x| x.get::<_, Option<i64>>(0)).ok()?.flatten().flatten());
        }
        refs.remove(&r);
        if refs.is_empty() {
            return None;
        }
        let refs: Vec<i64> = refs.into_iter().collect();
        let s = self.meta.newest_full;
        let mut count: HashMap<i64, i64> = HashMap::new();
        for ch in refs.chunks(500) {
            let mut st = self.con.prepare(&format!("SELECT sub FROM nodes WHERE slice = ? AND ref IN ({})", id_list(ch))).ok()?;
            for k in st.query_map([s], |x| x.get::<_, Option<i64>>(0)).ok()?.flatten().flatten() {
                if k > 0 {
                    *count.entry(k).or_default() += 1;
                }
            }
        }
        let clustered: i64 = count.values().sum();
        let mut counted: Vec<(i64, i64)> = count.into_iter().collect();
        counted.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
        let mut subs = Vec::new();
        let mut areas: Vec<(i64, String, i64)> = Vec::new();
        for (k, n) in counted {
            let Some(unit) = self.unit(Level::Sub, s, k) else { continue };
            let area = self.area_of(unit.lineage);
            if let Some((a, an)) = &area {
                match areas.iter_mut().find(|x| x.0 == *a) {
                    Some(x) => x.2 += n,
                    None => areas.push((*a, an.clone(), n)),
                }
            }
            subs.push(DrawsSub { unit, n, area });
        }
        areas.sort_by(|a, b| b.2.cmp(&a.2));
        Some(DrawsOn { slice: s, refs: refs.len() as i64, unclustered: refs.len() as i64 - clustered, subs, areas })
    }

    /// The bulk data's year: casedb's citation-map file name in the main database, else this year.
    fn data_year(&self) -> i64 {
        self.main
            .as_ref()
            .and_then(|m| m.query_row("SELECT file FROM loaded WHERE name='citation-map'", [], |x| x.get::<_, String>(0)).ok())
            .and_then(|f| {
                // the first "YYYY-MM-DD" in the file name
                let w = f.as_bytes().windows(10).find(|w| w.iter().enumerate().all(|(k, c)| if k == 4 || k == 7 { *c == b'-' } else { c.is_ascii_digit() }))?;
                stamp_year(std::str::from_utf8(w).ok()?)
            })
            .unwrap_or_else(current_year)
    }

    /// Dormant or not, read live for a case with no cached profile: the case's opinions -> `edges`
    /// (citing opinions) -> their cases -> the newest date_filed. Only for cases CourtListener says
    /// are cited at most LIVE_DORMANT_MAX times, so it stays fast. None when nothing dated is found.
    fn live_dormancy(&self, r: i64) -> Option<Dormancy> {
        let (main, ops) = (self.main.as_ref()?, self.ops.as_ref()?);
        let own: Vec<i64> = ops.prepare("SELECT id FROM opinions WHERE cluster_id = ?").ok()?.query_map([r], |x| x.get(0)).ok()?.flatten().collect();
        if own.is_empty() {
            return None;
        }
        let citing: Vec<i64> = main
            .prepare(&format!("SELECT DISTINCT citing FROM edges WHERE cited IN ({})", id_list(&own)))
            .ok()?
            .query_map([], |x| x.get::<_, Option<i64>>(0))
            .ok()?
            .flatten()
            .flatten()
            .collect();
        let mut clusters = std::collections::BTreeSet::new();
        for ch in citing.chunks(500) {
            let mut st = ops.prepare(&format!("SELECT DISTINCT cluster_id FROM opinions WHERE id IN ({})", id_list(ch))).ok()?;
            clusters.extend(st.query_map([], |x| x.get::<_, Option<i64>>(0)).ok()?.flatten().flatten());
        }
        clusters.remove(&r);
        let clusters: Vec<i64> = clusters.into_iter().collect();
        let mut last: Option<i64> = None;
        for ch in clusters.chunks(500) {
            let d: Option<String> = main
                .query_row(&format!("SELECT max(date_filed) FROM clusters WHERE id IN ({})", id_list(ch)), [], |x| x.get(0))
                .ok()
                .flatten();
            last = last.max(d.as_deref().and_then(stamp_year));
        }
        let last = last?;
        Some(Dormancy::from_years(
            &[(last, 1)],
            self.data_year(),
            &format!("checked just now: {} citing decisions in the local citation data, newest filed {last}", clusters.len()),
        ))
    }

    fn head(&self, unit: UnitRow, parent: Option<UnitRow>) -> UnitHead {
        let lin = unit.lineage;
        let span: Option<(i64, i64)> = self
            .con
            .query_row("SELECT first_slice, last_slice FROM lineages WHERE lineage=?", [lin], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .ok();
        UnitHead {
            label: self.lineage_label(lin),
            lineage_first: span.map(|s| s.0),
            lineage_last: span.map(|s| s.1),
            area: if unit.level == Level::Sub { self.area_of(lin) } else { None },
            parent,
            unit,
        }
    }

    fn case(&self, r: i64) -> rusqlite::Result<CaseData> {
        let (mut name, mut cite, mut year) = self.case_name(r);
        let main = self.main.as_ref().and_then(|m| describe(m, r));
        if let Some(d) = &main {
            if name.starts_with("cluster ") {
                name = d.name.clone();
            }
            if cite.is_empty() {
                cite = d.cite.clone();
            }
            year = year.or(d.year);
        }
        let court = main.as_ref().map(|d| d.court.clone()).unwrap_or_default();
        // nodes: one row per slice the case is a node in
        let mut st = self
            .con
            .prepare("SELECT slice, n, cl, sub, subw FROM nodes WHERE ref=? ORDER BY slice DESC")?;
        let nodes: Vec<(i64, i64, Option<i64>, Option<i64>, Option<f64>)> = st
            .query_map([r], |x| Ok((x.get(0)?, x.get(1)?, x.get(2)?, x.get(3)?, x.get(4)?)))?
            .collect::<rusqlite::Result<_>>()?;
        let mut st = self.con.prepare("SELECT slice, cl, home, share FROM members WHERE ref=?")?;
        let mut mem: HashMap<i64, Vec<(i64, bool, f64)>> = HashMap::new();
        for x in st.query_map([r], |x| {
            Ok((x.get::<_, i64>(0)?, x.get::<_, i64>(1)?, x.get::<_, Option<i64>>(2)?.unwrap_or(0) != 0, x.get::<_, Option<f64>>(3)?.unwrap_or(0.0)))
        })? {
            let (s, cl, home, share) = x?;
            mem.entry(s).or_default().push((cl, home, share));
        }
        let mut slices = Vec::new();
        for (s, n, cl, sub, subw) in nodes {
            let home = cl.and_then(|cl| self.unit(Level::Cluster, s, cl));
            let sub_u = sub.filter(|&k| k > 0).and_then(|k| self.unit(Level::Sub, s, k));
            let ms = mem.remove(&s).unwrap_or_default();
            let home_share = ms.iter().find(|m| m.1).map(|m| m.2);
            let mut also: Vec<(UnitRow, f64)> = ms
                .iter()
                .filter(|m| !m.1)
                .filter_map(|m| self.unit(Level::Cluster, s, m.0).map(|u| (u, m.2)))
                .collect();
            also.sort_by(|a, b| b.1.total_cmp(&a.1));
            slices.push(CaseSlice { slice: s, n, home, sub: sub_u, sub_weight: subw.unwrap_or(0.0), also, home_share });
        }
        // its newest sub-cluster lineage, and that lineage's area
        // (newest full slice first: a partial slice's lineages are still forming)
        let full = self.meta.newest_full;
        let lineage = slices
            .iter()
            .filter(|c| c.slice <= full)
            .chain(slices.iter().filter(|c| c.slice > full))
            .find_map(|c| c.sub.as_ref().map(|u| u.lineage).filter(|&l| l != 0));
        let lineage_named = lineage.map(|l| (l, self.lineage_name(l)));
        let area = lineage.and_then(|l| self.area_of(l));
        // partners: newest full slice it is a node in, else its newest slice
        let partner_slice = slices
            .iter()
            .map(|c| c.slice)
            .find(|&s| s <= self.meta.newest_full)
            .or_else(|| slices.first().map(|c| c.slice));
        let mut partners = Vec::new();
        if let Some(ps) = partner_slice {
            let mut st = self.con.prepare(
                "SELECT p.partner, p.support, p.lift, p.cosine, n.name, n.cite, n.year FROM partners p
                 LEFT JOIN names n ON n.ref = p.partner WHERE p.ref=? AND p.slice=? ORDER BY p.cosine DESC",
            )?;
            for x in st.query_map(params![r, ps], |x| {
                Ok(Partner {
                    r#ref: x.get(0)?,
                    support: x.get::<_, Option<f64>>(1)?.unwrap_or(0.0),
                    lift: x.get::<_, Option<f64>>(2)?.unwrap_or(0.0),
                    cosine: x.get::<_, Option<f64>>(3)?.unwrap_or(0.0),
                    name: x.get::<_, Option<String>>(4)?.unwrap_or_default(),
                    cite: x.get::<_, Option<String>>(5)?.unwrap_or_default(),
                    year: x.get(6)?,
                })
            })? {
                partners.push(x?);
            }
        }
        let (goodlaw, mut dormant) = read_standing(&self.cases_dir, r);
        if dormant.is_none() {
            dormant = match main.and_then(|d| d.cited) {
                Some(0) => Some(Dormancy {
                    dormant: true,
                    last: None,
                    from: self.data_year() - RECENT,
                    source: "CourtListener's citing count is 0".into(),
                }),
                Some(n) if n <= LIVE_DORMANT_MAX => self.live_dormancy(r),
                _ => None,
            };
        }
        Ok(CaseData {
            r#ref: r,
            name,
            cite,
            year,
            court,
            slices,
            lineage: lineage_named,
            area,
            partner_slice,
            partners,
            goodlaw,
            dormant,
            details: self.main.as_ref().and_then(|m| details(m, r)),
            profile: read_profile(&self.cases_dir, r).or_else(|| self.live_profile(r)),
            draws: self.draws_on(r),
            coupling: read_coupling(&self.cases_dir, r),
        })
    }
}

/// The live dormancy check runs only for cases cited at most this often (CourtListener's count).
pub const LIVE_DORMANT_MAX: i64 = 300;

/// A year written as "2006" or 2006.
fn year_of(v: &serde_json::Value) -> Option<i64> {
    v.as_i64().or_else(|| v.as_str().and_then(|s| s.trim().parse().ok()))
}

/// cases/citedby/<ref>/profile.json, written by case2md.py: the good-law result (when casemap.py
/// goodlaw has run) and, from the citations per year, whether the case is dormant. Read only if it is there.
pub fn read_standing(cases_dir: &Path, r: i64) -> (Option<GoodLaw>, Option<Dormancy>) {
    let p = cases_dir.join("citedby").join(r.to_string()).join("profile.json");
    let Some(v) = std::fs::read_to_string(p).ok().and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok()) else {
        return (None, None);
    };
    let stamp = v["stamp"].as_str().map(String::from);
    let dormant = v["years"].as_object().map(|o| {
        let years: Vec<(i64, i64)> = o.iter().filter_map(|(k, n)| Some((k.parse().ok()?, n.as_i64()?))).collect();
        let year = stamp.as_deref().and_then(stamp_year).unwrap_or_else(current_year);
        Dormancy::from_years(&years, year, "citations per year in the cached case profile")
    });
    let g = &v["goodlaw"];
    let goodlaw = g["status"].as_str().map(|status| GoodLaw {
        status: status.to_string(),
        yellow_year: year_of(&g["yellow_year"]),
        red_year: year_of(&g["red_year"]),
        read: g["read"].as_i64(),
        of: g["of"].as_i64(),
        stamp: stamp.clone(),
        level: g["level"].as_str().unwrap_or("").to_string(),
        version: g["version"].as_i64(),
        evidence: g["evidence"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|e| Evidence {
                        cluster: e["cluster"].as_i64().unwrap_or(0),
                        name: e["name"].as_str().unwrap_or("").to_string(),
                        court: e["court"].as_str().unwrap_or("").to_string(),
                        year: year_of(&e["year"]).map(|y| y.to_string()).unwrap_or_default(),
                        above: e["above"].as_bool().unwrap_or(false),
                        kind: e["kind"].as_str().unwrap_or("").to_string(),
                        word: e["word"].as_str().unwrap_or("").to_string(),
                        snippet: e["snippet"].as_str().unwrap_or("").to_string(),
                    })
                    .collect()
            })
            .unwrap_or_default(),
        n_evidence: g["n_evidence"].as_i64().unwrap_or(0),
    });
    (goodlaw, dormant)
}

// ------------------------------------------------------------------ worker thread

pub enum Req {
    Open(PathBuf),
    Load(Page),
}

pub enum Resp {
    Opened(Result<ViewMeta, String>),
    Loaded(Page, Result<PageData, String>, std::time::Duration),
}

/// Spawn the worker. It owns the connection; `wake` repaints the UI when a result is ready.
pub fn spawn(labels_file: PathBuf, cases_dir: PathBuf, main_db: PathBuf, ops_db: PathBuf, wake: impl Fn() + Send + 'static) -> (Sender<Req>, Receiver<Resp>) {
    let (tx_req, rx_req) = channel::<Req>();
    let (tx_resp, rx_resp) = channel::<Resp>();
    std::thread::spawn(move || {
        let mut db: Option<Db> = None;
        // test aid: LAWGUI_DELAY_MS=800 makes every page load slow, to see the placeholders
        let delay = std::env::var("LAWGUI_DELAY_MS").ok().and_then(|v| v.parse::<u64>().ok());
        while let Ok(req) = rx_req.recv() {
            let resp = match req {
                Req::Open(path) => match Db::open(&path, &labels_file, &cases_dir, &main_db, &ops_db) {
                    Ok(d) => {
                        let m = d.meta.clone();
                        db = Some(d);
                        Resp::Opened(Ok(m))
                    }
                    Err(e) => {
                        db = None;
                        Resp::Opened(Err(e))
                    }
                },
                Req::Load(page) => {
                    let t = std::time::Instant::now();
                    if let Some(ms) = delay {
                        std::thread::sleep(std::time::Duration::from_millis(ms));
                    }
                    let r = match &db {
                        Some(d) => d.load(&page),
                        None => Err("no view is open".into()),
                    };
                    Resp::Loaded(page, r, t.elapsed())
                }
            };
            if tx_resp.send(resp).is_err() {
                break;
            }
            wake();
        }
    });
    (tx_req, rx_resp)
}
