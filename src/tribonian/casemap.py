"""casemap.py -- the citation graph around a case, as Obsidian notes. Markdown only, no PDFs.

    tribonian map get --cite "401 U.S. 402"
        One case out of the local database: writes cases/<cite>_<name>.json (the file
        case2md.py build turns into a PDF) and its note. No API.

    tribonian map map --cite "463 U.S. 29" --depth 2
        Outbound, from the local citation map. Every case within --depth citation steps of
        the root gets a note in md-cases/. --text K gives full text to the first K steps
        (default depth - 1) and stubs to the rest; a stub still lists what it cites.
        Citations inside full notes become [[wikilinks]] to those notes. No API.

    tribonian map citedby cases/463-u-s-29_*.json
        Inbound, from the local database that casedb.py builds out of
        CourtListener's bulk files. No API requests. Writes a "## Cited by"
        section into the root's note:
          - how many opinions cite it, and how many times each (exact)
          - which of its sentences the citing courts' parentheticals restate (exact)
          - once casedb has loaded opinions + dockets: citing courts by level and
            decade (exact), and from the citing opinions' own text the pages
            pin-cited, the sentences quoted, and the signals used. Up to
            --sample-over citing opinions are all read; above that a simple random
            sample sized for --margin (default +/-5% at 95%); --full reads all.
        --list also writes md-cases/<slug>--cited-by.md, the complete list.

    tribonian map coupling --cluster 9493818 [--max-df 2000] [--top 30] [--json]
        Coupled cases: the cases citing the most of the same cases as this one, rare
        shared citations weighted up (log N/df). Works for any case, cited or not. Reads
        the co-citation citation cache (cocite.py prep); never run by a build. Writes
        cases/citedby/<cluster>/coupling.json, which case2md.py build then shows.

    tribonian map stubs [--list] [--max N] [--api] [slug or cluster id ...]
        Turn stub notes into full cases: for each stub (most-cited first) take the text
        from the local database and write cases/<cite>_<name>.json plus the full note,
        so tribonian build builds its PDF. --api: when the database has no text build can
        read, fetch it from CourtListener instead (rate-limited; stops cleanly at the
        daily limit, and rerunning resumes). --list: show the stubs, fetch nothing.

    tribonian map semsearch "plain-language text" [--file draft.md] [--court scotus] [--since 1980]
        CourtListener semantic search (API), one search per five-year slice, re-ranked by the semantic
        score and joined to each hit's sub-cluster and area. Writes cases/semsearch/<slug>/results.json.
        See semsearch.py.

Everything downloaded is cached under cases/. No language model anywhere:
counting, regular expressions, string matching.
"""
import argparse, bisect, html, json, math, os, random, re, sqlite3, sys, time, urllib.parse, zlib
from collections import Counter
from datetime import date
from pathlib import Path

from tribonian import case2md as c
from tribonian import casedb


# ================================================================ cases out of the local database

def db_lookup(con, cite):
    """'463 U.S. 29' -> cluster id, from the citations table."""
    mm = re.match(r"\s*(\d+)\s+(.+?)\s+(\d+)\s*$", cite)
    if not mm:
        sys.exit("cannot read %r as 'volume reporter page'" % cite)
    squash = lambda t: re.sub(r"[\s.]", "", t).lower()
    rows = [r for r in con.execute("SELECT cluster_id, reporter FROM cites WHERE volume=? AND page=?", (mm.group(1), mm.group(3)))
            if squash(r[1]) == squash(mm.group(2))]
    ids = sorted({r[0] for r in rows})
    if not ids:
        sys.exit("no case with citation %s in the database" % cite)
    if len(ids) > 1:
        print("%s matches %d records:" % (cite, len(ids)))
        for cid in ids:
            print("  --cluster %-9d %s" % (cid, con.execute("SELECT case_name FROM clusters WHERE id=?", (cid,)).fetchone()[0]))
        sys.exit(1)
    return ids[0]


def db_case(con, cid, with_text):
    """The same {cluster, docket, opinions} structure case2md.py fetch saves from the API, built from
    the bulk tables, plus 'cites': {cluster id: times cited} from the citation map. None if unknown."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(clusters)")]
    r = con.execute("SELECT * FROM clusters WHERE id=?", (cid,)).fetchone()
    if r is None:
        return None
    cl = dict(zip(cols, r))
    cluster = {"id": cid, "case_name": cl["case_name"], "case_name_short": cl["case_name_short"],
               "case_name_full": cl["case_name_full"], "date_filed": cl["date_filed"], "judges": cl["judges"] or "",
               "citation_count": cl["citation_count"], "other_dates": cl["other_dates"] or "", "scdb_id": cl["scdb_id"] or "",
               "precedential_status": cl["precedential_status"], "summary": cl.get("summary") or "", "syllabus": cl.get("syllabus") or "",
               "headmatter": (zlib.decompress(cl["headmatter"]).decode("utf-8", "replace") if isinstance(cl.get("headmatter"), bytes)
                              else cl.get("headmatter") or ""),   # stored compressed; older databases have no column
               "absolute_url": "/opinion/%d/%s/" % (cid, cl["slug"] or "x"),
               "citations": [{"volume": v, "reporter": rp, "page": pg, "type": t} for v, rp, pg, t in
                             con.execute("SELECT volume, reporter, page, type FROM cites WHERE cluster_id=? ORDER BY type, reporter", (cid,))]}
    d = con.execute("SELECT * FROM dockets WHERE id=?", (cl["docket_id"],)).fetchone()
    docket = None
    if d:           # older databases have only id, court_id, docket_number
        docket = dict(zip([r[1] for r in con.execute("PRAGMA table_info(dockets)")], d))
        docket["docket_number"] = docket.get("docket_number") or ""
    ops = con.execute("SELECT id, type, author_str, field, text FROM opinions WHERE cluster_id=? ORDER BY type, id", (cid,)).fetchall()
    ids = [o[0] for o in ops]
    cites = {}
    if ids:
        for k, n in con.execute("SELECT o.cluster_id, sum(e.depth) FROM edges e JOIN opinions o ON o.id = e.cited "
                                "WHERE e.citing IN (%s) GROUP BY o.cluster_id" % ",".join("?" * len(ids)), ids):
            if k != cid:
                cites[str(k)] = n
    opinions = []
    if with_text:
        for oid, typ, author, field, blob in ops:
            o = {"id": oid, "type": typ, "author_str": author or ""}
            if blob:
                o[field] = zlib.decompress(blob).decode("utf-8", "replace")
            opinions.append(o)
    stamp = con.execute("SELECT file FROM loaded WHERE name='opinions'").fetchone()
    return {"cluster": cluster, "docket": docket, "opinions": opinions, "cites": cites,
            "source": "CourtListener bulk data %s" % (re.search(r"\d{4}-\d\d-\d\d", stamp[0]).group(0) if stamp else "")}


def save_case(data, index, full):
    cid = data["cluster"]["id"]
    ent = index.get(str(cid), {})
    if full:
        name = ent.get("full") or "%s_%s.json" % (c.slugify(c.best_cite(data["cluster"])),
                                                  c.slugify(data["cluster"].get("case_name") or "case"))
        path = c.CASES / name
        if not path.exists() or "source" in json.loads(path.read_text(encoding="utf-8")):   # never overwrite an API fetch
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        c.register(data, index, path)
    else:
        (c.CASES / "stubs").mkdir(parents=True, exist_ok=True)
        (c.CASES / "stubs" / ("%d.json" % cid)).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        c.register(data, index)


def root_cluster(args, con):
    if args.json:
        return json.loads(Path(args.json).read_text(encoding="utf-8"))["cluster"]["id"]
    return args.cluster or db_lookup(con, args.cite)


def cmd_get(args):
    """One case out of the database: cases/*.json (what build reads) and its note."""
    con, done = open_db()
    index = c.load_index()
    cid = root_cluster(args, con)
    data = db_case(con, cid, True)
    if data is None:
        if getattr(args, "no_api", False):
            sys.exit("cluster %s is not in the database" % cid)
        return get_api(cid, index)
    save_case(data, index, True)
    c.save_index(index)
    kind = c.write_note(cid, index)
    e = index[str(cid)]
    print("%s, %s (%s): %d opinions, cites %d cases -> cases/%s, md-cases/%s.md (%s)" % (
        e["short"] or e["name"], e["cite"], e["year"], len(data["opinions"]), len(data["cites"]), e["full"], e["slug"], kind))
    print("PDF: tribonian case build cases/%s" % e["full"])


def get_api(cid, index):
    """The fallback for a case the bulk data doesn't have (filed after the last dump): download it
    with case2md's fetch, about 2 + (number of opinions) requests. Same cases/*.json shape, so
    build doesn't know the difference; references come from its `opinions_cited` (case2md.json_cited)."""
    print("cluster %s is not in the local database (newer than the bulk data?); downloading it from CourtListener"
          % cid, file=sys.stderr)
    tok = c.token()
    cluster = c.call("%s/clusters/%s/" % (c.API, cid), tok, soft=True)
    if cluster is None:
        sys.exit("cluster %s: CourtListener has no such case either" % cid)
    out = c.fetch_cluster(cluster, tok)
    data = json.loads(out.read_text(encoding="utf-8"))
    e = c.register(data, index, out)
    c.save_index(index)
    print("%s, %s (%s): %d opinions, cites %d opinions -> cases/%s (from the API)" % (
        e["short"] or e["name"], e["cite"], e["year"], len(data["opinions"]), len(c.json_cited(cid, data)), e["full"]))
    print("PDF: tribonian case build cases/%s" % e["full"])


# ================================================================ stubs -> full cases

def stub_ids(index, picks):
    """Cluster ids that are stubs (no full file), most-cited first; picks narrow by id or slug substring."""
    out = []
    for cid, e in index.items():
        if e.get("full") and (c.CASES / e["full"]).exists():
            continue
        if not (c.CASES / "stubs" / ("%s.json" % cid)).exists():
            continue
        if picks and not any(p == cid or p.lower() in e["slug"] for p in picks):
            continue
        out.append(cid)
    return sorted(out, key=lambda k: -(index[k].get("cited_by") or 0))


def readable(data, index):
    """None if case2md.py build can read this text, else why not."""
    try:
        c.prepare(data, index)
        return None
    except Exception as e:                   # Unsupported, or a parser surprise: either way, not buildable
        return str(e) or type(e).__name__


def cmd_stubs(args):
    index = c.load_index()
    ids = stub_ids(index, args.picks)
    if args.max:
        ids = ids[:args.max]
    if args.list or not ids:
        for cid in ids:
            e = index[cid]
            print("  %-9s cited by %-6s %s, %s (%s)" % (cid, e.get("cited_by") or "?", e["short"] or e["name"], e["cite"], e["year"]))
        print("%d stubs%s" % (len(ids), "" if ids else " to fetch"))
        return
    con = open_db()[0] if casedb.DB.exists() else None
    if con is None and not args.api:
        sys.exit("No local database at %s; add --api to fetch the stubs from CourtListener instead." % casedb.DB)
    tok, got_n, left = None, Counter(), []
    for k, cid in enumerate(ids, 1):
        e = index[cid]
        label = "%s, %s" % (e["short"] or e["name"], e["cite"])
        stub = json.loads((c.CASES / "stubs" / ("%s.json" % cid)).read_text(encoding="utf-8"))
        how, why = None, "not in the local database"
        if con is not None:
            data = db_case(con, int(cid), True)
            if data is not None:
                why = readable(data, index)
                if why is None:
                    save_case(data, index, True)
                    how = "database"
        if how is None and args.api:
            tok = tok or c.token()
            cluster = c.call("%s/clusters/%s/" % (c.API, cid), tok)   # stops cleanly at the daily limit
            out = c.fetch_cluster(cluster, tok)
            data = json.loads(out.read_text(encoding="utf-8"))
            if stub.get("cites") is not None:                           # keep what the citation map knew
                data["cites"] = stub["cites"]
                out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            why = readable(data, index)
            if why is None:
                c.register(data, index, out)
                how = "API"
            else:                                                       # out of cases/, so tribonian build skips it
                (c.CASES / "unparsed").mkdir(exist_ok=True)
                out.replace(c.CASES / "unparsed" / out.name)
        if how:
            c.save_index(index)
            kind = c.write_note(int(cid), index)
            got_n[how] += 1
            print("%3d/%d  ok    %-55s from the %s -> cases/%s (%s note)" % (k, len(ids), label[:55], how, index[cid]["full"], kind))
        else:
            left.append(cid)
            print("%3d/%d  stub  %-55s %s" % (k, len(ids), label[:55], why))
    print("fetched %d (%s), still stubs %d%s" % (sum(got_n.values()), ", ".join("%d %s" % (n, h) for h, n in got_n.items()) or "none",
                                                 len(left), "" if args.api or not left else "   (--api tries CourtListener for these)"))
    if got_n:
        print("PDFs: tribonian build cases")


# ---------------------------------------------------------------- good-law check (no language model)
# Warning words about this case in the opinions that cite it: next to its citations, and in
# sentences that name it ("Chevron is overruled."). A flag with its evidence, for deciding what
# to read -- not a citator. Read: every citing opinion from a court at the case's level or above
# (up to GOODLAW_MAX), plus the GOODLAW_RECENT newest from any court.
# The rules were tuned with goodlaw_eval.py (ruleset R9 there) on set A, 20 overruled Supreme Court
# cases, with 20 never-overruled controls watched throughout; set B, 20 more overruled, was consulted
# once (the same-name rule came from it), so it is a soft check, not a clean holdout. Rerun it after
# changing anything here.
GOODLAW_RECENT, GOODLAW_MAX = 500, 1500
GOODLAW_QUICK = 100           # quick check: only the newest N citing opinions, any court. On goodlaw_eval.py's
                              # 60 cases (2026-10-07): 34 of 40 overruled red, 4 yellow, 2 green (the full check:
                              # 37/2/1), 19 of 20 never overruled green (full: 14). Gives status now, not the year.
GOODLAW_VERSION = 5            # bump when the rules change: cached results are redone
RANK = {"supreme": 3, "circuit": 2, "district": 1, "other": 0}
V_PASSIVE = r"overruled|abrogated|superseded|rev'd|reversed|vacated|disapproved"
V_MORE = V_PASSIVE + r"|overturned|repudiated|abandoned"
HIST_TAIL = r"(?=\s*(?:by\b|in\b|on\b|sub\b|as\b|[,;.)\]]|$))"
# "we overrule X" -- the court doing it
OVERRULE_HERE_RE = re.compile(r"\b(?:we|today we|we now|we hereby)\s+(?:\w+\s+)?(overrule|disapprove|abrogate)\b[^.;]{0,120}$", re.I)
OTHER_GROUNDS_RE = re.compile(r"^[^.;]{0,60}on other grounds", re.I)
# a full stop after a word of 2+ letters or a closing bracket/quote (not "v." or "U. S."), or at the very start
SENTENCE_END = re.compile(r"(?:^|(?<=[a-z0-9]{2})|(?<=[)”\"]))\.\s+(?=[A-Z])")
# a strong word in a sentence like these is a hypothesis, an argument or a prediction, not a holding:
# "Unless Roe ... is overruled", "will declare that Roe is no longer good law", "Defendant argues that Apodaca has been overruled"
SENT_HYPO_RE = re.compile(r"\b(unless|until|if|whether|should|would|will|shall|urg\w*|ask\w*|invit\w*|predict\w*|may|might|could|"
                          r"argu\w*|contend\w*|claim\w*|suggest\w*|assert\w*|insist\w*|maintain\w*|disagree\w*)\b", re.I)
# the same for "we overrule X": not after "unless", "if", "not", "urge" ... within a few words
HYPOTHETICAL_RE = re.compile(r"\b(unless|if|until|whether|not|never|nor|would|should|could|might|may|to|urge[sd]?|ask(?:s|ed)?)\s+(?:\w+\s+){0,4}$", re.I)
WEAK_TIGHT_RE = re.compile(r"\b(questioned|criticized|called into (?:serious )?(?:question|doubt)|declin\w+ to (?:follow|extend)|"
                           r"no longer (?:good law|valid|followed)|limited to its facts|eroded)", re.I)
NAME_STOP = {"united states", "state", "people", "commonwealth", "commissioner", "in re", "ex parte", "doe", "smith"}
STATES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "georgia",
          "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts",
          "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
          "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
          "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
          "wisconsin", "wyoming"}

# The rules as switches, so goodlaw_eval.py can try variants of them. These are R9.
RULES = {"verbs": V_MORE, "names": True, "ing_name": True, "rejected": True, "read": ["above", "newest"],
         "red_above": 1, "red_lower": 2, "sent_guard": True, "sep": True, "tight_is": True, "weak_tight": True}


def compile_rules(rs):
    c = dict(rs)
    verbs = rs["verbs"]
    # right after the citation (pin, parallel cites and parenthetical skipped), after a comma or paren:
    # "463 U.S. 29 (1983), overruled by X" -- never "Casey repudiated" (the case doing it)
    c["anchored"] = re.compile(r"^(?:\s*,\s*(?:at\s+)?\d[\d\s,–-]*)?(?:\s*,\s*\d+\s+[A-Z][\w. ]{1,12}\s+\d+(?:,\s*\d+)?)*"
                               r"(?:\s*\([^()]{0,80}\))?\s*" + (r"[,;(]\s*" if rs.get("sep") else r"[,;]?\s*") +
                               "(" + verbs + ")" + HIST_TAIL, re.I)
    # "<citation> is overruled" / "<citation>, which has been abrogated": the case itself as the subject
    c["is_over"] = re.compile((r"^(?:\s*,\s*(?:at\s+)?\d[\d\s,–-]*)?(?:\s*\([^()]{0,80}\))?\s*,?\s*(?:which\s+|that\s+)?"
                               if rs.get("tight_is") else r"^[^.;]{0,60}?\b") +
                              r"(?:is|are|was|were|has been|have been)\s+(?:hereby\s+|now\s+|effectively\s+|implicitly\s+|expressly\s+)?"
                              r"(" + verbs + r"|no longer good law)\b(?!\s+(?:in part|on other))", re.I)
    return c


def case_names(short, full):
    """The name courts use for the case without citing it: the short name ('Chevron', 'Plessy'),
    not a state or a generic party ('Arizona', 'United States')."""
    n = re.sub(r",.*$|\bet al\.?$", "", (short or "")).strip()
    if n and len(n) >= 3 and n.lower() not in NAME_STOP and n.lower() not in STATES and len(n.split()) <= 3:
        return [n]
    return []


def name_rxs(c, names):
    nverbs = "|".join(v for v in c["verbs"].split("|") if v not in ("rev'd", "reversed", "vacated")) + ("|rejected" if c.get("rejected") else "")
    out = []
    for n in names:
        q = re.escape(n)
        # "Chevron is overruled", "Plessy v. Ferguson contrary to this finding is rejected"
        out.append(("passive", re.compile(r"\b%s\b(?:[^.;]|(?<=\bv)\.){0,60}?\b(?:is|are|was|were|has been|have been|stands)\s+"
                                          r"(?:hereby\s+|now\s+|therefore\s+|accordingly\s+|expressly\s+|effectively\s+)?(%s)\b"
                                          r"(?!\s+(?:in part|on other))" % (q, nverbs))))
        # "we now overrule Chevron", "the Court overruled Chevron"
        out.append(("we", re.compile(r"\b(?:we|today we|we now|we hereby|the Court|this Court|the Supreme Court)\s+(?:\w+\s+){0,2}"
                                     r"(overrule[sd]?|repudiate[sd]?|overturn(?:ed|s)?)\s+%s\b" % q, re.I)))
        if c.get("ing_name"):   # "Brown v. Board, 347 U.S. 483 (1954) (overruling Plessy v. Ferguson ...)"
            out.append(("ing", re.compile(r"\((overruling|abrogating|overturning|repudiating)\s+(?:in part\s+)?%s\b" % q)))
    return out


def sentence_window(before, after):
    """The citation's own sentence: back to the last full stop, on to the next."""
    return re.split(SENTENCE_END, before)[-1][-200:], re.split(SENTENCE_END, after)[0][:150]


def classify(c, before, after):
    """One citation -> (kind, word): kind is strong | other grounds | weak, or (None, None)."""
    hit = c["anchored"].match(after) or c["is_over"].match(after) or OVERRULE_HERE_RE.search(before)
    if hit and c.get("sent_guard"):
        b, a = sentence_window(before, after)
        if SENT_HYPO_RE.search(b) or SENT_HYPO_RE.search(after[:hit.start()]):
            hit = None
    if hit:
        return ("other grounds" if OTHER_GROUNDS_RE.match(after[hit.end():]) else "strong"), hit.group(1).lower()
    b, a = sentence_window(before, after)
    b, a = b[-80:], a[:80]                   # softer words: near the citation, not negated, not in a quotation
    for q in (b, a):
        q2 = re.sub(r"[“\"][^”\"]*[”\"]", " ", q)
        hit = WEAK_TIGHT_RE.search(q2)
        if hit and not re.search(r"\b(not|never|no|nor)\b[^.;]{0,40}$", q2[:hit.start()]):
            return "weak", hit.group(1).lower()
    return None, None


def other_case(s, parties, names, m):
    """True when the name in this match is followed by 'v. <someone else>' ('Aguilar v. Texas' is not 'Aguilar v. Felton')."""
    if len(parties) < 2:
        return False
    for n in names:
        for k in re.finditer(r"\b%s\s+v\.\s+([A-Z][\w'-]+)" % re.escape(n), s[max(0, m.start() - 5):m.end() + 60]):
            if k.group(1).lower() not in re.findall(r"[\w'-]+", parties[1]):
                return True
    return False


def _cite_marks(text, cites):
    """Plain-text opinions have no citation links: mark '463 U.S. 29' (any spacing) instead."""
    for ct in cites:
        vol, rep_, page = str(ct[0] or ""), ct[1] or "", str(ct[2] or "")
        if vol.isdigit() and page.isdigit() and rep_:
            flex = r"\s*".join(re.escape(ch) for ch in rep_.replace(" ", ""))
            text = re.sub(r"\b%s\s+%s\s+%s\b" % (vol, flex, page), lambda m: "\x01" + m.group(0) + "\x02", text)
    return text


def opinion_windows(raw, cid, cites, name_rx):
    """-> (citation windows [(before, anchor, after)], sentences naming the case) for one citing opinion."""
    text = marked_text(raw, cid)
    if "\x01" not in text:                        # no links to this case: look for its citation instead
        text = _cite_marks(marked_text(raw, -1), cites)
    wins = []
    for para in text.split("\n"):
        for m in re.finditer("\x01([^\x02]*)\x02", para):
            wins.append([para[max(0, m.start() - 300):m.start()].replace("\x01", "").replace("\x02", ""), m.group(1),
                         para[m.end():m.end() + 200].replace("\x01", "").replace("\x02", "")])
    plain = text.replace("\x01", "").replace("\x02", "")
    sents = [plain[max(0, m.start() - 160):m.end() + 160] for m in name_rx.finditer(plain)] if name_rx else []
    return wins[:40], sents[:40]


def evaluate(case, rs, c=None):
    """case: {"name", "names", "ops": [{cluster, year, court, above, why, wins, sents, ...}]} -> (status, yellow, red, evidence)."""
    c = c or compile_rules(rs)
    parties = [x.strip().lower() for x in (case.get("name") or "").split(" v. ")]
    names = case.get("names") or []
    nrx = name_rxs(c, names) if rs.get("names") else []
    ev, seen = [], set()
    for o in case["ops"]:
        if not set(o.get("why", ["above", "newest"])) & set(rs["read"]):
            continue
        found = []
        for kind_rx, rx in nrx:
            for s in o["sents"]:
                for m in rx.finditer(s):
                    if other_case(s, parties, names, m):
                        continue
                    if kind_rx != "passive" and HYPOTHETICAL_RE.search(s[max(0, m.start() - 60):m.start()]):
                        continue
                    if c.get("sent_guard") and SENT_HYPO_RE.search(re.split(SENTENCE_END, s[:m.start()])[-1] + s[m.start():m.end()]):
                        continue
                    found.append(("strong", m.group(1).lower(), s))
                    break
        for b, anc, a in o["wins"]:
            k, w = classify(c, b, a)
            if k:
                found.append((k, w, b[-160:] + "⟦" + anc + "⟧" + a[:120]))
        for k, w, snip in found:
            if (o["cluster"], k) in seen:        # once per citing case (a cluster can hold 2 copies)
                continue
            seen.add((o["cluster"], k))
            ev.append({"cluster": o["cluster"], "name": o.get("name", ""), "slug": o.get("slug", "x"), "court": o["court"],
                       "year": o["year"], "above": o["above"], "kind": k, "word": w,
                       "snippet": re.sub(r"\s+", " ", snip).strip()})
    # red: sure -- a court that could overrule it says so, or two lower courts report it
    strong = sorted((e for e in ev if e["kind"] == "strong"), key=lambda e: e["year"])
    red = None
    for i, e in enumerate(strong):
        if sum(x["above"] for x in strong[:i + 1]) >= rs["red_above"] or sum(not x["above"] for x in strong[:i + 1]) >= rs["red_lower"]:
            red = e["year"]
            break
    # yellow: doubts -- any other strong word or "on other grounds", three softer words, or two from courts above
    doubts = sorted((e for e in ev if e["kind"] != "weak"), key=lambda e: e["year"])
    weak = sorted((e for e in ev if e["kind"] == "weak"), key=lambda e: e["year"])
    wa = [e for e in weak if e["above"]]
    cands = [e["year"] for e in doubts[:1]] + ([weak[2]["year"]] if len(weak) >= 3 else []) + ([wa[1]["year"]] if len(wa) >= 2 else [])
    yellow = min(cands) if cands else None
    if red and (not yellow or red < yellow):
        yellow = red
    order = {"strong": 0, "other grounds": 1, "weak": 2}
    ev.sort(key=lambda e: (order[e["kind"]], not e["above"], e["year"]))
    return ("red" if red else "yellow" if yellow else "green"), yellow, red, ev


def citing_rows(con, cid):
    op = casedb.op_table(con)                     # opmap when built: no reads of the opinion-text file
    return con.execute(
        "SELECT DISTINCT o.id, cl.id, cl.date_filed, d.court_id, ct.jurisdiction, ct.citation_string, "
        "coalesce(nullif(cl.case_name_short,''), nullif(cl.case_name,''), 'cluster ' || cl.id), cl.slug "
        "FROM edges e JOIN %s o ON o.id = e.citing JOIN clusters cl ON cl.id = o.cluster_id "
        "LEFT JOIN dockets d ON d.id = cl.docket_id LEFT JOIN courts ct ON ct.id = d.court_id "
        "WHERE e.cited IN (SELECT id FROM %s WHERE cluster_id = ?) AND cl.id != ?" % (op, op), (cid, cid)).fetchall()


def goodlaw_subject(con, cid):
    """The case being checked: (its row, its court level, its citations, its short names, a regex for
    them, overrules(court_id, jurisdiction) -> could that court overrule it)."""
    me = con.execute("SELECT cl.date_filed, d.court_id, ct.jurisdiction, cl.case_name, cl.case_name_short FROM clusters cl "
                     "LEFT JOIN dockets d ON d.id = cl.docket_id LEFT JOIN courts ct ON ct.id = d.court_id WHERE cl.id = ?", (cid,)).fetchone()
    my_level, my_court = (level_of(me[1], me[2]), me[1]) if me else ("other", None)
    cites = con.execute("SELECT volume, reporter, page FROM cites WHERE cluster_id = ?", (cid,)).fetchall()
    names = case_names(me[4] if me else "", me[3] if me else "")
    name_rx = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in names)) if names else None

    def overrules(court_id, jurisdiction):
        lv = level_of(court_id, jurisdiction)
        if my_level == "other":
            return lv == "supreme" or bool(court_id and court_id == my_court)
        return RANK[lv] >= RANK[my_level]
    return me, my_level, cites, names, name_rx, overrules


def goodlaw_case(con, cid, strat=0, show=None, quick=None):
    """Read this case's citing opinions -> the case dict evaluate() takes. strat > 0 adds that many
    older opinions per decade (goodlaw_eval.py uses it to try reading more). quick=N reads only the
    N newest, from any court."""
    me, my_level, cites, names, name_rx, overrules = goodlaw_subject(con, cid)
    rows = citing_rows(con, cid)

    def can_overrule(r):
        return overrules(r[3], r[4])
    newest = sorted(rows, key=lambda r: r[2] or "", reverse=True)
    pick = {}
    for r in ([] if quick else [x for x in newest if can_overrule(x)][:GOODLAW_MAX]):
        pick.setdefault(r[0], [r, set()])[1].add("above")
    for r in newest[:quick or GOODLAW_RECENT]:
        pick.setdefault(r[0], [r, set()])[1].add("newest")
    if strat:
        by_decade = {}
        for r in newest[GOODLAW_RECENT:]:
            by_decade.setdefault((r[2] or "0000")[:3], []).append(r)
        for rs_ in by_decade.values():
            for r in rs_[::max(1, len(rs_) // strat)][:strat]:
                pick.setdefault(r[0], [r, set()])[1].add("strat")
    ops = []
    for oid, (r, why) in pick.items():
        row = con.execute("SELECT field, text FROM ops.opinions WHERE id = ?", (oid,)).fetchone()
        if not row or not row[1]:
            continue
        raw = zlib.decompress(row[1]).decode("utf-8", "replace")
        wins, sents = opinion_windows(raw, cid, cites, name_rx)
        if show and show.lower() in raw.lower():
            print("\n== %s %s (%s)%s  opinion %d, %d citation marks, %d name mentions" % (
                (r[2] or "")[:4], r[6], r[5] or r[3], " could overrule" if can_overrule(r) else "", oid, len(wins), len(sents)))
            c = compile_rules(RULES)
            for b, anc, a in wins:
                print("   %-13s %s" % (classify(c, b, a)[0] or "-", re.sub(r"\s+", " ", b[-90:] + "⟦" + anc + "⟧" + a[:90])))
        ops.append({"oid": oid, "cluster": r[1], "name": r[6], "slug": r[7] or "x", "year": (r[2] or "")[:4],
                    "court": r[5] or r[3] or "?", "level": level_of(r[3], r[4]), "above": can_overrule(r),
                    "why": sorted(why), "wins": wins, "sents": sents})
    return {"cluster": cid, "name": me[3] if me else "", "names": names, "level": my_level, "of": len(rows), "ops": ops}


def goodlaw(con, cid, show=None, quick=None):
    """-> {"status": green|yellow|red, "yellow_year", "red_year", "read", "of", "evidence": [...]}.
    quick=N: the newest N citing opinions only -> also "mode": "quick", "quick": N, "window": [first, last
    year read], and no yellow/red year (an older change falls outside what was read)."""
    case = goodlaw_case(con, cid, show=show, quick=quick)
    rec = recent_data(con, cid)
    if rec:                                   # citing cases newer than the bulk data (casemap.py recent)
        have = {o["cluster"] for o in case["ops"]}
        case["ops"] += [o for o in rec["ops"] if o["cluster"] not in have]
        case["of"] += rec["listed"]
    status, yellow, red, ev = evaluate(case, RULES)
    out = {"status": status, "yellow_year": yellow, "red_year": red, "level": case["level"], "version": GOODLAW_VERSION,
           "read": len(case["ops"]), "of": case["of"], "evidence": ev[:25], "n_evidence": len(ev)}
    if quick:
        yrs = sorted(int(o["year"]) for o in case["ops"] if str(o.get("year") or "").isdigit())
        out.update(mode="quick", quick=quick, yellow_year=None, red_year=None, window=[yrs[0], yrs[-1]] if yrs else None)
    if rec:
        out["recent"] = {"after": rec["after"], "found": rec["found"], "listed": rec["listed"], "read": len(rec["ops"]),
                         "searched": rec["searched"]}
    return out


def cmd_goodlaw(args):
    """Run the good-law check for one case and print the result (and, with --show, what it read)."""
    con, done = open_db()
    cid = root_cluster(args, con)
    r = goodlaw(con, cid, args.show, quick=args.quick)
    if args.quick:
        print("\n%s: status %s (quick: the newest %d of %d citing opinions, %s); case level: %s" % (
            cid, r["status"].upper(), r["read"], r["of"], "%d-%d" % tuple(r["window"]) if r.get("window") else "none", r["level"]))
    else:
        print("\n%s: status %s, yellow from %s, red from %s; read %d of %d citing opinions (case level: %s)" % (
            cid, r["status"].upper(), r["yellow_year"], r["red_year"], r["read"], r["of"], r["level"]))
    for e in r["evidence"]:
        print("  %s %-12s %-13s %-18s %s| %s" % (e["year"], e["court"][:12], e["kind"], e["word"], "above " if e["above"] else "", e["snippet"][:160]))

# ================================================================ recent citers (after the bulk data)
# The good-law check reads citing opinions from the local database, which ends at the bulk dump: an
# overruling filed since is exactly what it can't see. `recent` asks CourtListener's search who has cited
# the case since (cites:(opinion ids), 20 per request), reads the text of those that could overrule it
# plus the newest few (1 request each, cached under cases/api/opinions/), and stores their reading
# windows in cases/citedby/<cluster>/recent.json; goodlaw() adds them to its reading set from then on.
RECENT_PAGES, RECENT_TEXTS, RECENT_NEWEST = 3, 25, 10
API_LAG_DAYS = 30            # as case2md.API_LAG_DAYS: cases filed just before a dump reach it late


def recent_path(cid):
    return c.CASES / "citedby" / str(cid) / "recent.json"


def recent_data(con, cid):
    """The stored recent citers, if they were searched against this bulk data (a newer dump covers them)."""
    try:
        rec = json.loads(recent_path(cid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if rec.get("stamp") == c.bulk_stamp(con) else None


def own_records(con, cid):
    """-> (opinion ids, cluster ids) of this case and its other records (CourtListener often holds a case
    twice, slip opinion and reporter version, sharing a reporter citation), plus the case file's own
    opinions (a case fetched from the API isn't in the database)."""
    clusters = {cid}
    for v, rp, pg in con.execute("SELECT volume, reporter, page FROM cites WHERE cluster_id = ?", (cid,)).fetchall():
        clusters.update(r for (r,) in con.execute("SELECT cluster_id FROM cites WHERE volume=? AND reporter=? AND page=?", (v, rp, pg)))
    ids = {r for k in clusters for (r,) in con.execute("SELECT id FROM ops.opinions WHERE cluster_id = ?", (k,))}
    ent = c.load_index().get(str(cid), {})
    if ent.get("full"):
        try:
            data = json.loads((c.CASES / ent["full"]).read_text(encoding="utf-8"))
            ids.update(o["id"] for o in data.get("opinions") or [] if o.get("id"))
        except (OSError, ValueError):
            pass
    return sorted(ids), sorted(clusters)


def api_opinion_text(oid, tok):
    """One opinion's text from the API (html_with_citations, else plain text), cached."""
    import gzip, html
    path = c.CASES / "api" / "opinions" / ("%d.html.gz" % oid)
    if path.exists():
        return gzip.decompress(path.read_bytes()).decode("utf-8")
    o = c.call("%s/opinions/%d/?fields=id,html_with_citations,plain_text" % (c.API, oid), tok, soft=True) or {}
    raw = o.get("html_with_citations") or ("<pre>%s</pre>" % html.escape(o["plain_text"]) if o.get("plain_text") else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".%d.tmp" % os.getpid())
    tmp.write_bytes(gzip.compress(raw.encode("utf-8")))
    os.replace(tmp, path)
    return raw


def cmd_recent(args):
    """Who has cited the case since the bulk data, and what they say about it (for the good-law check)."""
    import datetime
    con, done = open_db()
    cid = root_cluster(args, con)
    stamp = c.bulk_stamp(con)
    after = args.after or (str(datetime.date.fromisoformat(stamp) - datetime.timedelta(days=API_LAG_DAYS)) if stamp else "")
    oids, clusters = own_records(con, cid)
    if not oids:
        sys.exit("cluster %s: no opinion ids, here or in a case file (tribonian map get --cluster %s first)" % (cid, cid))
    me, my_level, cites, names, name_rx, overrules = goodlaw_subject(con, cid)
    tok = c.token()
    params = {"type": "o", "q": "cites:(%s)" % " OR ".join(map(str, oids)), "order_by": "dateFiled desc"}
    if after:
        params["filed_after"] = after
    url, hits, found = "%s/search/?%s" % (c.API, urllib.parse.urlencode(params)), [], 0
    for _ in range(args.pages):
        res = c.call(url, tok)
        found = res.get("count") or found
        hits += res.get("results") or []
        url = res.get("next")
        if not url:
            break
    # citing cases the local database lacks (those it has, goodlaw already reads), one per cluster
    ids = list({h["cluster_id"] for h in hits if h.get("cluster_id") and h["cluster_id"] not in clusters})
    local = {r for (r,) in con.execute("SELECT id FROM clusters WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)} if ids else set()
    jur = dict(con.execute("SELECT id, jurisdiction FROM courts"))
    citers, seen = [], set()
    for h in hits:
        k = h.get("cluster_id")
        if not k or k in clusters or k in local or k in seen:
            continue
        seen.add(k)
        court = h.get("court_id") or ""
        citers.append({"cluster": k, "name": h.get("caseName") or "", "date": h.get("dateFiled") or "",
                       "court": h.get("court_citation_string") or court, "court_id": court,
                       "level": level_of(court, jur.get(court)), "above": overrules(court, jur.get(court)),
                       "opinions": [o["id"] for o in h.get("opinions") or [] if o.get("id")]})
    pick = [x for x in citers if x["above"]] + [x for x in citers if not x["above"]][:RECENT_NEWEST]
    pick = pick[:args.texts]
    ops = []
    for x in pick:
        for oid in x["opinions"]:
            raw = api_opinion_text(oid, tok)
            if not raw:
                continue
            wins, sents = [], []
            for k in [cid] + [k for k in clusters if k != cid]:     # links may point at another record of the case
                wins, sents = opinion_windows(raw, k, cites, name_rx)
                if wins:
                    break
            ops.append({"oid": oid, "cluster": x["cluster"], "name": x["name"], "slug": "x", "year": x["date"][:4],
                        "court": x["court"], "level": x["level"], "above": x["above"],
                        "why": ["above"] if x["above"] else ["newest"], "wins": wins, "sents": sents})
    rec = {"cluster": cid, "stamp": stamp, "after": after, "searched": time.strftime("%Y-%m-%d %H:%M"),
           "found": found, "listed": len(citers), "citers": citers, "ops": ops}
    c.write_profile(recent_path(cid), rec)
    print("%s: %s citing decisions filed after %s; %d not in the local database (%d could overrule it); read %d opinions" % (
        cid, found, after or "any date", len(citers), sum(x["above"] for x in citers), len(ops)))
    if found and found > len(hits):
        print("  listed the newest %d of %d (--pages %d); citing cases from the local database are read anyway" % (
            len(hits), found, args.pages))
    for x in citers[:15]:
        print("  %s %-10s %s%s" % (x["date"], x["court"][:10], x["name"][:70], "  · could overrule" if x["above"] else ""))
    ent = c.load_index().get(str(cid), {})
    print("next: the good-law check reads them on the next full build" +
          (": tribonian case build cases/%s" % ent["full"] if ent.get("full") else ""))


# ================================================================ map (outbound)

def cocite_name(ref):
    try:
        from tribonian import cocite
        db = cocite.open_view(None)
        r = db.execute("SELECT name FROM names WHERE ref=?", (ref,)).fetchone() if db else None
        return r[0] if r else "cluster %d" % ref
    except Exception:
        return "cluster %d" % ref


def cmd_map(args):
    con, done = open_db()
    missing = {"citation-map", "citations", "opinion-clusters", "dockets", "opinions"} - done
    if missing:
        sys.exit("map needs these loaded first: tribonian db load %s" % " ".join(sorted(missing)))
    index = c.load_index()
    root = root_cluster(args, con)
    mode = args.by or ("depth" if args.depth is not None else "cluster")
    level = {root: 0}
    if mode == "cluster":
        try:
            from tribonian import cocite
            got = cocite.crawl_set(root, view=args.courts or cocite.NOTE_VIEW, top=args.cluster_max, scope=args.scope)
        except Exception as e:
            got = None
            print("cluster crawl unavailable (%s)" % e)
        if got is None:
            print("not in any co-citation cluster (or cocite.py not built for --courts %s): falling back to --depth 1" % args.courts)
            mode = "depth"
        else:
            for r, why in got:
                level[r] = 1
            print("cluster crawl: %d cases around the root (%s)" % (len(got), args.scope))
            for r, why in got[:10]:
                print("  %-60s %s" % (cocite_name(r)[:60], why))
            if len(got) > 10:
                print("  ... %d more" % (len(got) - 10))
    if mode == "depth":
        args.depth = args.depth or 1
    text_levels = (args.depth - 1 if mode == "depth" else 1) if args.text is None else args.text
    frontier = [root]
    for d in range(1, (args.depth if mode == "depth" else 0) + 1):
        nxt = []
        for cid in frontier:
            data = db_case(con, cid, False)
            for k in (data or {}).get("cites", {}):
                if int(k) not in level:
                    level[int(k)] = d
                    nxt.append(int(k))
        print("depth %d: %d new cases (%d so far)" % (d, len(nxt), len(level)))
        if len(level) > args.max:
            sys.exit("%d cases is over --max %d. Lower --depth, or raise --max if you mean it." % (len(level), args.max))
        frontier = nxt
    n_text = sum(1 for d in level.values() if d <= text_levels)
    print("writing %d notes: full text for depth 0-%d (%d cases), stubs for the rest" % (len(level), text_levels, n_text))
    for k, (cid, d) in enumerate(sorted(level.items(), key=lambda kv: kv[1]), 1):
        ent = index.get(str(cid), {})
        want_text = d <= text_levels
        have_api_copy = ent.get("full") and (c.CASES / ent["full"]).exists()
        if not (have_api_copy and want_text):
            data = db_case(con, cid, want_text or bool(ent.get("full")))
            if data is None:
                level[cid] = None
                continue
            if have_api_copy:                      # keep the fetched file; just record what it cites
                old = json.loads((c.CASES / ent["full"]).read_text(encoding="utf-8"))
                old["cites"] = data["cites"]
                (c.CASES / ent["full"]).write_text(json.dumps(old, indent=1, ensure_ascii=False), encoding="utf-8")
            else:
                save_case(data, index, want_text)
        if k % 200 == 0:
            print("  %d of %d" % (k, len(level)))
    c.save_index(index)
    kinds = Counter(c.write_note(cid, index) for cid, d in level.items() if d is not None and str(cid) in index)
    print("wrote %d notes in md-cases/: %s" % (sum(kinds.values()), ", ".join("%d %s" % (n, k) for k, n in kinds.items())))
    if kinds.get("unparsed"):
        print("  'unparsed' = the text is there but not in the Harvard format build can read yet; written as a stub with its Cites list")


# ================================================================ citedby (inbound)

def sample_size(N, margin, z=1.96):
    """Cochran's n for a proportion at worst case p = 0.5, with finite-population correction."""
    n0 = z * z * 0.25 / (margin * margin)
    return min(N, math.ceil(n0 / (1 + (n0 - 1) / N))) if N else 0


def moe(k, n, N, z=1.96):
    """Half-width of the 95% interval for k/n observed in a simple random sample of n from N."""
    if not n or n >= N:
        return 0.0
    p = k / n
    return z * math.sqrt(p * (1 - p) / n * (N - n) / (N - 1))


# ---------------------------------------------------------------- the cited case, cut into sentences

NO_BREAK = {"v", "co", "inc", "corp", "ltd", "no", "nos", "stat", "cong", "sess", "rep", "fed", "reg", "ed",
            "supp", "cir", "app", "cf", "ibid", "id", "op", "cit", "ass", "assn", "mfrs", "mut", "ins", "auto",
            "dept", "e", "g", "i", "u", "s", "c", "f", "d", "pp", "p", "n", "nn", "ch", "pt", "art", "sec",
            "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sept", "sep", "oct", "nov", "dec", "etc", "al", "et"}


def sentences(text):
    """Split on . ? ! followed by space + capital/quote, except after an abbreviation or an initial."""
    out, start = [], 0
    for m in re.finditer('[.?!]["”’)\\]]*\\s+(?=(?:\x04\\d+\x05\\s*)?[A-Z“"‘(\\[])', text):
        word = re.findall(r"([A-Za-z]+)\W*$", text[start:m.start() + 0] or " ")
        if text[m.start()] == "." and word and (word[-1].lower() in NO_BREAK or len(word[-1]) == 1):
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    if text[start:].strip():
        out.append(text[start:].strip())
    return out


def norm(t):
    t = t.lower().replace("[", "").replace("]", "")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def source_sentences(data, index):
    """[{who, part, page, text}] for every sentence of the cited case, footnotes included."""
    meta, ops, _ = c.prepare(data, index)
    out, where = [], {}                     # where: page -> ["White III", ...] (body text only)

    def flat(el, st):                       # text of a block; st["page"] follows the *NN markers
        bits = []
        for k in el.kids:
            if isinstance(k, str):
                bits.append(k)
            elif k.tag == "page-number":
                st["page"] = k.attrs.get("label") or st["page"]
                bits.append(" \x04%s\x05 " % st["page"])
            elif k.tag != "footnotemark":
                bits.append(flat(k, st))
        return "".join(bits)

    for o in ops:
        st = {"page": o["start"] or meta["page"]}

        def add(el, part):
            page0 = st["page"]
            text = re.sub(r"\s+", " ", flat(el, st)).strip()
            for s in sentences(text):
                pages = re.findall("\x04(\\d+)\x05", s)
                clean_s = re.sub(" ?\x04\\d+\x05 ?", " ", s).strip()
                if not part.startswith("n."):
                    label = "%s %s" % (o["ctx"].who.split(",")[0], part or "intro")
                    for pg in [page0] + pages:
                        if label not in where.setdefault(str(pg), []):
                            where[str(pg)].append(label)
                if len(clean_s.split()) >= 4:
                    out.append({"who": o["ctx"].who, "part": part, "page": page0, "text": clean_s})
                if pages:
                    page0 = pages[-1]

        def walk(part):
            for kind, el in part.blocks:
                add(el, part.path)
            for ch in part.children:
                walk(ch)
        walk(o["tree"])
        for label, fn in o["footnotes"].items():
            keep = st["page"]
            add(fn, "n. %s" % label)
            st["page"] = keep
    return meta, out, where


# ---------------------------------------------------------------- one citing opinion

SIGNAL_RE = re.compile(r"\b(See, e\. ?g\.,|See also|See generally|See|see, e\. ?g\.,|see also|see generally|see|"
                       r"But see|but see|But cf\.|but cf\.|Cf\.|cf\.|Accord|accord|Compare|Contra|"
                       r"quoting|citing|cited in)(?=[\s,])")
TREATMENT_RE = re.compile(r"\b(overrul\w+|abrogat\w+|supersed\w+|distinguish\w+|declin\w+ to follow|"
                          r"limited (?:to|by)|no longer good law|called into (?:question|doubt))", re.I)


def marked_text(htm, cid):
    """HTML -> plain text, one paragraph per line, with each link to the cited case turned
    into \x01anchor text\x02. Many opinions are <pre> text with a hard line break every 70
    characters, so a single newline is just a space; blank lines and block tags are the
    paragraph breaks. Words hyphenated across a line break are rejoined, and star-page
    markers (*673) dropped, so that quotations still match."""
    # \x06 = paragraph break. Decide them on the RAW html: a blank line there is a real one
    # (<pre> text). After tags are stripped, pretty-printed html is full of lines that only
    # held a tag, and those must not count.
    htm = re.sub(r"\n[ \t]*\n", "\x06", htm.replace("\r", ""))
    # CourtListener often puts the pin in the link itself: href="/opinion/110991/slug/#43"
    htm = re.sub(r'<a\b[^>]*href="/opinion/%d/[^"#]*(?:#p?(\d+))?"[^>]*>(.*?)</a>' % cid,
                 lambda m: "\x01" + re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
                           + ("\x07" + m.group(1) if m.group(1) else "") + "\x02", htm, flags=re.S)
    htm = re.sub(r"<(p|div|blockquote|h\d|footnote|li|tr|pre)\b[^>]*>", "\x06", htm)
    text = html.unescape(re.sub(r"<[^>]+>", "", htm))
    text = re.sub(r"(\w)-\n[ \t]*(?=[a-z])", r"\1", text)
    text = re.sub(r"\*+\d+", " ", text)
    paras = [re.sub(r"\s+([,.;:)])", r"\1", re.sub(r"\s+", " ", p)).strip() for p in text.split("\x06")]
    return "\n".join(p for p in paras if p)


def parallel_patterns(citations, main_reporter="U.S."):
    """Regexes that find a pin in a parallel reporter: '103 S.Ct. 2856, 2866' -> ('S. Ct.', 2866)."""
    out = []
    for ct in citations or []:
        rep_, vol, page = ct.get("reporter") or "", str(ct.get("volume") or ""), str(ct.get("page") or "")
        if rep_ and rep_ != main_reporter and vol.isdigit() and page.isdigit():
            flex = r"\s*".join(re.escape(ch) for ch in rep_.replace(" ", ""))
            out.append((rep_, re.compile(r"\b%s\s*%s\s*%s\s*,\s*(\d{1,5})" % (vol, flex, page)),
                        re.compile(r"\b%s\s*%s\s*,?\s*at\s*(\d{1,5})" % (vol, flex))))
    return out


def analyze(ops, cid, first_page, last_page, src_norm, src_offsets, parallels=()):
    """-> {depth, pins, signals, treatment, sentences (indices quoted), contexts}"""
    res = {"depth": 0, "pins": set(), "signals": [], "treatment": set(), "quoted": set(), "contexts": [],
           "pairs": [], "other_pins": []}       # (U.S. pin, reporter, its pin) seen together; parallel pins seen alone
    for o in ops:
        text = marked_text(o.get("html_with_citations") or "", cid)
        for para in text.split("\n"):
            for m in re.finditer("\x01([^\x02]*)\x02", para):
                res["depth"] += 1
                before = para[max(0, m.start() - 700):m.start()].replace("\x01", "").replace("\x02", "")
                after = para[m.end():m.end() + 250].replace("\x01", "").replace("\x02", "")
                # pin cite: "463 U.S. at 43", or "463 U.S. 29, 43", or "Id. at 43"
                anchor, _, frag = m.group(1).partition("\x07")
                # "463 U.S. 29, 43" or "463 U.S. at 43, 52" inside the link; else ", 43" / " at 43" right after it
                tail = re.sub(r"^.*?\b(?:at\b|%d\b)" % first_page, "", anchor, count=1) if re.search(r"\bat\b|\b%d\b" % first_page, anchor) else ""
                found = [int(x) for x in re.findall(r"\b(\d{1,4})\b", tail)]
                if not found:
                    pm = re.match(r"\s*,?\s*at\s+(\d{1,4})", after) or re.match(r"\s*,\s*(\d{1,4})\b", after)
                    found = [int(pm.group(1))] if pm else []
                if frag.isdigit():
                    found.append(int(frag))
                here = [x for x in found if first_page < x <= last_page]
                res["pins"].update(here)
                for rep_, full, short in parallels:
                    pp = full.search(after[:160]) or short.search(anchor + after[:160])
                    if pp:
                        if here:
                            res["pairs"].append((here[0], rep_, int(pp.group(1))))
                        else:
                            res["other_pins"].append((rep_, int(pp.group(1))))
                # signal: the last one within 140 characters before the citation (the case name sits between)
                sig = None
                for s in SIGNAL_RE.finditer(before[-160:]):
                    sig = s
                res["signals"].append(sig.group(1).lower().replace("e. g.", "e.g.") if sig else "(none)")
                for t in TREATMENT_RE.finditer(before[-250:] + " " + after[:150]):
                    res["treatment"].add(t.group(1).lower())
                # quoted language that is verbatim from the cited case
                window = before + " " + after
                for q in re.findall(r"“([^”]{20,700})”", window) + re.findall(r'"([^"]{20,700})"', window):
                    for piece in re.split(r"\.\s?\.\s?\.\s?\.?|…", q):
                        n = norm(piece)
                        if len(n.split()) >= 5:
                            pos = src_norm.find(n)
                            if pos >= 0:
                                res["quoted"].add(bisect.bisect_right(src_offsets, pos) - 1)
                if len(res["contexts"]) < 2:
                    res["contexts"].append((before[-300:] + "⟦" + anchor + "⟧" + after[:120]).strip())
    res["pins"], res["treatment"], res["quoted"] = sorted(res["pins"]), sorted(res["treatment"]), sorted(res["quoted"])
    return res


# ---------------------------------------------------------------- report

def pct(k, n, N):
    if not n:
        return "n/a"
    m = moe(k, n, N)
    return "%d%%" % round(100 * k / n) + (" ±%d" % max(1, round(100 * m)) if m else "")


LEVELS = ("supreme", "circuit", "district", "other")
LEVEL_NAMES = {"supreme": "Supreme Court", "circuit": "Federal courts of appeals",
               "district": "Federal district courts", "other": "Other (state, bankruptcy, specialty)"}


def level_of(court_id, jurisdiction):
    """courts.jurisdiction: F = federal appellate, FD = federal district, FB = bankruptcy, S* = state ..."""
    if court_id == "scotus":
        return "supreme"
    return {"F": "circuit", "FD": "district"}.get(jurisdiction or "", "other")


def open_db():
    if not casedb.DB.exists():
        sys.exit("No database at %s. Build it with tribonian db load (see tribonian db status)." % casedb.DB)
    lock = Path(str(casedb.DB) + ".loading")
    if lock.exists():
        sys.exit("casedb.py is loading into the database right now (%s exists). Reading during a load can make the "
                 "load fail with 'database is locked'. Try again when it has finished." % lock.name)
    con = sqlite3.connect("file:%s?mode=ro" % casedb.DB, uri=True, timeout=30)
    if casedb.OPS_DB.exists():
        con.execute("ATTACH DATABASE ? AS ops", ("file:%s?mode=ro" % casedb.OPS_DB,))
    done = {r[0] for r in con.execute("SELECT name FROM loaded")}
    return con, done


def credit_sentences(words, shingles, MIN=5, MIN_REPEATED=8):
    """Which source sentences does this text restate? A sentence is credited for a run of
    MIN+ consecutive shared words if some 5-word window of the run occurs in that sentence
    only; a run made purely of phrases the opinion itself repeats ("the arbitrary and
    capricious standard") needs MIN_REPEATED+ words and goes to the earliest sentence."""
    best = {}                                    # sentence -> (run length in words, has a unique window)
    run = {}
    for k in range(len(words) - 4):
        here = shingles.get(" ".join(words[k:k + 5]), ())
        nxt = {}
        for i in here:
            length, uniq = run.get(i, (4, False))
            nxt[i] = (length + 1, uniq or len(here) == 1)
            if nxt[i][0] > best.get(i, (0, False))[0] or (nxt[i][1] and not best.get(i, (0, False))[1]):
                best[i] = (max(nxt[i][0], best.get(i, (0, False))[0]), nxt[i][1] or best.get(i, (0, False))[1])
        run = nxt
    out = {i for i, (length, uniq) in best.items() if uniq and length >= MIN}
    repeated = [i for i, (length, uniq) in best.items() if not uniq and length >= MIN_REPEATED]
    if repeated and not out & set(repeated):
        out.add(min(repeated))
    return out


def longest_run(sent_words, grams, text_words, k):
    """Longest run of consecutive words of the sentence that also appears, in order, in the text (>= k, else 0)."""
    best, best_at, i, n = 0, 0, 0, len(text_words)
    while i <= n - k:
        starts = grams.get(tuple(text_words[i:i + k]))
        if not starts:
            i += 1
            continue
        top = 0
        for s in starts:
            length = k
            while s + length < len(sent_words) and i + length < n and sent_words[s + length] == text_words[i + length]:
                length += 1
            if length > top:
                top, at = length, s
        if top > best:
            best, best_at = top, at
        i += max(1, top - k + 1)
    return (best, best_at) if best >= k else (0, 0)


class Lineage:
    """Follow a sentence's wording back through the citation graph. Step 1 = the cases this
    case cites; a case is followed further only if it contains the wording itself, so the
    search stays on the phrase's trail instead of fanning out over the whole graph."""

    def __init__(self, con, own_cluster, start_ids, min_run=7, max_steps=10):
        self.con, self.own, self.start, self.k, self.max_steps = con, own_cluster, list(start_ids), min_run, max_steps
        self.words = {}                           # opinion id -> (cluster, name, date, court, [words])

    def opinion(self, oid):
        if oid not in self.words:
            r = self.con.execute(
                "SELECT o.text, cl.id, coalesce(nullif(cl.case_name,''), nullif(cl.case_name_short,''), 'cluster ' || cl.id), "
                "cl.date_filed, ct.citation_string FROM opinions o JOIN clusters cl ON cl.id = o.cluster_id "
                "LEFT JOIN dockets d ON d.id = cl.docket_id LEFT JOIN courts ct ON ct.id = d.court_id WHERE o.id = ?", (oid,)).fetchone()
            if not r or not r[0]:
                self.words[oid] = None
            else:
                t = html.unescape(re.sub(r"<[^>]+>", " ", zlib.decompress(r[0]).decode("utf-8", "replace")))
                self.words[oid] = (r[1], r[2], r[3] or "", r[4] or "", norm(re.sub(r"(\w)-\n\s*(?=[a-z])", r"\1", t)).split())
        return self.words[oid]

    def trace(self, sentence):
        sw = norm(sentence).split()
        grams = {}
        for s in range(len(sw) - self.k + 1):
            grams.setdefault(tuple(sw[s:s + self.k]), []).append(s)
        found, seen, frontier = {}, {self.own}, self.start
        for step in range(1, self.max_steps + 1):
            if not frontier:
                break
            cited = [r[0] for r in self.con.execute(
                "SELECT DISTINCT cited FROM edges WHERE citing IN (%s)" % ",".join("?" * len(frontier)), frontier)]
            frontier = []
            for oid in cited:
                o = self.opinion(oid)
                if not o or o[0] == self.own:
                    continue
                n, at = longest_run(sw, grams, o[4], self.k)
                if n and any(w == "v" or w.isdigit() for w in sw[at:at + n]):
                    n = 0                 # the shared words are a citation inside the sentence ("... co v ... 380 u s 438"), not wording
                if n:
                    frontier.append(oid)                      # it has the wording: keep following it back
                    if o[0] not in found or n > found[o[0]]["words"]:
                        found[o[0]] = {"cluster": o[0], "case": o[1], "date": o[2], "court": o[3], "step": min(step, found.get(o[0], {}).get("step", step)),
                                       "words": n, "run": " ".join(sw[at:at + n])}
                seen.add(o[0])
        return sorted(found.values(), key=lambda f: (f["date"], f["cluster"]))


def case_label(name, cites, court, year, cluster_id):
    return "[%s](%s)%s (%s %s)" % (c.esc(name or "case", "md"), c.opinion_url(cluster_id, name),
                                               ", " + cites if cites else "", court or "", year or "")


def cmd_citedby(args):
    index = c.load_index()
    if not args.json and (args.cite or args.cluster):      # a case already fetched: find its file in the index
        want = re.sub(r"\s+", "", args.cite or "")
        for k, e in index.items():
            if e.get("full") and (str(args.cluster) == k or (want and want in
                                  [re.sub(r"\s+", "", x) for x in [e.get("cite") or ""] + (e.get("cites") or [])])):
                args.json = str(c.CASES / e["full"])
                break
    if not args.json:
        sys.exit("citedby needs the fetched case file (cases/*.json): its text is what the citing courts are matched against.")
    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    c.register(data, index, args.json)
    c.save_index(index)
    cid = data["cluster"]["id"]
    ent = index[str(cid)]
    workdir = c.CASES / "citedby" / str(cid)
    workdir.mkdir(parents=True, exist_ok=True)
    con, done = open_db()
    for need in ("citation-map", "parentheticals"):
        if need not in done:
            sys.exit("the database has no %s table yet: tribonian db load %s" % (need, need))
    have_text = {"opinions", "opinion-clusters", "dockets", "courts"} <= done

    # 1. the cited case as sentences
    try:
        meta, src, page_parts = source_sentences(data, index)
    except c.Unsupported as e:
        sys.exit("citedby needs the cited case's text, and %s" % e)
    pages = [int(s["page"]) for s in src if str(s["page"]).isdigit()]
    first_page = int(meta["page"]) if meta["page"].isdigit() else min(pages, default=0)   # slip opinion, no star pages: analyze() finds no pins
    last_page = max(pages + [first_page])
    words = [norm(s["text"]).split() for s in src]
    shingles = {}
    for i, w in enumerate(words):
        for k in range(len(w) - 4):
            shingles.setdefault(" ".join(w[k:k + 5]), set()).add(i)
    src_offsets, pos = [], 0
    for w in words:
        src_offsets.append(pos)
        pos += len(" ".join(w)) + 3
    src_norm = " | ".join(" ".join(w) for w in words)

    def where(s):
        part = s["part"]
        return "%s, %s, *%s" % (s["who"], part if part.startswith("n.") else ("Part " + part if part else "intro"), s["page"])

    # 2. who cites it, how many times each: exact, from the citation map
    op_ids = [o["id"] for o in data["opinions"] if o.get("id")]
    q = ",".join("?" * len(op_ids))
    depth = dict(con.execute("SELECT citing, sum(depth) FROM edges WHERE cited IN (%s) GROUP BY citing" % q, op_ids))
    for own in op_ids:
        depth.pop(own, None)
    N = len(depth)
    if not N:
        sys.exit("the citation map has nothing citing opinion ids %s" % op_ids)
    stamp = con.execute("SELECT file FROM loaded WHERE name='citation-map'").fetchone()[0]
    stamp = re.search(r"\d{4}-\d\d-\d\d", stamp).group(0)
    print("%s, %s: cited by %s opinions (bulk data of %s)" % (ent["short"], ent["cite"], format(N, ","), stamp))

    L = ["## Cited by", "",
         "**%s opinions** cite this case (CourtListener bulk data of %s; its own running count: %s). "
         "Exact counts unless a ± is shown." % (format(N, ","), stamp, ent.get("cited_by", "?")), "",
         "### Times cited per opinion", "",
         "Counts every cite, short forms and *id.* included, so one proposition cited once can count as 2. "
         "Mean %.1f." % (sum(depth.values()) / N), "",
         "| cites in the opinion | opinions | share |", "|---|---:|---:|"]
    for label, lo, hi in (("1", 1, 1), ("2–3", 2, 3), ("4–9", 4, 9), ("10+", 10, 10**9)):
        k = sum(1 for d in depth.values() if lo <= d <= hi)
        L.append("| %s | %s | %.1f%% |" % (label, format(k, ","), 100 * k / N))
    L.append("")

    # 3. courts and decades: exact, needs opinions -> clusters -> dockets -> courts
    info = {}
    if have_text:
        ids = list(depth)
        for a in range(0, len(ids), 500):
            chunk = ids[a:a + 500]
            for row in con.execute(
                    "SELECT o.id, cl.id, coalesce(nullif(cl.case_name,''), nullif(cl.case_name_short,''), nullif(cl.case_name_full,''), 'cluster ' || cl.id), "
                    "cl.date_filed, d.court_id, ct.citation_string, ct.jurisdiction "
                    "FROM opinions o JOIN clusters cl ON cl.id = o.cluster_id LEFT JOIN dockets d ON d.id = cl.docket_id "
                    "LEFT JOIN courts ct ON ct.id = d.court_id WHERE o.id IN (%s)" % ",".join("?" * len(chunk)), chunk):
                info[row[0]] = row[1:]
        by_level = Counter(level_of(v[3], v[5]) for v in info.values())
        by_decade = Counter((v[2] or "?")[:3] + "0s" for v in info.values())
        L += ["### Citing courts", "", "| Court level | opinions | share |", "|---|---:|---:|"]
        L += ["| %s | %s | %.1f%% |" % (LEVEL_NAMES[k], format(by_level[k], ","), 100 * by_level[k] / len(info)) for k in LEVELS if by_level[k]]
        L += ["", "| Decade | opinions |", "|---|---:|"] + ["| %s | %s |" % (k, format(v, ",")) for k, v in sorted(by_decade.items())] + [""]

    # 4. what it is cited FOR: every parenthetical, matched to the opinion's own sentences
    parens = con.execute("SELECT describing, score, text FROM parens WHERE described IN (%s)" % q, op_ids).fetchall()
    credited, examples, unmatched = Counter(), {}, []
    for describing, score, text in parens:
        got = credit_sentences(norm(text).split(), shingles)
        for i in got:
            credited[i] += 1
            if score > examples.get(i, (-1, ""))[0]:
                examples[i] = (score, text)
        if not got:
            unmatched.append((score, text))
    nm = len(parens) - len(unmatched)
    L += ["### What it is cited for", "",
          "A parenthetical is the short “(holding that …)” summary a citing court writes after the cite. "
          "%s of them describe this case; %s (%d%%) restate one of its sentences closely enough to match "
          "(5+ consecutive words; phrases the opinion itself repeats need 8+ and go to their first use). "
          "Share = of all %s. “Wording traced back” follows the sentence's own words through the cases this case cites, "
          "then the cases those cite, for as long as the words keep turning up; it sees verbatim language only, and only along "
          "citation links CourtListener recorded." % (format(len(parens), ","), format(nm, ","), round(100 * nm / max(len(parens), 1)), format(len(parens), ",")), ""]
    tracer = Lineage(con, cid, op_ids, args.min_run, args.steps) if have_text else None
    lineages = {}
    for rank, (i, k) in enumerate(credited.most_common(args.top), 1):
        L += ["%d. **%d** (%.0f%%) · %s" % (rank, k, 100 * k / len(parens), where(src[i])),
              "   > %s" % c.esc(src[i]["text"], "md"),
              "   e.g. (%s)" % c.esc(re.sub(r"\s+", " ", examples[i][1])[:260], "md")]
        if tracer:
            chain = lineages[i] = tracer.trace(src[i]["text"])
            if chain:
                L.append("   Wording traced back %d step%s:" % (max(f["step"] for f in chain), "" if max(f["step"] for f in chain) == 1 else "s"))
                for f in chain[:5]:
                    e = index.get(str(f["cluster"]))
                    name = "[[%s|%s]]" % (e["slug"], c.esc(e["short"] or e["name"], "md")) if e else "[%s](%s)" % (c.esc(f["case"], "md"), c.opinion_url(f["cluster"], f["case"]))
                    L.append("   - %s %s (%s; step %d): %d words, “%s”" % (f["date"][:4], name, f["court"], f["step"], f["words"], f["run"]))
                if len(chain) > 5:
                    L.append("   - …and %d later cases carrying the same words, %s–%s (all in results.json)" % (
                        len(chain) - 5, chain[5]["date"][:4], chain[-1]["date"][:4]))
            else:
                L.append("   Wording original to this case: none of the cases it cites shares %d consecutive words of it." % args.min_run)
        L.append("")
    if unmatched:
        L += ["Paraphrases that matched no sentence, highest-scored first:", ""]
        seen = []
        for _, t in sorted(unmatched, reverse=True):
            t = re.sub(r"\s+", " ", t)
            if t not in seen:
                seen.append(t)
        L += ["- (%s)" % c.esc(t[:220], "md") for t in seen[:8]] + [""]

    # 5. the citing opinions' own text: pin cites, quoted sentences, signals
    results = []
    if have_text:
        pool = sorted(info)
        if args.level:
            pool = [i for i in pool if level_of(info[i][3], info[i][5]) in args.level]
        Np = len(pool)
        n = Np if (args.full or Np <= args.sample_over) else sample_size(Np, args.margin)
        chosen = random.Random(cid).sample(pool, n)               # same case -> same sample, every run
        parallels = parallel_patterns(data["cluster"].get("citations"), meta["reporter"].split(" ", 1)[-1])
        print("  reading %s" % ("all %d citing opinions" % Np if n == Np else "a random sample of %d of %d (sized for ±%d%% at 95%%)" % (n, Np, round(args.margin * 100))))
        for k, oid in enumerate(chosen, 1):
            row = con.execute("SELECT text FROM opinions WHERE id=?", (oid,)).fetchone()
            text = zlib.decompress(row[0]).decode("utf-8", "replace") if row and row[0] else ""
            res = analyze([{"html_with_citations": text}], cid, first_page, last_page, src_norm, src_offsets, parallels)
            res["id"] = oid
            results.append(res)
            if k % 1000 == 0:
                print("  read %d of %d" % (k, n))
        ok = [x for x in results if x["depth"]]
        na = len(ok)
        # Many courts pin-cite only the parallel reporter ("103 S.Ct. 2856, 2866"). Opinions that give
        # both pins teach the page correspondence; apply the commonest pairing to the ones that give one.
        votes = {}
        for x in ok:
            for us, rep_, pg in x["pairs"]:
                votes.setdefault((rep_, pg), Counter())[us] += 1
        inferred = 0
        for x in ok:
            if not x["pins"]:
                got = {votes[k].most_common(1)[0][0] for k in x["other_pins"] if k in votes}
                if got:
                    x["pins"] = sorted(got)
                    inferred += 1
        Neff = na if n == Np else Np                               # everything read -> no sampling error
        half = moe(na / 2, na, Neff)                               # achieved worst-case half-width: pct() over na, not the design n
        scope = " at level: %s" % ", ".join(LEVEL_NAMES[k] for k in args.level) if args.level else ""
        if half:
            print("  %d of the %d read had a linked cite; percentages are of those, worst case ±%d%% at 95%%" % (na, n, round(half * 100)))
        L += ["### From the citing opinions' text", "",
              ("All %s citing opinions%s were read." % (format(Np, ","), scope)) if n == Np else
              ("A simple random sample of %d of the %s citing opinions%s was read (seed = cluster id, so reruns draw the same "
               "sample); ± is the 95%% margin with finite-population correction." % (n, format(Np, ","), scope)),
              "%d had a linked cite in their text; the rest were matched by CourtListener but not linked, or are plain text. "
              "Percentages are of those %d%s." % (na, na, " (worst case ±%d%% at 95%%)" % round(half * 100) if half else ""), ""]
        if na:
            pins = Counter(p for x in ok for p in x["pins"])
            npin = sum(1 for x in ok if x["pins"])
            L += ["**Pages cited.** %d of %d opinions (%s) give a pin cite%s." % (
                      npin, na, pct(npin, na, Neff),
                      "; for %d of them the page was given only in a parallel reporter and is translated using the "
                      "opinions that give both" % inferred if inferred else ""), "",
                  "| page | where | opinions | share |", "|---:|---|---:|---:|"]
            for p, k in sorted(pins.items(), key=lambda kv: -kv[1])[:12]:
                L.append("| %d | %s | %d | %s |" % (p, "; ".join(page_parts.get(str(p), [])), k, pct(k, na, Neff)))
            light = [x for x in ok if x["depth"] <= 3]
            heavy = [x for x in ok if x["depth"] >= 4]
            quoted = Counter(i for x in ok for i in x["quoted"])
            L += ["", "**Sentences quoted** (5+ consecutive words inside quotation marks near the cite). "
                  "Light = opinions citing it 1–3 times; heavy = 4 or more.", ""]
            for rank, (i, k) in enumerate(quoted.most_common(args.top), 1):
                kl = sum(1 for x in light if i in x["quoted"])
                kh = sum(1 for x in heavy if i in x["quoted"])
                L += ["%d. **%s** (%d of %d; light %d/%d, heavy %d/%d) · %s" % (rank, pct(k, na, Neff), k, na, kl, len(light), kh, len(heavy), where(src[i])),
                      "   > %s" % c.esc(src[i]["text"], "md"), ""]
            sig = Counter(s for x in ok for s in x["signals"])
            tot = sum(sig.values())
            L += ["**Signals**, per citation (%d citations). Heuristic: the last signal word within 160 characters before the cite." % tot, "",
                  "| signal | citations | share |", "|---|---:|---:|"]
            L += ["| %s | %d | %d%% |" % (k, v, round(100 * v / tot)) for k, v in sig.most_common()]
            treat = sorted((x for x in ok if x["treatment"]), key=lambda x: -x["depth"])
            L += ["", "%d of %d opinions (%s) have a treatment word (overrule, abrogate, supersede, distinguish, decline to follow, "
                  "limited to) near the cite. The word may be about a different case; these are the ones to read." % (len(treat), na, pct(len(treat), na, Neff)), ""]
            for x in treat[:10]:
                v = info[x["id"]]
                L.append("- %s: %s (cites it ×%d)" % (case_label(v[1], "", v[4], (v[2] or "")[:4], v[0]), ", ".join(x["treatment"]), x["depth"]))
            L.append("")
        L += ["### Heaviest users", "", "Cites summed over all of a case's opinions (majority, concurrence, dissent).", ""]
        per_case = {}
        for oid, d in depth.items():
            v = info.get(oid)
            if v:
                e = per_case.setdefault(v[0], [0, v])
                e[0] += d
        for d, v in sorted(per_case.values(), key=lambda e: -e[0])[:12]:
            L.append("- ×%d %s" % (d, case_label(v[1], "", v[4], (v[2] or "")[:4], v[0])))
        L.append("")
    else:
        missing = sorted({"opinions", "opinion-clusters", "dockets", "courts"} - done)
        L += ["*Citing courts, pages cited, quoted sentences and signals appear here once casedb.py has loaded: %s.*" % ", ".join(missing), ""]

    (workdir / "section.md").write_text("\n".join(L), encoding="utf-8")
    (workdir / "results.json").write_text(json.dumps({
        "cluster": cid, "bulk_date": stamp, "citing_opinions": N, "depth": depth,
        "sentences_credited": [{"n": k, "where": where(src[i]), "text": src[i]["text"], "lineage": lineages.get(i)}
                               for i, k in credited.most_common()],
        "text_results": [{"opinion": x["id"], "depth": x["depth"], "pins": x["pins"], "signals": x["signals"],
                          "treatment": x["treatment"], "quoted_sentences": x["quoted"], "other_pins": x["other_pins"],
                          "contexts": x["contexts"]} for x in results]},
        indent=1, ensure_ascii=False), encoding="utf-8")

    if args.list:
        if not have_text:
            print("  --list needs opinions, opinion-clusters, dockets and courts loaded; skipped")
        else:
            P = ["---", 'title: "Cases citing %s"' % ent["cite"], "type: citing-list", "---", "",
                 "# Cases citing [[%s|%s, %s]]" % (ent["slug"], c.esc(ent["short"], "md"), ent["cite"]), "",
                 "%s opinions, most cites first within each level. Bulk data of %s." % (format(len(info), ","), stamp), ""]
            for k in LEVELS:
                grp = sorted((oid for oid in info if level_of(info[oid][3], info[oid][5]) == k), key=lambda o: (-depth[o], info[o][2] or ""))
                if grp:
                    P += ["## %s (%s)" % (LEVEL_NAMES[k], format(len(grp), ",")), ""]
                    P += ["- ×%d %s" % (depth[o], case_label(info[o][1], "", info[o][4], (info[o][2] or "")[:4], info[o][0])) for o in grp] + [""]
            (c.NOTES / (ent["slug"] + "--cited-by.md")).write_text("\n".join(P), encoding="utf-8")
            print("  wrote md-cases/%s--cited-by.md" % ent["slug"])

    c.write_note(cid, index)
    print("  wrote the '## Cited by' section into md-cases/%s.md (raw numbers: cases/citedby/%d/results.json)" % (ent["slug"], cid))


def cmd_coupling(args):
    """Coupled cases: those citing the most of the same cases as this one, rare shared citations
    weighted up (cocite.coupling). Explicit only, never part of a build: it reads the whole citation
    cache (cocite-cache, about 300 MB). Cached in cases/citedby/<cluster>/coupling.json, where
    case2md.py build picks it up for the "Coupled with" section."""
    con, done = open_db()
    cid = root_cluster(args, con)
    t0 = time.time()
    res = c.coupling_cache(cid, con, args.max_df, args.top)
    if args.as_json:
        print(json.dumps(res, indent=1))
        return
    from tribonian import cocite
    name, cite, yr, court = cocite.case_label(con, cid)
    print("%s (%s): cites %d cases; %d used, %d cited by more than %s decisions left out%s" % (
        name, ", ".join(t for t in (cite, court, yr) if t), res["refs"], res["used"], res["skipped"], format(res["max_df"], ","),
        ", %d not in the citation cache" % res["missing"] if res["missing"] else ""))
    if res.get("stale_cache"):
        print("  note: the citation cache is from an older bulk load (tribonian cocite prep refreshes it)")
    print("%4s %7s %6s  %s" % ("#", "score", "shared", "case"))
    for i, x in enumerate(res["results"], 1):
        print("%4d %7.1f %6d  %s%s" % (i, x["score"], x["shared"], x["name"],
                                       " (%s)" % ", ".join(t for t in (x["cite"], x["court"], x["year"]) if t)))
        print("%19s shared: %s" % ("", "; ".join(r["name"] for r in x["refs"][:6]) + (" ..." if len(x["refs"]) > 6 else "")))
    print("wrote %s  (%.1f s)" % (c.coupling_path(cid).relative_to(c.ROOT), time.time() - t0))


def cmd_fixlinks(args):
    """Give every bare https://www.courtlistener.com/opinion/<id>/ link in md-cases/ and in the
    saved Cited-by sections its slug, without rerunning anything."""
    if c.local_db() is None:
        sys.exit("fixlinks needs the local database for the slugs (tribonian db status)")
    rx = re.compile(r"\(%s/opinion/(\d+)/(?:case/)?\)" % re.escape(c.CL))    # bare, or the no-database stand-in
    files = list(c.NOTES.rglob("*.md")) + list((c.CASES / "citedby").glob("*/section.md"))
    changed = links = 0
    for f in files:
        s = f.read_text(encoding="utf-8")
        t, n = rx.subn(lambda m: "(%s)" % c.opinion_url(m.group(1)), s)
        if n:
            f.write_text(t, encoding="utf-8")
            changed, links = changed + 1, links + n
    print("fixed %d links in %d files" % (links, changed))


def main():
    if sys.argv[1:2] == ["fixlinks"]:
        return cmd_fixlinks(None)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stubs", help="fetch full text for stub notes")
    st.add_argument("picks", nargs="*", help="only these: cluster ids, or parts of note names (default: every stub)")
    st.add_argument("--list", action="store_true", help="show the stubs, fetch nothing")
    st.add_argument("--max", type=int, help="at most this many (the most-cited first)")
    st.add_argument("--api", action="store_true", help="fall back to the CourtListener API when the database has no readable text")
    st.set_defaults(func=cmd_stubs)
    from tribonian import semsearch
    ss = sub.add_parser("semsearch", help="CourtListener semantic search, re-ranked, joined to the sub-clusters")
    semsearch.add_args(ss)
    ss.set_defaults(func=semsearch.run)
    for name, fn in (("get", cmd_get), ("map", cmd_map), ("citedby", cmd_citedby), ("goodlaw", cmd_goodlaw),
                     ("coupling", cmd_coupling), ("recent", cmd_recent)):
        p = sub.add_parser(name)
        p.add_argument("json", nargs="?", help="a cases/*.json file")
        p.add_argument("--cluster", type=int, help="or a CourtListener cluster id")
        p.add_argument("--cite", help='or a citation, e.g. "401 U.S. 402"')
        p.set_defaults(func=fn)
        if name == "recent":
            p.add_argument("--after", help="filed after this date (default: the bulk data's date less %d days)" % API_LAG_DAYS)
            p.add_argument("--pages", type=int, default=RECENT_PAGES,
                           help="search pages of 20, newest first (default %d; 1 request each)" % RECENT_PAGES)
            p.add_argument("--texts", type=int, default=RECENT_TEXTS,
                           help="at most this many citing cases read (1 request per opinion): every one that could "
                           "overrule it, then the %d newest (default %d)" % (RECENT_NEWEST, RECENT_TEXTS))
        if name == "get":
            p.add_argument("--no-api", dest="no_api", action="store_true",
                           help="don't download a case the local database lacks (default: fetch it from CourtListener)")
        if name == "map":
            p.add_argument("--by", choices=["cluster", "depth"], help="cluster (the default) = the cases in the root's "
                           "co-citation sub-cluster (cocite.py); depth = follow its citations --depth steps out "
                           "(the default when --depth is given)")
            p.add_argument("--depth", type=int, default=None, help="how many citation steps out from the root get a note (--by depth)")
            p.add_argument("--text", type=int, default=None, help="how many of those steps get full text (default: depth - 1, "
                           "the outer ring is stubs; --by cluster: 1, all of them; 0 = stubs only)")
            p.add_argument("--cluster-max", dest="cluster_max", type=int, default=60,
                           help="--by cluster: at most this many cases, heaviest first (default 60)")
            p.add_argument("--scope", choices=["current", "all"], default="current",
                           help="--by cluster: current = its sub-cluster in the newest full slice, plus cases that joined "
                           "it since; all = every lineage it has belonged to (default current)")
            p.add_argument("--courts", help="--by cluster: cocite.py view (default: the case notes' view, cocite.NOTE_VIEW)")
            p.add_argument("--max", type=int, default=3000, help="stop if the map would exceed this many notes")
        elif name == "coupling":
            p.add_argument("--max-df", dest="max_df", type=int, default=2000,
                           help="leave out cited cases cited by more decisions than this (default 2000)")
            p.add_argument("--top", type=int, default=30, help="how many coupled cases (default 30)")
            p.add_argument("--json", dest="as_json", action="store_true", help="print the result as JSON")
        elif name == "goodlaw":
            p.add_argument("--show", metavar="WORD", help="print every citation window in the opinions read that contain WORD")
            p.add_argument("--quick", type=int, nargs="?", const=GOODLAW_QUICK, metavar="N",
                           help="read only the newest N citing opinions (default %d): status now, not the year it "
                           "changed; prints only, caches nothing" % GOODLAW_QUICK)
        elif name == "citedby":
            p.add_argument("--sample-over", type=int, default=2000, help="read every citing opinion up to this many; sample above it")
            p.add_argument("--margin", type=float, default=0.05, help="target 95%% margin of error when sampling (default 0.05)")
            p.add_argument("--full", action="store_true", help="read every citing opinion, however many")
            p.add_argument("--level", nargs="+", choices=LEVELS, help="restrict to these court levels")
            p.add_argument("--list", action="store_true", help="also write the complete list of citing cases as a note")
            p.add_argument("--top", type=int, default=12, help="how many sentences to show in each ranking")
            p.add_argument("--steps", type=int, default=10, help="how many citation steps back to follow a sentence's wording")
            p.add_argument("--min-run", type=int, default=7, help="consecutive shared words that count as the same wording")
    args = ap.parse_args()
    if args.cmd not in ("stubs", "semsearch") and not (args.json or args.cluster or args.cite):
        ap.error('give a cases/*.json file, --cluster ID or --cite "401 U.S. 402"')
    args.func(args)


if __name__ == "__main__":
    main()
