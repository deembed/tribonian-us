#!/usr/bin/env python3
"""casemap.py -- the citation graph around a case, as Obsidian notes. Markdown only, no PDFs.

    ./casemap.py get --cite "401 U.S. 402"
        One case out of the local database: writes cases/<cite>_<name>.json (the file
        case2md.py build turns into a PDF) and its note. No API.

    ./casemap.py map --cite "463 U.S. 29" --depth 2
        Outbound, from the local citation map. Every case within --depth citation steps of
        the root gets a note in md-cases/. --text K gives full text to the first K steps
        (default depth - 1) and stubs to the rest; a stub still lists what it cites.
        Citations inside full notes become [[wikilinks]] to those notes. No API.

    ./casemap.py citedby cases/463-u-s-29_*.json
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

Everything downloaded is cached under cases/. No language model anywhere:
counting, regular expressions, string matching.
"""
import argparse, bisect, html, json, math, random, re, sqlite3, sys, zlib
from collections import Counter
from datetime import date
from pathlib import Path

import case2md as c
import casedb


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
               "absolute_url": "/opinion/%d/%s/" % (cid, cl["slug"] or "x"),
               "citations": [{"volume": v, "reporter": rp, "page": pg, "type": t} for v, rp, pg, t in
                             con.execute("SELECT volume, reporter, page, type FROM cites WHERE cluster_id=? ORDER BY type, reporter", (cid,))]}
    d = con.execute("SELECT court_id, docket_number FROM dockets WHERE id=?", (cl["docket_id"],)).fetchone()
    docket = {"court_id": d[0], "docket_number": d[1] or ""} if d else None
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
        sys.exit("cluster %s is not in the database" % cid)
    save_case(data, index, True)
    c.save_index(index)
    kind = c.write_note(cid, index)
    e = index[str(cid)]
    print("%s, %s (%s): %d opinions, cites %d cases -> cases/%s, md-cases/%s.md (%s)" % (
        e["short"] or e["name"], e["cite"], e["year"], len(data["opinions"]), len(data["cites"]), e["full"], e["slug"], kind))
    print("PDF: ./case2md.py build cases/%s" % e["full"])


# ================================================================ map (outbound)

def cmd_map(args):
    con, done = open_db()
    missing = {"citation-map", "citations", "opinion-clusters", "dockets", "opinions"} - done
    if missing:
        sys.exit("map needs these loaded first: ./casedb.py load %s" % " ".join(sorted(missing)))
    index = c.load_index()
    root = root_cluster(args, con)
    text_levels = args.depth - 1 if args.text is None else args.text
    level = {root: 0}
    frontier = [root]
    for d in range(1, args.depth + 1):
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
        sys.exit("No database at %s. Build it with ./casedb.py load (see ./casedb.py status)." % casedb.DB)
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
    return "[%s](%s/opinion/%s/)%s (%s %s)" % (c.esc(name or "case", "md"), c.CL, cluster_id,
                                               ", " + cites if cites else "", court or "", year or "")


def cmd_citedby(args):
    index = c.load_index()
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
            sys.exit("the database has no %s table yet: ./casedb.py load %s" % (need, need))
    have_text = {"opinions", "opinion-clusters", "dockets", "courts"} <= done

    # 1. the cited case as sentences
    try:
        meta, src, page_parts = source_sentences(data, index)
    except c.Unsupported as e:
        sys.exit("citedby needs the cited case's text, and %s" % e)
    pages = [int(s["page"]) for s in src if str(s["page"]).isdigit()]
    first_page = int(meta["page"]) if meta["page"].isdigit() else min(pages)
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
                    name = "[[%s|%s]]" % (e["slug"], c.esc(e["short"] or e["name"], "md")) if e else "[%s](%s/opinion/%s/)" % (c.esc(f["case"], "md"), c.CL, f["cluster"])
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
        print("  reading %s" % ("all %d citing opinions" % Np if n == Np else "a random sample of %d of %d (±%d%% at 95%%)" % (n, Np, round(args.margin * 100))))
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
        scope = " at level: %s" % ", ".join(LEVEL_NAMES[k] for k in args.level) if args.level else ""
        L += ["### From the citing opinions' text", "",
              ("All %s citing opinions%s were read." % (format(Np, ","), scope)) if n == Np else
              ("A simple random sample of %d of the %s citing opinions%s was read (seed = cluster id, so reruns draw the same "
               "sample); ± is the 95%% margin with finite-population correction." % (n, format(Np, ","), scope)),
              "%d had a linked cite in their text; the rest were matched by CourtListener but not linked, or are plain text. "
              "Percentages are of those %d." % (na, na), ""]
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("get", cmd_get), ("map", cmd_map), ("citedby", cmd_citedby)):
        p = sub.add_parser(name)
        p.add_argument("json", nargs="?", help="a cases/*.json file")
        p.add_argument("--cluster", type=int, help="or a CourtListener cluster id")
        p.add_argument("--cite", help='or a citation, e.g. "401 U.S. 402"')
        p.set_defaults(func=fn)
        if name == "map":
            p.add_argument("--depth", type=int, default=1, help="how many citation steps out from the root get a note")
            p.add_argument("--text", type=int, default=None, help="how many of those steps get full text (default: depth - 1; the outer ring is stubs)")
            p.add_argument("--max", type=int, default=3000, help="stop if the map would exceed this many notes")
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
    if not (args.json or args.cluster or args.cite):
        ap.error('give a cases/*.json file, --cluster ID or --cite "401 U.S. 402"')
    args.func(args)


if __name__ == "__main__":
    main()
