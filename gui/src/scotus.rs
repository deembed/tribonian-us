//! Supreme Court dockets the repo's `scotus.py` has fetched, and its `recent` summary.
//!
//! - `cases/scotus/<docket>.json`: `tribonian scotus fetch` (the Court's docket feed, Proceedings and
//!   Orders, with each linked PDF's local copy).
//! - `cases/scotus/_recent.json`: `scotus.py recent` (opinions, Granted & Noted lists and order
//!   lists of the last year, from supremecourt.gov).
//! - `md-cases/Pending SCOTUS/`: `tribonian scotus build` (one docket note, one note per filing; their
//!   front matter carries `docket` and the filing's `source` URL, which is how a filing's note is found).
//!
//! Small JSON files: read on the UI thread at start and after every queued job. Read-only.

use serde_json::Value;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Default)]
pub struct Link {
    pub desc: String,
    pub url: String,
    /// local copy, relative to the repo (fetch writes it)
    pub pdf: String,
}

#[derive(Clone, Debug, Default)]
pub struct Entry {
    pub date: String,
    pub text: String,
    /// classify(): petition, opposition, reply, brief, amicus, joint-appendix, supplemental, other
    pub kind: String,
    pub side: String,
    pub filer: String,
    pub stage: String,
    pub links: Vec<Link>,
}

impl Docket {
    /// The case below as (CourtListener court id, case number) when it is in a federal court of appeals (RECAP).
    pub fn below_ref(&self) -> Option<(String, String)> {
        let num = self.lower_numbers.trim_matches(|c| c == '(' || c == ')' || c == ' ').split(',').next()?.trim().to_string();
        let lc = self.lower_court.to_lowercase();
        let (_, rest) = lc.split_once("for the ")?;
        let name = rest.strip_suffix(" circuit").or_else(|| rest.split(" circuit").next())?;
        let id = match name {
            "first" => "ca1", "second" => "ca2", "third" => "ca3", "fourth" => "ca4", "fifth" => "ca5", "sixth" => "ca6",
            "seventh" => "ca7", "eighth" => "ca8", "ninth" => "ca9", "tenth" => "ca10", "eleventh" => "ca11",
            "district of columbia" => "cadc", "federal" => "cafc",
            _ => return None,
        };
        (!num.is_empty()).then(|| (id.to_string(), num))
    }
}

#[derive(Clone, Debug, Default)]
pub struct Docket {
    /// "25-579"
    pub number: String,
    pub petitioner: String,
    pub respondent: String,
    pub docketed: String,
    pub lower_court: String,
    pub lower_numbers: String,
    pub lower_decision: String,
    pub granted: String,
    pub argued: String,
    pub retrieved: String,
    pub qp_url: String,
    /// the Clerk's question presented, as text: the fetched PDF's text, if fetch kept it
    pub qp_text: String,
    pub below_cluster: Option<i64>,
    pub entries: Vec<Entry>,
}

/// "Department of the Air Force, et al., Petitioners" -> "Department of the Air Force"
/// (scotus.py party(): the caption's first party, without "et al." and the role).
pub fn party(t: &str) -> String {
    let t = t.trim();
    let t = t.split(", Petitioner").next().unwrap_or(t);
    let t = t.split(", Respondent").next().unwrap_or(t);
    let t = t.split(", fka ").next().unwrap_or(t);
    t.trim_end_matches(", et al.").trim_end_matches(" et al.").trim().to_string()
}

impl Docket {
    pub fn title(&self) -> String {
        format!("{} v. {}", party(&self.petitioner), party(&self.respondent))
    }
    pub fn last_date(&self) -> &str {
        self.entries.iter().rev().map(|e| e.date.as_str()).find(|d| !d.is_empty()).unwrap_or("")
    }
    /// Briefs and petitions with a document (the filings `tribonian scotus build` makes notes of).
    pub fn filings(&self) -> impl Iterator<Item = &Entry> {
        self.entries.iter().filter(|e| e.kind != "other" && e.kind != "joint-appendix" && e.links.iter().any(|l| !l.url.is_empty()))
    }
    /// The main document of an entry: the first link that is not a proof of service or certificate.
    pub fn main_link(e: &Entry) -> Option<&Link> {
        e.links.iter().find(|l| !l.url.is_empty() && !skip_link(&l.desc))
    }
    /// The Court's public docket page.
    pub fn web_url(&self) -> String {
        format!("https://www.supremecourt.gov/docket/docketfiles/html/public/{}.html", self.number)
    }
    pub fn status(&self) -> String {
        status(&self.entries, &self.granted, &self.argued)
    }
}

/// scotus.py SKIP_LINK: proofs of service, certificates and affidavits are not filings.
pub fn skip_link(desc: &str) -> bool {
    let d = desc.to_lowercase();
    d.contains("proof of service") || d.contains("certificate") || d.contains("affidavit")
}

/// Where the case stands, read from the docket's own words, newest decisive entry first.
/// Plain substring tests on the Clerk's standard phrases; "" dates are shown as unknown.
pub fn status(entries: &[Entry], granted: &str, argued: &str) -> String {
    // (lowercased text, the Clerk's own text, date), searched newest first
    let rows: Vec<(String, &str, &str)> = entries.iter().map(|e| (e.text.to_lowercase(), e.text.as_str(), e.date.as_str())).collect();
    let find = |pred: &dyn Fn(&str) -> bool| rows.iter().rev().find(|(t, _, _)| pred(t)).map(|(_, o, d)| (o.to_string(), d.to_string()));
    let tail = |o: &str, n: usize| o.get(n..).unwrap_or("").trim().trim_end_matches('.').to_string();
    if let Some((_, d)) = find(&|t| t.starts_with("judgment issued")) {
        return format!("decided; judgment issued {d}");
    }
    if let Some((_, d)) = find(&|t| {
        t.starts_with("judgment affirmed") || t.starts_with("judgment reversed") || t.starts_with("judgment vacated")
            || t.contains("delivered the opinion") || t.starts_with("adjudged to be")
    }) {
        return format!("decided {d}");
    }
    if let Some((_, d)) = find(&|t| t.starts_with("petition denied") || t.starts_with("petition dismissed")) {
        return format!("petition denied {d}");
    }
    if let Some((_, d)) = find(&|t| t.starts_with("argued")) {
        return format!("argued {d}; awaiting decision");
    }
    if !argued.is_empty() {
        return format!("argued {argued}; awaiting decision");
    }
    let granted = if granted.is_empty() { find(&|t| t.contains("petition granted")).map(|x| x.1).unwrap_or_default() } else { granted.to_string() };
    if let Some((o, _)) = find(&|t| t.starts_with("set for argument")) {
        let when = tail(&o, "set for argument".len());
        let when = when.strip_prefix("on ").unwrap_or(&when);
        return format!("granted {}; set for argument {when}", if granted.is_empty() { "?" } else { &granted });
    }
    if !granted.is_empty() {
        return format!("granted {granted}; awaiting argument");
    }
    if let Some((o, _)) = find(&|t| t.starts_with("distributed for conference")) {
        return format!("petition pending; distributed for conference {}", tail(&o, "distributed for conference".len()));
    }
    "petition pending".into()
}

/// "25-579", "No. 25-579", "25-00579" -> "25-579"; an application "25A374", "25a00374" -> "25A374".
/// Original cases ("22O145") are refused: tribonian scotus fetch doesn't read them.
pub fn norm_docket(s: &str) -> Option<String> {
    let s = s.trim();
    let s = s.strip_prefix("No.").or_else(|| s.strip_prefix("No")).or_else(|| s.strip_prefix("no.")).unwrap_or(s).trim();
    // an application: "26A476" (its own docket; "26-476" is a different case)
    if let Some((yy, n)) = s.split_once(['A', 'a']) {
        if yy.len() == 2 && yy.bytes().all(|b| b.is_ascii_digit()) && !n.is_empty() && n.len() <= 5 && n.bytes().all(|b| b.is_ascii_digit()) {
            let n: u32 = n.parse().ok()?;
            return (n > 0).then(|| format!("{yy}A{n}"));
        }
        return None;
    }
    let (yy, n) = s.split_once('-')?;
    if yy.len() != 2 || !yy.bytes().all(|b| b.is_ascii_digit()) || n.is_empty() || n.len() > 5 || !n.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let n: u32 = n.parse().ok()?;
    if n == 0 {
        return None;
    }
    Some(format!("{yy}-{n}"))
}

/// A queue key for a docket, apart from every CourtListener cluster id (those are positive).
pub fn docket_key(num: &str) -> i64 {
    let (base, (yy, n)) = match num.split_once('A') {
        Some(p) => (1_500_000_000, p),
        None => (1_000_000_000, num.split_once('-').unwrap_or(("0", "0"))),
    };
    -(base + yy.parse::<i64>().unwrap_or(0) * 100_000 + n.parse::<i64>().unwrap_or(0))
}
/// The queue key of `scotus.py recent`.
pub const RECENT_KEY: i64 = -1;

fn s(v: &Value, k: &str) -> String {
    match &v[k] {
        Value::String(x) => x.trim().to_string(),
        Value::Null => String::new(),
        x => x.to_string(),
    }
}

pub fn read_docket(path: &Path, repo: &Path) -> Result<Docket, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let v: Value = serde_json::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))?;
    let f = &v["feed"];
    let mut d = Docket {
        number: s(&v, "number"),
        petitioner: s(f, "PetitionerTitle"),
        respondent: s(f, "RespondentTitle"),
        docketed: s(f, "DocketedDate"),
        lower_court: s(f, "LowerCourt"),
        lower_numbers: s(f, "LowerCourtCaseNumbers").trim_matches(|c| c == '(' || c == ')').to_string(),
        lower_decision: s(f, "LowerCourtDecision"),
        granted: s(&v, "granted"),
        argued: s(&v, "argued"),
        retrieved: s(&v, "retrieved"),
        qp_url: s(&v["qp"], "url"),
        below_cluster: v["below_cluster"].as_i64(),
        ..Default::default()
    };
    if d.number.is_empty() {
        d.number = path.file_stem().and_then(|x| x.to_str()).unwrap_or("").to_string();
    }
    let qp_txt = s(&v["qp"], "txt");
    if !qp_txt.is_empty() {
        if let Ok(t) = std::fs::read_to_string(repo.join(&qp_txt)) {
            d.qp_text = t.split_whitespace().collect::<Vec<_>>().join(" ");
        }
    }
    for e in v["entries"].as_array().into_iter().flatten() {
        d.entries.push(Entry {
            date: s(e, "date"),
            text: s(e, "text"),
            kind: s(e, "kind"),
            side: s(e, "side"),
            filer: s(e, "filer"),
            stage: s(e, "stage"),
            links: e["links"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|l| Link { desc: s(l, "desc"), url: s(l, "url"), pdf: s(l, "pdf") })
                .collect(),
        });
    }
    Ok(d)
}

/// Every cases/scotus/<docket>.json (not the _-prefixed ones), newest activity first; plus errors.
pub fn load_dockets(cases_dir: &Path, repo: &Path) -> (Vec<Docket>, Vec<String>) {
    let (mut out, mut errs) = (Vec::new(), Vec::new());
    let dir = cases_dir.join("scotus");
    let Ok(rd) = std::fs::read_dir(&dir) else { return (out, errs) };
    let mut paths: Vec<PathBuf> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|x| x.to_str()) == Some("json"))
        .filter(|p| !p.file_name().and_then(|x| x.to_str()).unwrap_or("_").starts_with('_'))
        .collect();
    paths.sort();
    for p in paths {
        match read_docket(&p, repo) {
            Ok(d) => out.push(d),
            Err(e) => errs.push(e),
        }
    }
    out.sort_by(|a, b| b.last_date().cmp(a.last_date()).then(a.number.cmp(&b.number)));
    (out, errs)
}

// ---------------------------------------------------------------- _recent.json

#[derive(Clone, Debug, Default)]
pub struct Decided {
    pub date: String,
    pub docket: String,
    pub name: String,
    pub pdf: String,
    pub justice: String,
    pub citation: String,
    pub term: String,
}

#[derive(Clone, Debug, Default)]
pub struct Granted {
    pub docket: String,
    pub name: String,
    pub court: String,
    pub granted: String,
    pub argued: String,
    pub decided: String,
    pub term: String,
}

#[derive(Clone, Debug, Default)]
pub struct Order {
    pub date: String,
    pub docket: String,
    pub name: String,
    pub section: String,
    /// granted, gvr, denied, rehearing denied, pending, discipline, order
    pub kind: String,
    pub text: String,
    pub url: String,
    pub list: String,
}

#[derive(Clone, Debug, Default)]
pub struct Recent {
    pub retrieved: String,
    pub since: String,
    pub terms: Vec<String>,
    pub sources: Vec<String>,
    pub errors: Vec<String>,
    pub decided: Vec<Decided>,
    pub granted: Vec<Granted>,
    pub orders: Vec<Order>,
}

/// cases/scotus/_recent.json; Ok(None) when `scotus.py recent` has never run.
pub fn load_recent(cases_dir: &Path) -> Result<Option<Recent>, String> {
    let p = cases_dir.join("scotus").join("_recent.json");
    let text = match std::fs::read_to_string(&p) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("{}: {e}", p.display())),
    };
    let v: Value = serde_json::from_str(&text).map_err(|e| format!("{}: {e}", p.display()))?;
    let strs = |k: &str| v[k].as_array().into_iter().flatten().filter_map(|x| x.as_str().map(String::from)).collect::<Vec<_>>();
    let arr = |k: &str| v[k].as_array().cloned().unwrap_or_default();
    Ok(Some(Recent {
        retrieved: s(&v, "retrieved"),
        since: s(&v, "since"),
        terms: strs("terms"),
        sources: strs("sources"),
        errors: strs("errors"),
        decided: arr("decided")
            .iter()
            .map(|x| Decided {
                date: s(x, "date"),
                docket: s(x, "docket"),
                name: s(x, "name"),
                pdf: s(x, "pdf"),
                justice: s(x, "justice"),
                citation: s(x, "citation"),
                term: s(x, "term"),
            })
            .collect(),
        granted: arr("granted")
            .iter()
            .map(|x| Granted {
                docket: s(x, "docket"),
                name: s(x, "name"),
                court: s(x, "court"),
                granted: s(x, "granted"),
                argued: s(x, "argued"),
                decided: s(x, "decided"),
                term: s(x, "term"),
            })
            .collect(),
        orders: arr("orders")
            .iter()
            .map(|x| Order {
                date: s(x, "date"),
                docket: s(x, "docket"),
                name: s(x, "name"),
                section: s(x, "section"),
                kind: s(x, "kind"),
                text: s(x, "text"),
                url: s(x, "url"),
                list: s(x, "list"),
            })
            .collect(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn e(date: &str, text: &str) -> Entry {
        Entry { date: date.into(), text: text.into(), ..Default::default() }
    }

    #[test]
    fn docket_numbers() {
        assert_eq!(norm_docket("25-579").as_deref(), Some("25-579"));
        assert_eq!(norm_docket(" No. 25-579 ").as_deref(), Some("25-579"));
        assert_eq!(norm_docket("25-00579").as_deref(), Some("25-579"));
        assert_eq!(norm_docket("25A374").as_deref(), Some("25A374"));
        assert_eq!(norm_docket("26a00476").as_deref(), Some("26A476"));
        assert_ne!(docket_key("26A476"), docket_key("26-476"));
        assert_eq!(norm_docket("463 U.S. 29"), None);
        assert_eq!(norm_docket("chevron"), None);
        assert!(docket_key("25-579") < 0 && docket_key("25-579") != docket_key("25-580"));
    }

    #[test]
    fn statuses() {
        let set = [e("2026-03-09", "Petition GRANTED."), e("2026-08-04", "SET FOR ARGUMENT on Wednesday, October 7, 2026.")];
        assert_eq!(status(&set, "2026-03-09", ""), "granted 2026-03-09; set for argument Wednesday, October 7, 2026");
        assert_eq!(status(&set[..1], "2026-03-09", ""), "granted 2026-03-09; awaiting argument");
        assert_eq!(status(&[e("2026-01-09", "DISTRIBUTED for Conference of 1/23/2026.")], "", ""),
                   "petition pending; distributed for conference of 1/23/2026");
        assert_eq!(status(&[e("2026-01-26", "Petition DENIED.")], "", ""), "petition denied 2026-01-26");
        assert_eq!(status(&[e("2026-10-07", "Argued. For petitioners: ...")], "2026-03-09", ""), "argued 2026-10-07; awaiting decision");
        assert_eq!(status(&[e("2027-03-01", "Judgment REVERSED and case REMANDED. Gorsuch, J., delivered the opinion of the Court.")], "x", ""),
                   "decided 2027-03-01");
        assert_eq!(party("Department of the Air Force, et al., Petitioners"), "Department of the Air Force");
        assert_eq!(party("Prutehi Guahan, fka Prutehi Litekyan"), "Prutehi Guahan");
    }
}
