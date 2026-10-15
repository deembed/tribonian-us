#!/usr/bin/env python3
"""cocite_usc.py -- US Code provisions in the co-citation build (scope: docs/scope-cocite-usc.md).

    ./cocite.py usc-sample --n 2000             # step 1: how Code cites appear in the opinion text; writes nothing
    ./cocite.py usc-sample --n 2000 --examples 10

Step 1 measures, per random opinion with html_with_citations (the only field positions reads):
    span        citation spans CourtListener marked, by kind: linked case, ambiguous, Id., no link
    span_usc    no-link spans whose text is a Code cite (what positions v3 would keep)
    out_usc     Code cites in the text OUTSIDE any span (CourtListener missed them; spans alone lose these)
    out_nosect  of those, written without "§" ("28 U.S.C. 1291")
    bare_res    of those, the same section number cited in full elsewhere in the opinion (title known)
    pop_alone   popular-name cites with no Code cite in the next 150 characters (what would be lost)
    bare_sect   "§ N" with no Code, Stat., C.F.R. or state code just before it: short forms, title unknown
                (federal or state; an upper bound on what short-form resolution could add)
    popular     popular-name cites: "Title VII", "section 10(b) of the ... Act"
by decade of the citing decision. The no-link span shapes (digits as N) show what else sits under -2.
"""
import argparse, html, random, re, sqlite3, sys, zlib
from collections import Counter, defaultdict

import casedb

CITE_RE = re.compile(r'<span class="citation([^"]*)"[^>]*>(.*?)</span>', re.S)
HREF_RE = re.compile(r'href="/opinion/(\d+)/')
ID_RE = re.compile(r"^(?:(?:see also|see|cf\.|accord|but see)\s+)?(?:id\.|ibid\.?)", re.I)
# a Code cite: title, U.S.C. (any spacing, with or without dots, .A / .S. annotated editions), optional §, section
USC_RE = re.compile(r"\b(\d{1,2}[A-Z]?)\s*U\.?\s?S\.?\s?C\.?(?:\s?[AS]\.?)?(?:\s*\((?:19|20)\d\d(?:\s*ed\.)?\))?"
                    r"\s*(§§?|[Ss]ec(?:tions?|s?\.))?\s*(\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?)")
BARE_RE = re.compile(r"§§?\s*\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?")
# what may stand before a "§" that makes it not a short form: a Code cite, Stat., C.F.R., a state code
NOT_BARE = re.compile(r"(U\.?\s?S\.?\s?C\.?[AS]?\.?|Stat\.|C\.\s?F\.\s?R\.?|Code|Ann\.|Rev\.|Laws|Gen\.|Comp\.|"
                      r"Pub\.\s?L\.|Act,?|Rule|R\.|Const\.|art\.)[\s,()\d\w.–-]{0,14}$")
POPULAR_RE = re.compile(r"\bTitle\s+(?:VII|VI|IX|III|II|IV|I)\b(?!\s+of the (?:Constitution|U))|"
                        r"\b[Ss]ection\s+\d+[A-Za-z]?(?:\([A-Za-z0-9]+\))*\s+of\s+the\s+(?:[A-Z][\w'’.-]*\s+){1,8}Act\b")


def sect(s):
    """section number without letters' case or a range's tail: '1371-76' -> '1371', '1915A' -> '1915a'"""
    return s.lower().split("-")[0] if s else ""


def text_of(fragment):
    t = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", t)


def shape(s):
    return re.sub(r"\d+", "N", s)[:40]


def years_of(clusters):
    """cluster id -> filing year, from the main database (empty when it isn't there)."""
    if not casedb.DB.exists() or not clusters:
        return {}
    con = sqlite3.connect("file:%s?mode=ro" % casedb.DB, uri=True)
    out, ids = {}, list(clusters)
    for i in range(0, len(ids), 900):
        part = ids[i:i + 900]
        for cid, y in con.execute("SELECT id, CAST(substr(date_filed, 1, 4) AS INTEGER) FROM clusters WHERE id IN (%s)"
                                  % ",".join("?" * len(part)), part):
            out[cid] = y or 0
    return out


def measure(htm):
    """-> counts dict, examples dict(kind -> [snippet])"""
    c, ex = Counter(), defaultdict(list)
    shapes, full = Counter(), set()
    for m in CITE_RE.finditer(htm):
        cls, inner = m.group(1), m.group(2)
        anchor = text_of(inner).strip()
        if HREF_RE.search(inner):
            c["span_case"] += 1
        elif "multiple" in cls:
            c["span_ambiguous"] += 1
        elif ID_RE.match(anchor):
            c["span_id"] += 1
        else:
            c["span_nolink"] += 1
            um = USC_RE.search(anchor)
            if um:
                c["span_usc"] += 1
                full.add(sect(um.group(3)))
                ex["span_usc"].append(anchor)
            else:
                shapes[shape(anchor)] += 1
    outside = text_of(CITE_RE.sub(" \x00 ", htm))
    for m in USC_RE.finditer(outside):
        c["out_usc"] += 1
        full.add(sect(m.group(3)))
        if not m.group(2):
            c["out_nosect"] += 1
        ex["out_usc"].append(outside[max(0, m.start() - 50):m.end() + 20])
    for m in BARE_RE.finditer(outside):
        before = outside[max(0, m.start() - 40):m.start()]
        if not NOT_BARE.search(before) and "\x00" not in before[-6:]:
            c["bare_sect"] += 1
            if sect(re.sub(r"^§+\s*", "", m.group(0))) in full:
                c["bare_res"] += 1
            ex["bare_sect"].append(outside[max(0, m.start() - 60):m.end() + 20])
    whole = text_of(htm)
    for m in POPULAR_RE.finditer(whole):
        c["popular"] += 1
        if not USC_RE.search(whole[m.end():m.end() + 150]):
            c["pop_alone"] += 1
            ex["pop_alone"].append(whole[max(0, m.start() - 30):m.end() + 60])
    return c, ex, shapes


COLS = ("opinions", "span_case", "span_nolink", "span_usc", "out_usc", "out_nosect", "bare_sect", "bare_res", "popular", "pop_alone")


def cmd_sample(args):
    if not casedb.OPS_DB.exists():
        sys.exit("No opinion database at %s" % casedb.OPS_DB)
    con = sqlite3.connect("file:%s?mode=ro" % casedb.OPS_DB, uri=True)
    top = con.execute("SELECT max(rowid) FROM opinions").fetchone()[0]
    rnd = random.Random(args.seed)
    per, examples, shapes, skipped = [], defaultdict(list), Counter(), 0
    for rid in rnd.sample(range(1, top + 1), args.n):
        r = con.execute("SELECT cluster_id, field, text FROM opinions WHERE rowid = ?", (rid,)).fetchone()
        if not r or r[1] != "html_with_citations" or not r[2]:
            skipped += 1
            continue
        c, ex, sh = measure(zlib.decompress(r[2]).decode("utf-8", "replace"))
        per.append((r[0], c))
        shapes.update(sh)
        for k, v in ex.items():
            if len(examples[k]) < 200:
                examples[k].extend(v[:2])
    yrs = years_of({cid for cid, _ in per if cid})
    by = defaultdict(Counter)
    for cid, c in per:
        y = yrs.get(cid, 0)
        dec = "%ds" % (y // 10 * 10) if y else "unknown"
        for key in (dec, "ALL"):
            by[key]["opinions"] += 1
            by[key].update(c)
            by[key]["any_usc"] += bool(c["span_usc"] or c["out_usc"])
    print("%d opinions read, %d skipped (no html_with_citations)%s" % (len(per), skipped,
          "" if yrs else "; main database not found, so no decades"))
    print()
    hdr = ("decade",) + COLS + ("usc_in_span%", "opinions_w_usc%")
    print("".join("%-13s" % h if i == 0 else "%13s" % h for i, h in enumerate(hdr)))
    for key in sorted(k for k in by if k != "ALL") + ["ALL"]:
        b = by[key]
        usc = b["span_usc"] + b["out_usc"]
        cells = [key] + [str(b[k]) for k in COLS] + [
            "%.1f" % (100 * b["span_usc"] / usc) if usc else "-",
            "%.1f" % (100 * b["any_usc"] / b["opinions"]) if b["opinions"] else "-"]
        print("".join("%-13s" % x if i == 0 else "%13s" % x for i, x in enumerate(cells)))
    print()
    print("no-link spans that are not Code cites, commonest shapes:")
    for s, n in shapes.most_common(15):
        print("  %6d  %s" % (n, s))
    if args.examples:
        rnd2 = random.Random(args.seed + 1)
        for k in ("span_usc", "out_usc", "bare_sect", "pop_alone"):
            pool = examples.get(k, [])
            print("\n%s, %d of %d examples:" % (k, min(args.examples, len(pool)), len(pool)))
            for s in rnd2.sample(pool, min(args.examples, len(pool))):
                print("  ..." + s.strip() + "...")


def add_parser(sub):
    p = sub.add_parser("usc-sample", help="how US Code cites appear in opinion text (cocite_usc.py); writes nothing")
    p.add_argument("--n", type=int, default=2000, help="random opinions to read")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--examples", type=int, default=0, metavar="K", help="print K random examples of each kind")
    p.set_defaults(func=cmd_sample)
    return p


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    add_parser(sp)
    a = ap.parse_args(["usc-sample"] + sys.argv[1:])
    a.func(a)
