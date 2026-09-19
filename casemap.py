#!/usr/bin/env python3
"""casemap.py -- the citation graph around a case, as Obsidian notes. Markdown only, no PDFs.

    ./casemap.py map cases/463-u-s-29_*.json --depth 1
        Outbound. Every case the root cites gets a note in md-cases/: a stub (front
        matter + links) unless its text is already on disk. --depth 2 also
        fetches the text of the depth-1 cases, to learn what THEY cite.
        Citations inside full notes become [[wikilinks]] to those notes.

    ./casemap.py citedby cases/463-u-s-29_*.json
        Inbound. Lists every case citing the root (CourtListener search), draws
        a simple random sample sized for a +/-5% margin at 95% confidence
        (--margin), reads those opinions, and writes a "## Cited by" section
        into the root's note: who cites it, how often per opinion, which pages,
        which sentences get quoted, with which signals. --full reads them all.
        --list also writes md-cases/<slug>--cited-by.md, the complete list.

Everything downloaded is cached under cases/, so reruns and resumes are cheap.
No language model anywhere: counting, regular expressions, string matching.
"""
import argparse, bisect, html, json, math, random, re, sys, urllib.parse
from collections import Counter
from datetime import date
from pathlib import Path

import case2md as c


class Tok:
    """Look the API token up only if a request is actually needed."""
    def __init__(self):
        self.v = None

    def __call__(self):
        self.v = self.v or c.token()
        return self.v


def root_case(args, index, tok):
    """The case named on the command line -> cluster id (registered, text on disk)."""
    if args.json:
        data = json.loads(Path(args.json).read_text(encoding="utf-8"))
        c.register(data, index, args.json)
        return data["cluster"]["id"]
    return ensure_full(args.cluster, index, tok)


# ================================================================ map (outbound)

def ensure_stub(cid, index, tok):
    if str(cid) in index:
        return True
    cluster = c.call("%s/clusters/%d/" % (c.API, cid), tok(), soft=True)
    if cluster is None:
        return False
    docket = c.call(cluster["docket"], tok(), soft=True) if cluster.get("docket") else None
    data = {"cluster": cluster, "docket": docket, "opinions": []}
    (c.CASES / "stubs").mkdir(parents=True, exist_ok=True)
    (c.CASES / "stubs" / ("%d.json" % cid)).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    c.register(data, index)
    return True


def ensure_full(cid, index, tok):
    ent = index.get(str(cid))
    if ent and ent.get("full") and (c.CASES / ent["full"]).exists():
        return cid
    cluster = c.call("%s/clusters/%d/" % (c.API, cid), tok(), soft=True)
    if cluster is None:
        return None
    path = c.fetch_cluster(cluster, tok())
    c.register(json.loads(path.read_text(encoding="utf-8")), index, path)
    return cid


def cmd_map(args):
    tok, index = Tok(), c.load_index()
    root = root_case(args, index, tok)
    seen, frontier = {root}, [root]
    for d in range(1, args.depth + 1):
        last = d == args.depth
        todo = []
        for cid in frontier:
            data, _ = c.load_case(cid, index)
            todo += [x for x in c.cited_clusters(data) if x not in seen and not seen.add(x)]
        if len(seen) > args.max:
            sys.exit("%d cases at depth %d is over --max %d; raise it if you mean it." % (len(seen), d, args.max))
        need = [x for x in todo if str(x) not in index or (not last and not index[str(x)].get("full"))]
        reqs = len(need) * (2 if last else 5)
        print("depth %d: %d cases cited, %d to download (%s): about %d API requests; %d left today, limits %s per min/hour/day"
              % (d, len(todo), len(need), "metadata only" if last else "full text", reqs, c.budget_left(),
                 "/".join(map(str, c.LIMITS))))
        frontier = []
        for i, cid in enumerate(todo, 1):
            ok = ensure_stub(cid, index, tok) if last else ensure_full(cid, index, tok)
            if not ok:
                print("  [%d/%d] cluster %d: not found, skipped" % (i, len(todo), cid))
                seen.discard(cid)
                continue
            if cid in need:
                e = index[str(cid)]
                print("  [%d/%d] %s, %s (%s)" % (i, len(todo), e["short"] or e["name"], e["cite"], e["year"]))
                c.save_index(index)                      # a killed run resumes where it stopped
            if not last:
                frontier.append(cid)
        c.save_index(index)
    kinds = Counter(c.write_note(cid, index) for cid in sorted(seen) if str(cid) in index)
    print("wrote %d notes in md-cases/: %s" % (sum(kinds.values()), ", ".join("%d %s" % (n, k) for k, n in kinds.items())))
    if kinds.get("unparsed"):
        print("  'unparsed' = text is on disk but only in the format build cannot read yet; written as a stub with its Cites list")


# ================================================================ citedby (inbound)

CIRCUITS = {"ca%d" % i for i in range(1, 12)} | {"cadc", "cafc"}
LEVELS = ("supreme", "circuit", "district", "other")
LEVEL_NAMES = {"supreme": "Supreme Court", "circuit": "Federal courts of appeals",
               "district": "Federal district courts", "other": "Other (state, bankruptcy, specialty)"}


def level_of(r):
    cid = r.get("court_id") or ""
    cs = r.get("court_citation_string") or ""
    if cid == "scotus":
        return "supreme"
    if cid in CIRCUITS:
        return "circuit"
    if re.search(r"\bD\.", cs) and "Bankr" not in cs:          # "S.D.N.Y.", "D.D.C."
        return "district"
    return "other"


def population(cid, op_ids, tok, workdir):
    """Every case citing any of op_ids, via the search API, 20 per page. Resumable:
    results append to population.jsonl and the next-page cursor is saved after each page."""
    state_p, rows_p = workdir / "population.state.json", workdir / "population.jsonl"
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    if not state.get("done"):
        url = state.get("next") or "%s/search/?%s" % (c.API, urllib.parse.urlencode({
            "q": "cites:(%s)" % " OR ".join(str(i) for i in op_ids), "type": "o", "order_by": "dateFiled desc"}))
        while url:
            page = c.call(url, tok())
            if "count" in page and "count" not in state:
                state["count"] = page["count"]
                pages = math.ceil(page["count"] / 20)
                print("  %s citing cases reported; listing them is %d API requests = %.1f days at %d a day"
                      % (page["count"], pages, pages / c.LIMITS[-1], c.LIMITS[-1]))
                (workdir / "_first_page.json").write_text(json.dumps(page, indent=1)[:200000])
            with rows_p.open("a", encoding="utf-8") as fh:
                for r in page.get("results") or []:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            url = page.get("next")
            state.update({"next": url, "done": not url, "retrieved": date.today().isoformat()})
            state_p.write_text(json.dumps(state))
    rows, seen = [], set()
    for line in rows_p.read_text(encoding="utf-8").splitlines() if rows_p.exists() else []:
        r = json.loads(line)
        k = r.get("cluster_id")
        if k and k != cid and k not in seen:
            seen.add(k)
            rows.append(r)
    return rows, state


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


def citing_opinions(row, tok, workdir):
    """Text of each opinion in a citing cluster, cached trimmed to the fields used."""
    ids = [o.get("id") for o in row.get("opinions") or [] if o.get("id")][:4]
    out = []
    (workdir / "ops").mkdir(exist_ok=True)
    for oid in ids:
        p = workdir / "ops" / ("%s.json" % oid)
        if not p.exists():
            o = c.call("%s/opinions/%s/" % (c.API, oid), tok(), soft=True) or {}
            p.write_text(json.dumps({k: o.get(k) for k in ("id", "type", "author_str", "html_with_citations")},
                                    ensure_ascii=False), encoding="utf-8")
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


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
    """HTML -> plain text, one paragraph per line, with each link to the cited
    case turned into \\x01anchor text\\x02."""
    htm = re.sub(r'<a\b[^>]*href="/opinion/%d/[^"]*"[^>]*>(.*?)</a>' % cid,
                 lambda m: "\x01" + re.sub(r"<[^>]+>", "", m.group(1)) + "\x02", htm, flags=re.S)
    htm = re.sub(r"<(p|div|blockquote|br|h\d|footnote|li|tr)\b[^>]*>", "\n", htm)
    text = html.unescape(re.sub(r"<[^>]+>", "", htm))
    return "\n".join(re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n"))


def analyze(ops, cid, first_page, last_page, src_norm, src_offsets):
    """-> {depth, pins, signals, treatment, sentences (indices quoted), contexts}"""
    res = {"depth": 0, "pins": set(), "signals": [], "treatment": set(), "quoted": set(), "contexts": []}
    for o in ops:
        text = marked_text(o.get("html_with_citations") or "", cid)
        for para in text.split("\n"):
            for m in re.finditer("\x01([^\x02]*)\x02", para):
                res["depth"] += 1
                before = para[max(0, m.start() - 700):m.start()].replace("\x01", "").replace("\x02", "")
                after = para[m.end():m.end() + 250].replace("\x01", "").replace("\x02", "")
                # pin cite: "463 U.S. at 43", or "463 U.S. 29, 43", or "Id. at 43"
                pm = (re.search(r"\bat\s+(\d{1,4})", m.group(1)) or re.match(r"\s*,?\s*at\s+(\d{1,4})", after)
                      or re.match(r"\s*,\s*(\d{1,4})\b", after))
                if pm and first_page <= int(pm.group(1)) <= last_page:
                    res["pins"].add(int(pm.group(1)))
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
                    res["contexts"].append((before[-300:] + "⟦" + m.group(1) + "⟧" + after[:120]).strip())
    res["pins"], res["treatment"], res["quoted"] = sorted(res["pins"]), sorted(res["treatment"]), sorted(res["quoted"])
    return res


# ---------------------------------------------------------------- report

def pct(k, n, N):
    if not n:
        return "n/a"
    m = moe(k, n, N)
    return "%d%%" % round(100 * k / n) + (" ±%d" % max(1, round(100 * m)) if m else "")


def row_link(r):
    cites = r.get("citation") or []
    cite = cites[0] if isinstance(cites, list) and cites else (cites if isinstance(cites, str) else "")
    name = re.sub(r"<[^>]+>", "", r.get("caseName") or "case")
    return "[%s](%s%s)%s (%s %s)" % (c.esc(name, "md"), c.CL, r.get("absolute_url") or "/opinion/%s/" % r.get("cluster_id"),
                                     ", " + cite if cite else "", r.get("court_citation_string") or r.get("court_id") or "",
                                     (r.get("dateFiled") or "")[:4])


def cmd_citedby(args):
    tok, index = Tok(), c.load_index()
    cid = root_case(args, index, tok)
    if cid is None:
        sys.exit("cluster not found")
    c.save_index(index)
    data, _ = c.load_case(cid, index)
    ent = index[str(cid)]
    workdir = c.CASES / "citedby" / str(cid)
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. the cited case as sentences (needs its text in the parseable format)
    try:
        meta, src, page_parts = source_sentences(data, index)
    except c.Unsupported as e:
        sys.exit("citedby needs the cited case's text, and %s" % e)
    pages = [int(s["page"]) for s in src if str(s["page"]).isdigit()]
    first_page = int(meta["page"]) if meta["page"].isdigit() else min(pages)
    last_page = max(pages + [first_page])
    norms = [norm(s["text"]) for s in src]
    src_offsets, pos = [], 0
    for n in norms:
        src_offsets.append(pos)
        pos += len(n) + 3
    src_norm = " | ".join(norms)

    # 2. everyone who cites it (exact, from the search index)
    op_ids = [o["id"] for o in data["opinions"] if o.get("id")]
    print("%s, %s: listing citing cases" % (ent["short"], ent["cite"]))
    rows, state = population(cid, op_ids, tok, workdir)
    Nall = len(rows)
    by_level = Counter(level_of(r) for r in rows)
    by_decade = Counter((r.get("dateFiled") or "?")[:3] + "0s" for r in rows)
    frame = [r for r in rows if not args.level or level_of(r) in args.level]
    N = len(frame)
    if not N:
        sys.exit("no citing cases found (population file: %s)" % (workdir / "population.jsonl"))

    # 3. sample (or everything), read, analyze
    n = N if args.full else sample_size(N, args.margin)
    rng = random.Random(cid)                                  # same case -> same sample, every run
    chosen = rng.sample(sorted(frame, key=lambda r: r["cluster_id"]), n)
    have = sum(1 for r in chosen for o in (r.get("opinions") or [])[:4] if (workdir / "ops" / ("%s.json" % o.get("id"))).exists())
    print("  %d citing cases%s; reading %s (%d already cached; the rest is about %d API requests = %.1f days at %d a day)" % (
        N, " at level %s" % "+".join(args.level) if args.level else "",
        "all of them" if n == N else "a random sample of %d (±%d%% at 95%%)" % (n, round(args.margin * 100)),
        have, max(0, n - have), max(0, n - have) / c.LIMITS[-1], c.LIMITS[-1]))
    results = []
    for i, r in enumerate(chosen, 1):
        res = analyze(citing_opinions(r, tok, workdir), cid, first_page, last_page, src_norm, src_offsets)
        res["row"] = r
        results.append(res)
        if i % 25 == 0:
            print("  read %d of %d" % (i, n))
    ok = [x for x in results if x["depth"]]
    na = len(ok)
    if n == N:
        N = na                                   # everything was read: no sampling error to report

    # 4. write the section
    L = ["## Cited by", "",
         "%d citing cases in CourtListener as of %s (its own count: %s). Counts in the first two tables are exact."
         % (Nall, state.get("retrieved", "?"), ent.get("cited_by", "?")), "",
         "| Court level | cases | share |", "|---|---:|---:|"]
    L += ["| %s | %d | %d%% |" % (LEVEL_NAMES[k], by_level[k], round(100 * by_level[k] / Nall)) for k in LEVELS if by_level[k]]
    L += ["", "| Decade | cases |", "|---|---:|"] + ["| %s | %d |" % (k, v) for k, v in sorted(by_decade.items())]
    scope = " (restricted to: %s)" % ", ".join(LEVEL_NAMES[k] for k in args.level) if args.level else ""
    if n == N:
        L += ["", "Everything below is from reading **all %d** citing cases%s; no sampling error." % (N, scope)]
    else:
        L += ["", "Everything below is from a **simple random sample of %d of %d** citing cases%s (seed = cluster id, so "
              "reruns draw the same sample). ± is the 95%% margin with finite-population correction." % (n, N, scope)]
    if na < len(results):
        L += ["%d of the %d had no linked citation in their text (CourtListener matched the cite but did not link it, "
              "or the text is missing); percentages are of the %d that did." % (len(results) - na, len(results), na)]
    L.append("")

    L += ["### Times cited per opinion", "", "| cites in the opinion | opinions | share |", "|---|---:|---:|"]
    for label, lo, hi in (("1 (passing)", 1, 1), ("2–3", 2, 3), ("4–9", 4, 9), ("10+ (heavy)", 10, 10**6)):
        k = sum(1 for x in ok if lo <= x["depth"] <= hi)
        L.append("| %s | %d | %s |" % (label, k, pct(k, na, N)))

    pins = Counter(p for x in ok for p in x["pins"])
    npin = sum(1 for x in ok if x["pins"])
    L += ["", "### Pages cited", "", "%d of %d opinions (%s) give a pin cite. Share = of all opinions read." % (npin, na, pct(npin, na, N)), "",
          "| page | where | opinions | share |", "|---:|---|---:|---:|"]
    for p, k in sorted(pins.items(), key=lambda kv: -kv[1])[:12]:
        L.append("| %d | %s | %d | %s |" % (p, "; ".join(page_parts.get(str(p), [])) or "", k, pct(k, na, N)))

    passing = [x for x in ok if x["depth"] == 1]
    heavy = [x for x in ok if x["depth"] >= 4]
    quoted = Counter(i for x in ok for i in x["quoted"])
    L += ["", "### Most-quoted sentences", "",
          "A sentence counts once per citing opinion that quotes at least five consecutive words of it near the citation. "
          "Passing = opinions citing the case once; heavy = four or more times.", ""]
    for rank, (i, k) in enumerate(quoted.most_common(args.top), 1):
        s = src[i]
        kp = sum(1 for x in passing if i in x["quoted"])
        kh = sum(1 for x in heavy if i in x["quoted"])
        L += ["%d. **%s** of opinions (%d of %d; passing %d/%d, heavy %d/%d) · %s, %s, *%s" % (
              rank, pct(k, na, N), k, na, kp, len(passing), kh, len(heavy), s["who"], ("Part " + s["part"]) if s["part"] and not s["part"].startswith("n.") else (s["part"] or "intro"), s["page"]),
              "   > %s" % c.esc(s["text"], "md"), ""]
    if not quoted:
        L += ["*No verbatim quotations matched.*", ""]

    sig = Counter(s for x in ok for s in x["signals"])
    tot = sum(sig.values())
    L += ["### Signals", "", "Per citation (%d citations in the opinions read). Heuristic: the last signal word within 160 characters before the cite." % tot, "",
          "| signal | citations | share |", "|---|---:|---:|"]
    L += ["| %s | %d | %d%% |" % (k, v, round(100 * v / tot)) for k, v in sig.most_common()]
    treat = [x for x in ok if x["treatment"]]
    L += ["", "%d of %d opinions (%s) have a treatment word (overrule, abrogate, supersede, distinguish, decline to follow, limited to) "
          "within a sentence or so of the cite. These are the ones to read; the word may be about a different case." % (len(treat), na, pct(len(treat), na, N)), ""]
    for x in sorted(treat, key=lambda x: -x["depth"])[:10]:
        L.append("- %s: %s (cites it ×%d)" % (row_link(x["row"]), ", ".join(x["treatment"]), x["depth"]))
    L += ["", "### Heaviest users in the sample", ""]
    L += ["- ×%d %s" % (x["depth"], row_link(x["row"])) for x in sorted(ok, key=lambda x: -x["depth"])[:10]]
    L.append("")
    (workdir / "section.md").write_text("\n".join(L), encoding="utf-8")
    (workdir / "results.json").write_text(json.dumps(
        [{"cluster_id": x["row"].get("cluster_id"), "case": x["row"].get("caseName"), "court": x["row"].get("court_id"),
          "date": x["row"].get("dateFiled"), "depth": x["depth"], "pins": x["pins"], "signals": x["signals"],
          "treatment": x["treatment"], "quoted_sentences": x["quoted"], "contexts": x["contexts"]} for x in results],
        indent=1, ensure_ascii=False), encoding="utf-8")

    if args.list:
        P = ["---", 'title: "Cases citing %s"' % ent["cite"], "type: citing-list", "---", "",
             "# Cases citing [[%s|%s, %s]]" % (ent["slug"], c.esc(ent["short"], "md"), ent["cite"]), "",
             "%d cases, newest first within each level. Retrieved %s." % (Nall, state.get("retrieved", "?")), ""]
        for k in LEVELS:
            grp = [r for r in rows if level_of(r) == k]
            if grp:
                P += ["## %s (%d)" % (LEVEL_NAMES[k], len(grp)), ""] + ["- " + row_link(r) for r in grp] + [""]
        (c.NOTES / (ent["slug"] + "--cited-by.md")).write_text("\n".join(P), encoding="utf-8")
        print("wrote md-cases/%s--cited-by.md (%d cases)" % (ent["slug"], Nall))

    c.write_note(cid, index)
    print("wrote the '## Cited by' section into md-cases/%s.md  (%d read, %d with a linked cite; raw results in cases/citedby/%d/results.json)"
          % (ent["slug"], len(results), na, cid))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("map", cmd_map), ("citedby", cmd_citedby)):
        p = sub.add_parser(name)
        p.add_argument("json", nargs="?", help="a file written by case2md.py fetch")
        p.add_argument("--cluster", type=int, help="or a CourtListener cluster id (fetched if needed)")
        p.set_defaults(func=fn)
        if name == "map":
            p.add_argument("--depth", type=int, default=1, choices=[1, 2, 3])
            p.add_argument("--max", type=int, default=3000, help="stop if the map would exceed this many notes")
        else:
            p.add_argument("--margin", type=float, default=0.05, help="target 95%% margin of error (default 0.05)")
            p.add_argument("--full", action="store_true", help="read every citing case instead of sampling")
            p.add_argument("--level", nargs="+", choices=LEVELS, help="restrict to these court levels")
            p.add_argument("--list", action="store_true", help="also write the complete list of citing cases as a note")
            p.add_argument("--top", type=int, default=12, help="how many quoted sentences to show")
    args = ap.parse_args()
    if not args.json and not args.cluster:
        ap.error("give a cases/*.json file or --cluster ID")
    args.func(args)


if __name__ == "__main__":
    main()
