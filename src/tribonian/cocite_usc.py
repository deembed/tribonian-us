"""cocite_usc.py -- US Code provisions in the co-citation build (scope: docs/scope-cocite-usc.md).

    tribonian cocite usc-sample --n 2000             # step 1: how Code cites appear in the opinion text; writes nothing
    tribonian cocite usc-sample --n 2000 --examples 10
    tribonian cocite usc-extract --show 690840       # step 2: one opinion's Code references, with context
    tribonian cocite usc-extract --sample 2000       # rates by how/part, commonest sections and popular names
    tribonian cocite usc-map                         # step 3: old numbers -> current, from xml/ notes -> cocite-cache/usc/usc_map.json
    tribonian cocite usc-check --holdout 34,52,54    # step 4: map vs data (after the full run)
    tribonian cocite usc-profile                     # step 5: provisions per sub-cluster -> cocite-cache/usc/profile-<view>.sqlite
    tribonian cocite usc-profile --node 42:1983      #   which sub-clusters cite a provision; --sub 2010:17 the reverse
    tribonian cocite usc-citedby                     # step 6: most cited / newest appellate decisions per section -> cocite-cache/usc/citedby.sqlite
    tribonian cocite usc-extract                     # full run -> cocite-cache/usc/, sharded, resumable, all cores

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
import argparse, html, math, os, random, re, sqlite3, sys, time, zlib
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from tribonian import casedb, tconfig
from tribonian import cocite_positions as cp
from tribonian.cocite_positions import NO_LINK

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


# ================================================================ step 2: the extractor (positions' sidecar)
# Every Code reference in an opinion, in positions' coordinates (paragraph, sentence, character), one row each.
# The positions rows themselves are untouched: this runs extract() with its paragraph hook and scans the same
# text, so a v2 positions build stays reproducible. Resolution happens later (record once, compute later).
#
# Rows (cocite-cache/usc/<lo>-<hi>.npz, its own folder: positions' load() globs pos/):
#   op     citing opinion id
#   after  positions rows of this opinion before this reference (its place in the case-citation sequence)
#   para, sent, char, fn    as positions
#   how    HOW codes below
#   part   0 single section, 1 item of a list, 2 range start, 3 range end
#   ref    b"42:1983" (title:section, section lower-case, subsections dropped); b":1983" = short form, title unknown
# Popular names: p_row (index into the rows) and p_name (b"title vii|civil rights act"); their ref is b"".
USC_VERSION = 2                                 # 2: I.R.C. cites, default titles (Tax Court, "references are to"), "Sec." short forms
HOW = ["", "span", "regex", "short-resolved", "short-unresolved", "popular-paired", "popular-alone", "short-default"]
HOW_CODE = {h: i for i, h in enumerate(HOW)}
PART = ["single", "list", "range-start", "range-end"]

SECT = r"\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?"
SUBS = r"(?:\s?\([A-Za-z0-9]{1,4}\))*"
# title, U.S.C. (any spacing; .A/.S. annotated; an edition year; the old "Int.Rev.Code" form), marker, first section
FULL_RE = re.compile(r"\b(\d{1,2}[A-Z]?)\s*U\.?\s?S\.?\s?C\.?(?:\s?[AS]\.?)?"
                     r"(?:\s*\((?:19|20)\d\d[^)]{0,20}\))?"
                     r"(?:,?\s*Int\.?\s?Rev\.?\s?Code(?:\s+of\s+\d{4})?,?)?"
                     r"\s*(§§|§|[Ss]ections|[Ss]ecs?\.|[Ss]ection|)\s*(" + SECT + ")" + SUBS)
# the Internal Revenue Code cited by its own name: "I.R.C. § 162", "IRC sec. 6653(a)" -> Title 26 (group 1 empty)
IRC_RE = re.compile(r"\b()(?:I\.\s?R\.\s?C\.?|IRC)\s*(§§|§|[Ss]ections|[Ss]ecs?\.|[Ss]ection|)\s*(" + SECT + ")" + SUBS)
# an opinion's own statement of what bare section numbers mean -> its default title
DEFAULT_RE = re.compile(r"(?:section|statutory|§)\s+references\s+(?:herein\s+|in\s+this\s+opinion\s+)?are\s+to\s+(?:the\s+)?"
                        r"(Internal\s+Revenue\s+Code|Bankruptcy\s+Code|title\s+(\d+)|(\d+)\s+U\.?\s?S\.?\s?C)", re.I)
TAX_COURTS = ("tax", "bta")                      # United States Tax Court, Board of Tax Appeals: bare sections are I.R.C.
# list / range continuation after a first section: ", 1983", ", and 1985", "-76", "– 1376", " through 1376"
MORE_RE = re.compile(r"\s*(,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+|\s*[–—]\s*|-|\s+(?:through|to)\s+)(" + SECT + r")(?!\w|-\d|\.\d|\s*U\.?\s?S\.?\s?C)" + SUBS)
BARE_SECT_RE = re.compile(r"(?<![\w.§])(§§?)\s*(" + SECT + r")(?!\w|-\d|\.\d)" + SUBS + r"|\b(?:[Ss]ection|[Ss]ec\.)\s+(" + SECT + r")(?!\w|-\d|\.\d)" + SUBS + r"(?!\s+of\s+(?:the|this|title))")
NOT_SHORT = re.compile(r"(Stat\.|C\.\s?F\.\s?R\.?|Code|Ann\.|Rev\.|Laws|Gen\.|Comp\.|Pub\.\s?L\.|Rule|R\.|Const\.|art\.|"
                       r"Reg\.|Regs\.|ed\.\)?|OCGA|U\.\s?L\.\s?A\.|Restatement|Law|Article\s+[IVX\d]+,?|Amendment,?|Amend\.,?)[\s,()\d\w.–-]{0,14}$", re.I)
POP_RE = re.compile(r"\bTitle\s+(X{0,3}(?:IX|IV|V?I{0,3}))\b(?:\s+of\s+the\s+((?:[A-Z][\w'’.-]*\s+){0,8}?(?:Act|Code)|ADA|ADEA|ERISA|FHA))?"
                    r"|\b[Ss]ection\s+(\d+[A-Za-z]?(?:\([A-Za-z0-9]+\))*)\s+of\s+the\s+((?:[A-Z][\w'’.-]*\s+){1,8}?Act)\b")


def sect_norm(s):
    return s.lower()


def is_range_sep(sep, plural):
    s = sep.strip()
    return s in ("–", "—", "through", "to") or (s == "-" and plural)


def expand_end(start, end):
    """'1371', '76' -> '1376' (an abbreviated range end)"""
    if end.isdigit() and start.isdigit() and len(end) < len(start):
        return start[:len(start) - len(end)] + end
    return end


def split_hyphen(tok, plural, abbrev=True):
    """'2000e-2' is a section; '1371-76' after §§ (or, in a full cite, with a shortened end) is a range
    -> [(sect, part)]"""
    if "-" in tok:
        a, b = tok.split("-", 1)
        if a.isdigit() and b.isdigit() and (plural or (abbrev and len(b) < len(a))):
            return [(a, 2), (expand_end(a, b), 3)]
    return [(tok, 0)]


def full_refs(t, m):
    """A FULL_RE match in text t -> [(char, title, section, part)], end of the whole cite."""
    title, marker = (m.group(1) or "26").upper(), m.group(2)
    plural = marker in ("§§", "sections", "Sections") or marker.lower().startswith("secs")
    out = [(m.start(), title, s, p) for s, p in split_hyphen(m.group(3), plural)]
    end = m.end()
    prev = m.group(3)
    while plural:
        n = MORE_RE.match(t, end)
        if not n:
            break
        tok, sep = n.group(2), n.group(1)
        if is_range_sep(sep, plural) and "-" not in tok:
            out.append((n.start(2), title, expand_end(prev, tok), 3))
            if out[-2][3] == 0:
                out[-2] = out[-2][:3] + (2,)
        else:
            if len(out) == 1 and out[0][3] == 0:
                out[0] = out[0][:3] + (1,)
            out += [(n.start(2), title, s, 1 if p == 0 else p) for s, p in split_hyphen(tok, plural)]
        prev, end = tok, n.end()
    return out, end


def default_title(paras, tax_court=False):
    """the title an opinion's bare section numbers default to: its own "references are to" note, else 26 in a tax court"""
    for p in paras:
        m = DEFAULT_RE.search(p[4])
        if m:
            name = m.group(1).lower()
            return "26" if "revenue" in name else "11" if "bankruptcy" in name else (m.group(2) or m.group(3))
    return "26" if tax_court else None


def scan(op, paras, row_chars, default=None):
    """paras from cocite_positions.extract(paras=...) -> rows [(op, after, para, sent, char, fn, how, part, ref)], pop names"""
    found = []                                  # (char_abs, fn, para, sent, how, part, title, section, popname)

    def place(base, starts, s0, a):
        return base + a, s0 + int(np.searchsorted(starts, a, side="right")) - 1

    for fn, para, s0, base, txt, starts, cites in paras:
        taken = []                              # intervals already explained (case spans, full cites)
        for a, b, cid in cites:
            if cid == NO_LINK:
                m = FULL_RE.search(txt, a, b)
                if m and m.start() < b:
                    refs, end = full_refs(txt, m)
                    for c, ti, se, pa in refs:
                        ch, sn = place(base, starts, s0, c)
                        found.append((ch, fn, para, sn, HOW_CODE["span"], pa, ti, sect_norm(se), None))
                    taken.append((a, max(b, end)))
                    continue
            taken.append((a, b))

        def free(a, b):
            return not any(x < b and a < y for x, y in taken)
        for m in [*FULL_RE.finditer(txt), *IRC_RE.finditer(txt)]:
            if not free(m.start(), m.end()):
                continue
            refs, end = full_refs(txt, m)
            for c, ti, se, pa in refs:
                ch, sn = place(base, starts, s0, c)
                found.append((ch, fn, para, sn, HOW_CODE["regex"], pa, ti, sect_norm(se), None))
            taken.append((m.start(), end))
        for m in POP_RE.finditer(txt):
            if not free(m.start(), m.end()):
                continue
            if m.group(1) is not None:
                if not m.group(1):
                    continue
                name = "title " + m.group(1).lower() + ("|" + re.sub(r"\s+", " ", m.group(2)).lower() if m.group(2) else "")
            else:
                name = "section " + m.group(3).lower() + "|" + re.sub(r"\s+", " ", m.group(4)).lower()
            nxt = [x for x in starts if x > m.start()]           # paired: a Code cite later in the same sentence
            paired = bool(FULL_RE.search(txt, m.end(), nxt[0] if nxt else len(txt)))
            ch, sn = place(base, starts, s0, m.start())
            found.append((ch, fn, para, sn, HOW_CODE["popular-paired" if paired else "popular-alone"], 0, "", "", name))
            taken.append((m.start(), m.end()))
        for m in BARE_SECT_RE.finditer(txt):
            if not free(m.start(), m.end()) or NOT_SHORT.search(txt[max(0, m.start() - 40):m.start()]):
                continue
            tok = m.group(2) or m.group(3)
            plural = m.group(1) == "§§"
            for s, pa in split_hyphen(tok, plural, abbrev=False):
                ch, sn = place(base, starts, s0, m.start())
                found.append((ch, fn, para, sn, 0, pa, "", sect_norm(s), None))   # how decided below

    # short forms: the title of a full cite of the same section in this opinion, the latest before, else the first after
    full = defaultdict(list)
    for f in found:
        if f[4] in (HOW_CODE["span"], HOW_CODE["regex"]):
            full[f[7]].append((f[0], f[6]))
    rows, pop = [], []
    found.sort(key=lambda f: f[0])
    range_title = None
    for ch, fn, para, sn, how, pa, ti, se, name in found:
        if how == 0 and pa == 3 and range_title is not None:     # a short range's end takes its start's title
            ti, how = range_title, HOW_CODE["short-resolved"]
        if how == 0:
            cands = full.get(se)
            if cands:
                before = [t for c, t in cands if c < ch]
                ti, how = (before[-1] if before else cands[0][1]), HOW_CODE["short-resolved"]
            elif default:
                ti, how = default, HOW_CODE["short-default"]
            else:
                how = HOW_CODE["short-unresolved"]
        range_title = ti if (pa == 2 and how in (HOW_CODE["short-resolved"], HOW_CODE["short-default"])) else None
        after = int(np.searchsorted(row_chars, ch, side="left"))
        if name is not None:
            pop.append((len(rows), name))
        rows.append((op, after, para, sn, ch, fn, how, pa, ("%s:%s" % (ti, se)) if name is None else ""))
    return rows, pop


U_COLS = ("op", "after", "para", "sent", "char", "fn", "how", "part")
U_DTYPES = (np.int32, np.int32, np.int32, np.int32, np.int32, np.int16, np.int8, np.int8)


_TAX = None


def tax_clusters():
    """cluster ids of Tax Court / Board of Tax Appeals decisions. Built once by two sequential scans (a join seeks
    on the hard disk per row) and cached as cocite-cache/usc/tax_clusters.npy; workers only load the file."""
    global _TAX
    if _TAX is not None:
        return _TAX
    f = usc_dir() / "tax_clusters.npy"
    if not f.exists():
        if not casedb.DB.exists():
            sys.exit("No database at %s (needed once, for the Tax Court case list)" % casedb.DB)
        t0 = time.time()
        con = sqlite3.connect("file:%s?mode=ro" % casedb.DB, uri=True)
        dk = np.array([r[0] for r in con.execute("SELECT id FROM dockets WHERE court_id IN (%s)"
                                                  % ",".join("'%s'" % c for c in TAX_COURTS))], dtype=np.int64)
        out, cur = [], con.execute("SELECT id, coalesce(docket_id, -1) FROM clusters WHERE id IS NOT NULL")
        while True:
            rows = cur.fetchmany(2_000_000)
            if not rows:
                break
            a = np.array(rows, dtype=np.int64)
            out.append(a[np.isin(a[:, 1], dk), 0])
        ids = np.concatenate(out) if out else np.zeros(0, np.int64)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name("tax_clusters.tmp.npy")
        np.save(tmp, ids)
        tmp.replace(f)
        print("tax courts: %s dockets, %s decisions  (%.1f min)" % (format(len(dk), ","), format(len(ids), ","),
                                                                    (time.time() - t0) / 60), flush=True)
    _TAX = set(np.load(f).tolist())
    return _TAX


def extract_usc(oid, htm, own):
    paras = []
    prow, st, _ = cp.extract(oid, htm, own, paras=paras)
    dflt = default_title(paras, own in tax_clusters())
    rows, pop = scan(oid, paras, np.array([r[5] for r in prow], dtype=np.int64), dflt)
    return rows, pop, st, prow


def usc_dir(out=None):
    from tribonian import cocite
    return Path(out) if out else cocite.CACHE / "usc"


def usc_shard_done(path):
    if not path.exists():
        return False
    try:
        with np.load(path) as z:
            return int(z["version"]) == USC_VERSION
    except Exception:
        return False


def run_usc_shard(lo, hi, path):
    con = cp.ops_db()
    rows, pops, n = [], [], 0
    for oid, own, field, blob in con.execute(
            "SELECT id, cluster_id, field, text FROM opinions WHERE rowid BETWEEN ? AND ?", (lo, hi)):
        n += 1
        if field != "html_with_citations" or not blob:
            continue
        try:
            r, p, _, _ = extract_usc(oid, zlib.decompress(blob).decode("utf-8", "replace"), own)
        except Exception as e:
            print("  opinion %d skipped: %s: %s" % (oid, type(e).__name__, e), file=sys.stderr, flush=True)
            continue
        pops += [(len(rows) + i, name) for i, name in p]
        rows += r
    tmp = path.with_name(path.name + ".tmp.npz")
    cols = {c: np.array([r[i] for r in rows], dtype=d) for i, (c, d) in enumerate(zip(U_COLS, U_DTYPES))}
    np.savez(tmp, version=np.int32(USC_VERSION), ref=np.array([r[8].encode()[:24] for r in rows], dtype="S24"),
             p_row=np.array([i for i, _ in pops], dtype=np.int32),
             p_name=np.array([nm.encode()[:80] for _, nm in pops], dtype="S80"), **cols)
    tmp.replace(path)
    return n, len(rows)


def cmd_usc_show(oid):
    con = cp.ops_db()
    r = con.execute("SELECT id, cluster_id, field, text FROM opinions WHERE id = ?", (oid,)).fetchone()
    if not r or not r[3]:
        sys.exit("no text for opinion %d" % oid)
    rows, pop, st, prow = extract_usc(r[0], zlib.decompress(r[3]).decode("utf-8", "replace"), r[1])
    text, names = st["text"], dict(pop)
    print("opinion %d (case %d): %d positions rows, %d Code references" % (r[0], r[1], len(prow), len(rows)))
    print("%-5s %4s %4s %6s %3s %-16s %-11s %-14s  context" % ("after", "para", "sent", "char", "fn", "how", "part", "ref"))
    for i, x in enumerate(rows):
        ctx = text[max(0, x[4] - 40):x[4]].replace("\n", "¶") + "⟦" + text[x[4]:x[4] + 40].replace("\n", "¶")
        print("%-5d %4d %4d %6d %3d %-16s %-11s %-14s  %s" % (x[1], x[2], x[3], x[4], x[5], HOW[x[6]], PART[x[7]],
              x[8] or names.get(i, "")[:14], ctx))


def cmd_usc_sample(n, seed=1):
    con = cp.ops_db()
    top = con.execute("SELECT max(rowid) FROM opinions").fetchone()[0]
    rnd = random.Random(seed)
    how, part, refs, names, ops, t = Counter(), Counter(), Counter(), Counter(), 0, 0.0
    for rid in rnd.sample(range(1, top + 1), n):
        r = con.execute("SELECT id, cluster_id, field, text FROM opinions WHERE rowid = ?", (rid,)).fetchone()
        if not r or r[2] != "html_with_citations" or not r[3]:
            continue
        ops += 1
        t0 = time.perf_counter()
        rows, pop, _, _ = extract_usc(r[0], zlib.decompress(r[3]).decode("utf-8", "replace"), r[1])
        t += time.perf_counter() - t0
        for x in rows:
            how[HOW[x[6]]] += 1
            part[PART[x[7]]] += 1
            if x[8] and not x[8].startswith(":"):
                refs[x[8]] += 1
        names.update(nm for _, nm in pop)
    print("%d opinions, %.2f ms each (positions + this)" % (ops, 1000 * t / max(1, ops)))
    print("how:", dict(how))
    print("part:", dict(part))
    print("commonest sections:", ", ".join("%s %d" % kv for kv in refs.most_common(15)))
    print("commonest popular names:", ", ".join("%s %d" % kv for kv in names.most_common(15)))


def cmd_usc_extract(args):
    if args.show:
        return cmd_usc_show(args.show)
    if args.sample:
        return cmd_usc_sample(args.sample)
    d = usc_dir(args.out)
    d.mkdir(parents=True, exist_ok=True)
    jobs = cp.plan(d, args.shard_size)
    todo = [j for j in jobs if not usc_shard_done(j[2])]
    print("%d shards in %s: %d done, %d to do (usc extractor version %d)"
          % (len(jobs), d, len(jobs) - len(todo), len(todo), USC_VERSION), flush=True)
    if args.status or not todo:
        return
    if args.limit:
        todo = todo[:args.limit]
    tax_clusters()                                  # build the cached list once, before the workers start
    t0, n_ops, n_rows = time.time(), 0, 0
    with ProcessPoolExecutor(args.workers or os.cpu_count() or 1) as pool:
        futs = {pool.submit(run_usc_shard, lo, hi, path): path for lo, hi, path in todo}
        for i, f in enumerate(as_completed(futs), 1):
            o, r = f.result()
            n_ops += o
            n_rows += r
            el = time.time() - t0
            print("  %d/%d %s: %s opinions, %s references  (%.0f opinions/s)"
                  % (i, len(todo), futs[f].name, format(o, ","), format(r, ","), n_ops / el), flush=True)
    print("done: %s opinions, %s references" % (format(n_ops, ","), format(n_rows, ",")))



# ================================================================ step 3: the identity map (usc-map)
# Old Code number -> current number, from the notes in xml/*.xml (the current release point):
#   stub    a status="transferred" section: "Section N was editorially reclassified as / transferred to <ref .../tT/sM>"
#   based   Historical and Revision Notes: "Based on title 18, U.S.C., 1940 ed., § 80" (the 1948 titles and others)
#   table   positive-law revision tables: "Revised Section | Source (U.S. Code)", cells like "49:1651(a)."
# An edge (old, new, year, source) maps a citation of old forward. When old is ALSO a live section today (the number
# was reused, e.g. old 49 § 1651 -> 49 § 101 in 1994), the edge applies only to decisions filed before its year.
# Edge years: a stub's latest date in its note, else the reclassification year of its target title; based/table:
# the year of the section's first source credit (the revising act).
MAP_VERSION = 2                                 # 2: range stubs (s11601...11610) read paragraph by paragraph
RECLASS_YEAR = {"34": 2017, "52": 2014, "54": 2014}        # editorial reclassifications with no date in the stub note
MOVE_RE = re.compile(r"(?:reclassified|transferred|renumbered|redesignated)\s+(?:as|to)\s*(?:<[^>]+>\s*)*?"
                     r"(?:<ref\s+href=\"/us/usc/t(\w+)/s([^\"/]+)\")", re.S)
BASED_RE = re.compile(r"title\s+(\d+[A-Za-z]?),\s+U\.\s?S\.\s?C\.,\s+(\d{4})\s+ed\.,\s+(§§?)\s*([^;]*?)(?=\s\(|\.\s|\.$|;|$)")
BASED2_RE = re.compile(r"[Ss]ections?\s+([\d\w,\s]+?)\s+of\s+title\s+(\d+[A-Za-z]?),\s+U\.\s?S\.\s?C\.,\s+(\d{4})\s+ed\.")
CELL_RE = re.compile(r"(?:(\d+[A-Za-z]?)(\s*App\.)?:)?\s*(\d+[A-Za-z]*(?:[-–]\d+[A-Za-z]*)?)((?:\([^)]*\))*)(\s*\(note[^)]*\))?")
NUM_RE = re.compile(r"\d+[A-Za-z]*(?:[-–]\d+[A-Za-z]*)?")


def ukey(title, sec):
    return "%s:%s" % (title.upper().lstrip("0") or "0", sec.replace("–", "-").replace("‑", "-").lower())


def id_keys(ident):
    """'/us/usc/t42/s1973 /us/usc/t42/s1973a' -> ['42:1973', '42:1973a']"""
    out = []
    for part in ident.split():
        m = re.fullmatch(r"/us/usc/t(\w+?)/s([^/]+)", part)
        if m:
            out.append(ukey(m.group(1), m.group(2)))
    return out


def flat(xml):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", xml))).replace(" ", " ").strip()


def first_year(xml):
    m = re.search(r"<sourceCredit\b.*?</sourceCredit>", xml, re.S)
    y = re.search(r'/us/act/(\d{4})|date="(\d{4})', m.group(0)) if m else None
    return int(y.group(1) or y.group(2)) if y else 0


def sections_of(path):
    """top-level <section> elements of a title file, skipping quoted ones (amendment text inside notes)"""
    s = open(path, encoding="utf-8").read()
    s = re.sub(r"<quotedContent\b.*?</quotedContent>", "", s, flags=re.S)
    for m in re.finditer(r"<section\b([^>]*)>(.*?)</section>", s, re.S):
        attrs = m.group(1)
        ident = re.search(r'identifier="([^"]+)"', attrs)
        if not ident or not ident.group(1).startswith("/us/usc/"):
            continue
        st = re.search(r'status="(\w+)"', attrs)
        yield id_keys(ident.group(1)), (st.group(1) if st else ""), m.group(2)


def build_map(xml_dir):
    live, edges, stats = set(), [], Counter()
    for f in sorted(Path(xml_dir).glob("usc*.xml")):
        for keys, status, body in sections_of(f):
            if not keys:
                continue
            title = keys[0].split(":")[0]
            if status == "":
                live.update(keys)
            if status == "transferred":
                # one move per paragraph: "Section 11601, ..., was editorially reclassified as section 9001 of Title 22."
                # A range stub (identifier s11601...11610) names each section in its own paragraph; a single stub's
                # paragraph may say just "Section, Pub. L. ..." (this section).
                stats["stubs"] += 1
                found = 0
                for para in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S) or [body]:
                    targets = [ukey(t, s) for t, s in MOVE_RE.findall(para)]
                    if not targets:
                        continue
                    if len(keys) == 1 and "..." not in keys[0]:
                        olds = keys                         # a single stub: its identifier is authoritative
                    else:                                   # a range stub: the paragraph names the section
                        # letters may be set in their own tag ("1973<i>l</i>"), which flattening spaces apart
                        num = re.match(r"\s*Sections?\s+(\d+)\s?([A-Za-z]{0,4})((?:\s?[-–]\s?\d+[A-Za-z]*)?)\b", flat(para))
                        olds = [ukey(title, num.group(1) + num.group(2) + num.group(3).replace(" ", ""))] if num else []
                    mv = MOVE_RE.search(para)              # the moving act's date comes after the verb
                    dates = [int(y) for y in re.findall(r'date="(\d{4})', para[mv.start():])]
                    for old in olds:
                        for new in dict.fromkeys(targets):
                            if new != old:
                                yr = min(dates) if dates else RECLASS_YEAR.get(new.split(":")[0], 0)
                                edges.append((old, new, yr, "stub"))
                                stats["stub edges"] += 1
                                found += 1
                if not found:
                    stats["stubs without a target"] += 1
                continue
            if status:
                continue
            yr = first_year(body)
            for note in re.findall(r'<note\b[^>]*topic="historicalAndRevision"[^>]*>(.*?)</note>', body, re.S):
                txt = flat(note)
                olds = []
                for m in BASED_RE.finditer(txt):
                    olds += [(m.group(1), n, int(m.group(2))) for n in NUM_RE.findall(re.sub(r"\([^)]*\)", "", m.group(4)))]
                for m in BASED2_RE.finditer(txt):
                    olds += [(m.group(2), n, int(m.group(3))) for n in NUM_RE.findall(m.group(1))]
                for t, n, ed in olds:
                    o = ukey(t, n)
                    for new in keys:
                        if o != new:
                            edges.append((o, new, yr, "based"))
                            stats["based edges"] += 1
                if "Source (U.S. Code)" not in note:
                    continue
                for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", note, re.S):
                    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
                    if len(cells) < 2:
                        continue
                    cur, app = None, False
                    for m in CELL_RE.finditer(flat(cells[1])):
                        if m.group(1):
                            cur, app = m.group(1), bool(m.group(2))
                        if cur is None or m.group(5) or "note" in (m.group(4) or ""):
                            continue                # "49:101 (note)": the source is a note, not the section
                        o = ukey(cur + ("app" if app else ""), m.group(3))
                        for new in keys:
                            if o != new:
                                edges.append((o, new, yr, "table"))
                                stats["table edges"] += 1
    edges = sorted(set(edges))
    stats["edges (distinct)"] = len(edges)
    stats["live sections"] = len(live)
    stats["old numbers also live today (reused)"] = len({e[0] for e in edges} & live)
    return {"version": MAP_VERSION, "live": sorted(live), "edges": edges, "stats": dict(stats)}


class UscMap:
    """Separate nodes, linked over time (decided 2026-10-15). A citation keeps the number it was cited by; nothing is
    rewritten. Node id = that number, plus an era when the number was reused for a different provision: old
    49 § 1651 cited before its 1983 move is node "49:1651@-1983", today's 49 § 1651 is "49:1651". The map's edges become
    lineage links between nodes (links()), the way cluster lineages link clusters across slices; a split (28 § 41,
    1940 ed., into 30 sections) is one node with 30 links, and nothing is spread across the successors."""

    def __init__(self, m):
        self.live = set(m["live"])
        self.out = defaultdict(list)
        for old, new, yr, src in m["edges"]:
            self.out[old].append((new, yr, src))
        # a live number with dated moves out of it was reused: its eras end at those years
        self.ends = {k: sorted({y for n, y, s in v if y}) for k, v in self.out.items() if k in self.live}

    def node(self, key, year=0):
        """the node a citation of key in a decision filed in year belongs to (year 0 = unknown: the current era)"""
        for y in self.ends.get(key, ()):
            if year and year < y:
                return "%s@-%d" % (key, y)
        return key

    def links(self):
        """[(from node, to node, year, source)]: a move out of key in year y leaves key's era ending at y
        (or key itself when the number was not reused) for the node its target had in y"""
        out = []
        for old, lst in self.out.items():
            for new, y, s in lst:
                src = "%s@-%d" % (old, y) if (old in self.live and y) else old
                out.append((src, self.node(new, y) if y else new, y, s))
        return out

    def _by(self):
        if not hasattr(self, "_adj"):
            self._adj = defaultdict(list)
            for a, b, y, s in self.links():
                self._adj[a].append((b, y, s))
        return self._adj

    def resolve(self, key, year=0):
        """where a citation of key in a decision of year points today: its node, followed through the lineage links
        to the end -> current numbers (several after a split; [key] when it never moved). For marking cites
        (usc_status.py); co-citation itself keeps the cited node and never rewrites it."""
        by, start = self._by(), self.node(key, year)
        if not by.get(start):
            return [key]
        out, todo, seen = [], [start], {start}
        while todo:
            n = todo.pop(0)
            nxt = by.get(n, ())
            if not nxt and n != start:
                k = n.partition("@-")[0]
                if k not in out:
                    out.append(k)
            for b, y, s in nxt:
                if b not in seen:
                    seen.add(b)
                    todo.append(b)
        return out or [key]

    def successors(self, node, depth=6):
        """chains of links from a node -> [(node, year, source, depth)], for timelines"""
        by = self._by()
        out, todo, seen = [], [(node, 0)], {node}
        while todo:
            n, d = todo.pop(0)
            if d >= depth:
                continue
            for b, y, s in by.get(n, ()):
                if b not in seen:
                    seen.add(b)
                    out.append((b, y, s, d + 1))
                    todo.append((b, d + 1))
        return out


def map_path():
    return usc_dir() / "usc_map.json"


def load_map():
    import json
    return UscMap(json.loads(map_path().read_text()))


CHECKS = [("42:1973", 2010, "52:10301"), ("42:11603", 2010, "22:9003"), ("18:80", 1940, "18:1001"),
          ("49:1651", 1980, "49:101"), ("28:41", 1945, "28:1331"), ("16:1", 2000, None)]


def cmd_usc_map(args):
    import json
    xml_dir = Path(args.xml or tconfig.WORK / "xml")
    m = build_map(xml_dir)
    if not args.dry:
        p = map_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(m))
        tmp.replace(p)
        print("wrote %s" % p)
    for k, v in m["stats"].items():
        print("  %-40s %s" % (k, format(v, ",")))
    um = UscMap(m)
    links = um.links()
    print("  %-40s %s" % ("lineage links", format(len(links), ",")))
    print("  %-40s %s" % ("numbers with reuse eras", format(len(um.ends), ",")))
    print("spot checks (cited number, year -> node; successors):")
    for key, yr, want in CHECKS:
        n = um.node(key, yr)
        nxt = [b for b, y, s, d in um.successors(n)]
        print("  %-10s %d -> %-16s -> %-40s %s" % (key, yr, n, ", ".join(nxt[:4]) + (" +%d" % (len(nxt) - 4) if len(nxt) > 4 else ""),
              "" if not want else ("ok" if want in nxt else "MISSING")))
    for key in args.key or []:
        k, _, y = key.partition("@")
        n = um.node(k, int(y or 0))
        print("  %s -> node %s; successors: %s" % (key, n, um.successors(n)[:12]))



# ================================================================ step 4: the map against the data (usc-check)
# Partners of a statute in a set of decisions = the cases (cocite-cache citing/cited) and other Code sections those
# decisions cite, weighted tf-idf (count x log(N / document frequency)) so hubs (Celotex, 28 U.S.C. § 1291) count
# little. Two statutes that are one provision under two numbers should have the same partners on either side of the
# move: cosine(partners of A before the move, partners of B after it).
TITLED = (HOW_CODE["span"], HOW_CODE["regex"], HOW_CODE["short-resolved"], HOW_CODE["short-default"])


def op2ref():
    """opinion id -> cluster id, cached as cocite-cache/usc/op2ref.npy (prep's pairing of the two indexes)"""
    from tribonian import cocite
    f = usc_dir() / "op2ref.npy"
    if f.exists():
        return np.load(f, mmap_mode="r")
    t0 = time.time()
    ops = cp.ops_db()
    a = cocite.fetch_array(ops.execute("SELECT rowid, id FROM opinions INDEXED BY opinions_by_id"), 2)
    b = cocite.fetch_array(ops.execute("SELECT rowid, coalesce(cluster_id, -1) FROM opinions INDEXED BY opinions_by_cluster"), 2)
    a, b = a[np.argsort(a[:, 0], kind="stable")], b[np.argsort(b[:, 0], kind="stable")]
    if len(a) != len(b) or not np.array_equal(a[:, 0], b[:, 0]):
        sys.exit("the two opinion indexes disagree; is opinions.sqlite complete?")
    m = np.full(int(a[:, 1].max()) + 1, -1, np.int32)
    m[a[:, 1]] = b[:, 1]
    tmp = f.with_name("op2ref.tmp.npy")
    np.save(tmp, m)
    tmp.replace(f)
    print("opinions -> cases: %s  (%.1f min)" % (format(len(a), ","), (time.time() - t0) / 60), flush=True)
    return m


def load_refs(courts=None):
    """all usc shards -> decision, key id, year arrays (one row per decision x section), key names"""
    from tribonian import cocite
    o2r = op2ref()
    ry = np.load(cocite.CACHE / "ref_year.npy", mmap_mode="r")
    lvl = np.load(cocite.CACHE / "level.npy", mmap_mode="r")
    keys, kid, D, K, n_sh = [], {}, [], [], 0
    for f in sorted(usc_dir().glob("*-*.npz")):
        with np.load(f) as z:
            if int(z["version"]) != USC_VERSION:
                continue
            n_sh += 1
            ok = np.isin(z["how"], TITLED)
            op, ref = z["op"][ok], z["ref"][ok]
        u, inv = np.unique(ref, return_inverse=True)
        gid = np.array([kid.setdefault(k.decode(), len(kid)) for k in u.tolist()], dtype=np.int32)
        dec = np.where(op < len(o2r), o2r[np.minimum(op, len(o2r) - 1)], -1)
        good = dec >= 0
        D.append(dec[good].astype(np.int32))
        K.append(gid[inv][good])
    keys = [None] * len(kid)
    for k, i in kid.items():
        keys[i] = k
    dec, key = np.concatenate(D), np.concatenate(K)
    pair = np.unique((dec.astype(np.int64) << 32) | key.astype(np.int64))
    dec, key = (pair >> 32).astype(np.int32), (pair & 0xFFFFFFFF).astype(np.int32)
    ok = dec < len(ry)
    dec, key = dec[ok], key[ok]
    yr = np.asarray(ry)[dec].astype(np.int32)
    keep = yr > 0
    if courts:
        keep &= np.isin(np.asarray(lvl)[dec], courts)
    print("%d shards: %s decision x section rows, %s sections" % (n_sh, format(int(keep.sum()), ","), format(len(keys), ",")),
          flush=True)
    return dec[keep], key[keep], yr[keep], keys


def _ranges(a, b):
    """concatenated aranges a[i]..b[i]"""
    n = b - a
    if n.sum() == 0:
        return np.zeros(0, np.int64)
    rep = np.repeat(np.arange(len(a)), n)
    off = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    return a[rep] + off, rep


class Partners:
    """Profiles as sparse rows: (statute, decision set) x partners (cases, then Code sections), tf-idf weighted."""

    def __init__(self, dec, key, yr, nkeys):
        from tribonian import cocite
        self.ci = np.asarray(np.load(cocite.CACHE / "citing.npy", mmap_mode="r"))
        self.cd = np.asarray(np.load(cocite.CACHE / "cited.npy", mmap_mode="r"))
        self.dec, self.key, self.yr, self.nkeys = dec, key, yr, nkeys
        o = np.argsort(key, kind="stable")
        self.by_key = o
        self.kstart = np.searchsorted(key[o], np.arange(nkeys + 1))
        o2 = np.argsort(dec, kind="stable")
        self.d_sorted, self.k_by_dec = dec[o2], key[o2]
        self.ncase = int(self.cd.max()) + 1
        n_all = len(np.unique(self.ci))
        n_dec = max(len(np.unique(dec)), 1)
        self.idf = np.concatenate([np.log(n_all / np.maximum(np.bincount(self.cd, minlength=self.ncase), 1)),
                                   np.log(n_dec / np.maximum(np.bincount(key, minlength=nkeys), 1))])

    def decisions(self, k, lo, hi):
        rows = self.by_key[self.kstart[k]:self.kstart[k + 1]]
        y = self.yr[rows]
        return np.unique(self.dec[rows][(y >= lo) & (y < hi)])

    def matrix(self, groups):
        """groups: [(key, lo, hi)] -> L2-normalised sparse rows of partners; the row's own key column zeroed"""
        from scipy import sparse
        R, C = [], []
        for i, (k, lo, hi) in enumerate(groups):
            d = self.decisions(k, lo, hi)
            R.append(np.full(len(d), i))
            C.append(d)
        if not groups:
            return sparse.csr_matrix((0, self.ncase + self.nkeys))
        R, C = np.concatenate(R), np.concatenate(C)
        ud, cinv = np.unique(C, return_inverse=True)
        G = sparse.csr_matrix((np.ones(len(R)), (R, cinv)), shape=(len(groups), len(ud)))
        idx, rep = _ranges(np.searchsorted(self.ci, ud, "left"), np.searchsorted(self.ci, ud, "right"))
        idx2, rep2 = _ranges(np.searchsorted(self.d_sorted, ud, "left"), np.searchsorted(self.d_sorted, ud, "right"))
        rows = np.concatenate([rep, rep2])
        cols = np.concatenate([self.cd[idx].astype(np.int64), self.ncase + self.k_by_dec[idx2].astype(np.int64)])
        M = sparse.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(len(ud), self.ncase + self.nkeys))
        F = (G @ M).tocsr()
        own = np.array([self.ncase + k for k, lo, hi in groups])
        vals = np.asarray(F[np.arange(len(groups)), own]).ravel()
        F = (F - sparse.csr_matrix((vals, (np.arange(len(groups)), own)), shape=F.shape)).tocsr()
        F.eliminate_zeros()
        F = F.multiply(self.idf[np.newaxis, :]).tocsr()
        norm = np.sqrt(np.asarray(F.multiply(F).sum(axis=1)).ravel())
        norm[norm == 0] = 1
        return sparse.diags(1 / norm) @ F


def rowcos(A, B):
    """cosine of row i of A with row i of B (rows already normalised)"""
    return np.asarray(A.multiply(B).sum(axis=1)).ravel()


def cmd_usc_check(args):
    import json
    from tribonian import cocite
    courts, tag = cocite.parse_courts(args.courts)
    um = load_map()
    dec, key, yr, keys = load_refs(courts)
    kid = {k: i for i, k in enumerate(keys)}
    P = Partners(dec, key, yr, len(keys))
    rnd = random.Random(args.seed)
    W, MIN = args.window, args.min
    out_dir = usc_dir()

    # ---- 1. known edges: partners of A in the window before the move vs B in the window after
    hold = set(args.holdout.split(",")) if args.holdout else set()
    edges = [(o, n, y, s) for o, lst in um.out.items() for n, y, s in lst]
    testable = [(o, n, y, s) for o, n, y, s in edges if y and o in kid and n in kid]
    rnd.shuffle(testable)
    sel = [e for e in testable if len(P.decisions(kid[e[0]], e[2] - W, e[2])) >= MIN
           and len(P.decisions(kid[e[1]], e[2], e[2] + W)) >= MIN][:args.max_edges]
    A = P.matrix([(kid[o], y - W, y) for o, n, y, s in sel])
    B = P.matrix([(kid[n], y, y + W) for o, n, y, s in sel])
    real = rowcos(A, B)
    perm = np.array([rnd.randrange(len(sel)) for _ in sel]) if sel else np.zeros(0, int)
    null = rowcos(A, B[perm]) if sel else np.zeros(0)
    null = null[perm != np.arange(len(sel))]
    rows = sorted(zip(real.tolist(), [e[0] for e in sel], [e[1] for e in sel], [e[2] for e in sel], [e[3] for e in sel]))
    q = lambda v, f: float(np.quantile(v, f)) if len(v) else float("nan")
    print("\n1. known moves with >= %d citing decisions on both sides (window %d years): %d of %d dated edges"
          % (MIN, W, len(sel), len(testable)))
    print("   cosine   real: median %.2f  (10%% %.2f, 90%% %.2f)   null (random target): median %.2f  (90%% %.2f)"
          % (q(real, .5), q(real, .1), q(real, .9), q(null, .5), q(null, .9)))
    cut = q(null, .9)
    for src in ("stub", "based", "table"):
        v = [r[0] for r in rows if r[4] == src]
        if v:
            print("   %-6s %4d edges, median %.2f, below the null's 90%%: %d" % (src, len(v), q(v, .5), sum(x < cut for x in v)))
    print("   lowest (wrong edge, reused number, or a provision that changed with the move):")
    for c, o, n, y, s in rows[:args.show]:
        print("     %.2f  %-12s -> %-12s %d %s" % (c, o, n, y, s))

    # ---- 2. moves the data suggests: A falls while B rises across a slice transition, same partners
    known = {(o, n) for o, n, y, s in edges if n.split(":")[0] not in hold}
    held = {(o, n) for o, n, y, s in edges if n.split(":")[0] in hold}
    sl = (yr // 5) * 5
    cnt = {}
    for s in range(args.first, args.last + 6, 5):
        u, c = np.unique(key[sl == s], return_counts=True)
        cnt[s] = dict(zip(u.tolist(), c.tolist()))
    cands, detectable = [], set()
    for s in range(args.first, args.last + 1, 5):
        t = s + 5
        risers = [k for k, c in cnt[t].items() if c >= MIN and cnt[s].get(k, 0) <= args.rise * c]
        fallers = [k for k, c in cnt[s].items() if c >= MIN and cnt[t].get(k, 0) < c]
        rs, fs = set(risers), set(fallers)
        for o, n in held:
            if o in kid and n in kid and kid[n] in rs and kid[o] in fs:
                detectable.add((o, n))
        if not risers or not fallers:
            continue
        F = P.matrix([(k, s, s + 5) for k in fallers])
        Rm = P.matrix([(k, t, t + 5) for k in risers])
        S = (Rm @ F.T).toarray()                                  # risers x fallers
        for i, b in enumerate(risers):
            for j in np.argsort(-S[i])[:3]:
                if S[i, j] >= args.min_cos:
                    a = fallers[j]
                    cands.append((float(S[i, j]), keys[a], keys[b], t, cnt[s][a], cnt[t][b]))
        print("   %d-%02d -> %d-%02d: %d falling, %d rising" % (s, (s + 4) % 100, t, (t + 4) % 100, len(fallers), len(risers)),
              flush=True)
    cands.sort(reverse=True)
    found = {(o, n) for c, o, n, *_ in cands}
    print("\n2. candidate moves (a section falling while another rises, cosine >= %.2f): %d" % (args.min_cos, len(cands)))
    print("   already in the map: %d; not in the map: %d" % (sum(f in known for f in found),
                                                           sum(f not in known and f not in held for f in found)))
    if hold:
        hit = detectable & found
        print("   held-out titles %s: %d held-out moves detectable (both sides cited, A falling, B rising), "
              "%d found = recall %.0f%%" % (",".join(sorted(hold)), len(detectable), len(hit),
                                            100 * len(hit) / max(1, len(detectable))))
    print("   strongest not in the map (check by hand; never applied automatically):")
    shown = 0
    for c, o, n, t, na, nb in cands:
        if (o, n) in known or (o, n) in held:
            continue
        print("     %.2f  %-12s -> %-12s %d-%02d  (%d decisions before, %d after)" % (c, o, n, t, (t + 4) % 100, na, nb))
        shown += 1
        if shown >= args.show:
            break
    rows = [list(r) for r in rows]
    null = null.tolist()
    p = out_dir / ("usc-check-%s.json" % tag)
    p.write_text(json.dumps({"edges": rows, "null": null, "candidates": cands}))
    print("\nwrote %s" % p)



# ================================================================ step 5: statute profile on the shared clusters
# Per slice of a results view: every citing decision is assigned to its home sub-cluster (the one most of its cited
# cases fall in, as the case notes' "who cites it" does), then the Code nodes those decisions cite are counted.
# Row (slice, sub, node): n decisions, share = n / the sub's decisions, lift = share / the node's share of all
# assigned decisions in the slice. Nodes are UscMap.node(): the number as cited, with a reuse era when needed.
PROFILE_VERSION = 1


def profile_path(view):
    return usc_dir() / ("profile-%s.sqlite" % view)


def home_subs(ci, cd, edge_year, lvl, sub_of, s, codes):
    """decisions of slice s (citing courts in codes) -> (decision ids sorted, home sub of each)"""
    m = (edge_year >= s) & (edge_year < s + 5)
    D, R = ci[m], cd[m]
    if codes:
        k = np.isin(lvl[D], codes)
        D, R = D[k], R[k]
    S = sub_of[np.minimum(R, len(sub_of) - 1)]
    S[R >= len(sub_of)] = -1
    k = S >= 0
    D, S = D[k].astype(np.int64), S[k].astype(np.int64)
    if not len(D):
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    u, c = np.unique((D << 24) | S, return_counts=True)
    d, sb = u >> 24, u & 0xFFFFFF
    o = np.lexsort((-c, d))                          # per decision, the sub with most cites first (ties: lowest id)
    d, sb = d[o], sb[o]
    first = np.ones(len(d), bool)
    first[1:] = d[1:] != d[:-1]
    return d[first], sb[first]


def cmd_usc_profile(args):
    from tribonian import cocite
    view = args.view
    if args.node or args.sub:
        return profile_query(args)
    db = cocite.open_view(view)
    if db is None:
        sys.exit("no results view %s: tribonian cocite build first" % view)
    codes, _ = cocite.parse_courts(view)
    um = load_map()
    dec, key, yr, keys = load_refs(codes)
    t0 = time.time()
    ci = np.asarray(np.load(cocite.CACHE / "citing.npy", mmap_mode="r"))
    cd = np.asarray(np.load(cocite.CACHE / "cited.npy", mmap_mode="r"))
    ey = np.asarray(np.load(cocite.CACHE / "year.npy", mmap_mode="r"))
    lvl = np.asarray(np.load(cocite.CACHE / "level.npy", mmap_mode="r"))
    import shutil, tempfile
    p = profile_path(view)
    tmpdir = Path(tempfile.mkdtemp(prefix="usc-profile-"))    # built locally, moved into place when complete
    tmp = tmpdir / p.name
    out = sqlite3.connect(tmp)
    out.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE subn(slice INTEGER, sub INTEGER, decisions INTEGER, PRIMARY KEY(slice, sub));
        CREATE TABLE noden(slice INTEGER, node TEXT, decisions INTEGER, PRIMARY KEY(slice, node));
        CREATE TABLE prof(slice INTEGER, sub INTEGER, node TEXT, n INTEGER, share REAL, lift REAL);
    """)
    node_cache = {}

    def node(k, y):
        t = (k, y)
        if t not in node_cache:
            node_cache[t] = um.node(keys[k], int(y))
        return node_cache[t]

    for (s,) in db.execute("SELECT slice FROM slices ORDER BY slice").fetchall():
        nodes = np.array(db.execute("SELECT ref, sub FROM nodes WHERE slice=? AND sub > 0", (s,)).fetchall(), dtype=np.int64)
        if not len(nodes):
            continue
        sub_of = np.full(int(nodes[:, 0].max()) + 1, -1, np.int64)
        sub_of[nodes[:, 0]] = nodes[:, 1]
        dd, ds = home_subs(ci, cd, ey, lvl, sub_of, s, codes)
        if not len(dd):
            continue
        n_all = len(dd)
        subn = Counter(ds.tolist())
        m = (yr >= s) & (yr < s + 5)
        D, K, Y = dec[m].astype(np.int64), key[m], yr[m]
        i = np.searchsorted(dd, D)
        ok = (i < len(dd)) & (dd[np.minimum(i, len(dd) - 1)] == D)
        D, K, Y, H = D[ok], K[ok], Y[ok], ds[i[ok]]
        pairs = {(int(d), node(int(k), int(y)), int(h)) for d, k, y, h in zip(D.tolist(), K.tolist(), Y.tolist(), H.tolist())}
        by_node, by_sub_node = Counter(), Counter()
        for d, nd, h in pairs:
            by_node[nd] += 1
            by_sub_node[(h, nd)] += 1
        out.executemany("INSERT INTO subn VALUES (?,?,?)", [(s, b, n) for b, n in subn.items()])
        out.executemany("INSERT INTO noden VALUES (?,?,?)", [(s, nd, n) for nd, n in by_node.items()])
        rows = []
        for (h, nd), n in by_sub_node.items():
            if n >= args.min_n:
                share = n / subn[h]
                rows.append((s, h, nd, n, share, share / (by_node[nd] / n_all)))
        out.executemany("INSERT INTO prof VALUES (?,?,?,?,?,?)", rows)
        print("  %d-%02d: %s decisions in %d sub-clusters, %s cite the Code; %s rows"
              % (s, (s + 4) % 100, format(n_all, ","), len(subn), format(len({d for d, _, _ in pairs}), ","),
                 format(len(rows), ",")), flush=True)
    out.executescript("""
        CREATE INDEX prof_by_sub ON prof(slice, sub);
        CREATE INDEX prof_by_node ON prof(node, slice);
    """)
    out.executemany("INSERT INTO meta VALUES (?,?)", [
        ("version", str(PROFILE_VERSION)), ("view", view), ("usc_version", str(USC_VERSION)),
        ("map_version", str(MAP_VERSION)), ("results_built", str(db.execute("SELECT max(built) FROM slices").fetchone()[0])),
        ("built", time.strftime("%Y-%m-%d %H:%M"))])
    out.commit()
    out.close()
    part = p.with_name(p.name + ".part")
    shutil.copyfile(tmp, part)
    part.replace(p)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("wrote %s  (%.1f min)" % (p, (time.time() - t0) / 60))


def profile_db(view):
    p = profile_path(view)
    if not p.exists():
        sys.exit("no profile yet: tribonian cocite usc-profile --view %s" % view)
    return sqlite3.connect("file:%s?mode=ro&immutable=1" % p, uri=True)


def profile_query(args):
    from tribonian import cocite
    db, pr = cocite.open_view(args.view), profile_db(args.view)
    if args.node:
        nodes = [r[0] for r in pr.execute("SELECT DISTINCT node FROM noden WHERE node = ? OR node LIKE ?",
                                          (args.node, args.node + "@%"))]
        for nd in nodes:
            print("%s: which sub-clusters' decisions cite it (top %d per slice, n >= %d)" % (nd, args.top, args.min_n))
            for (s,) in pr.execute("SELECT DISTINCT slice FROM prof WHERE node=? ORDER BY slice", (nd,)).fetchall():
                tot = pr.execute("SELECT decisions FROM noden WHERE slice=? AND node=?", (s, nd)).fetchone()[0]
                print("  %d-%02d  (%s decisions)" % (s, (s + 4) % 100, format(tot, ",")))
                for sub, n, share, lift in pr.execute("SELECT sub, n, share, lift FROM prof WHERE slice=? AND node=? "
                                                      "ORDER BY n DESC LIMIT ?", (s, nd, args.top)):
                    name = cocite.unit_name(db, s, sub, "sub")[0]
                    print("     %5d  %4.1f%% of its decisions  lift %5.1f  %s" % (n, 100 * share, lift, name))
    if args.sub:
        s, sub = (int(x) for x in args.sub.split(":"))
        name = cocite.unit_name(db, s, sub, "sub")[0]
        tot = pr.execute("SELECT decisions FROM subn WHERE slice=? AND sub=?", (s, sub)).fetchone()
        print("%d-%02d sub-cluster %d, %s: %s decisions" % (s, (s + 4) % 100, sub, name, format(tot[0] if tot else 0, ",")))
        print("  provisions its decisions cite, by lift (n >= %d):" % args.min_n)
        for nd, n, share, lift in pr.execute("SELECT node, n, share, lift FROM prof WHERE slice=? AND sub=? AND n >= ? "
                                             "ORDER BY lift DESC LIMIT ?", (s, sub, args.min_n, args.top)):
            print("     %-16s %5d  %4.1f%%  lift %6.1f" % (nd, n, 100 * share, lift))



# ================================================================ "Cited with" in the US Code section notes (usc2md.py)
_SEC = {}


def _sec_state(view):
    """(results view, profile db, map, links into each node) once per process; None when not built"""
    if view not in _SEC:
        from tribonian import cocite
        try:
            db, p = cocite.open_view(view), profile_path(view)
            if db is None or not p.exists() or not map_path().exists():
                raise FileNotFoundError
            pr = sqlite3.connect("file:%s?mode=ro&immutable=1" % p, uri=True)
            um = load_map()
            into = defaultdict(list)
            for a, b, y, s in um.links():
                into[b].append((a, y))
            _SEC[view] = (db, pr, um, into)
        except (FileNotFoundError, SystemExit, sqlite3.Error):
            _SEC[view] = None
    return _SEC[view]


def note_link(node):
    """'42:1973' -> '[[42-usc-1973|42 U.S.C. § 1973]]'; an era node names its end year"""
    key, _, era = node.partition("@-")
    t, s = key.split(":", 1)
    txt = "%s U.S.C. § %s" % (t, s)
    return "[[%s-usc-%s|%s]]%s" % (t.lower(), s, txt, " (as cited before %s)" % era if era else "")


def section_md(identifier, view=None, top=3, min_n=5):
    """Markdown lines for '## Cited with' in a section note: per slice, newest first, the sub-clusters whose decisions
    cite this section most, linked to their lineage notes in the co-citation vault. [] when nothing to show."""
    from tribonian import cocite
    st = _sec_state(view or cocite.NOTE_VIEW)
    if st is None:
        return []
    db, pr, um, into = st
    keys = id_keys(identifier)
    if len(keys) != 1:
        return []
    key = keys[0]
    vl_lin, _ = cocite.vault_links(db)
    nodes = [key] + sorted({r[0] for r in pr.execute("SELECT DISTINCT node FROM noden WHERE node LIKE ?", (key + "@-%",))})
    out = []
    for nd in nodes:
        slices = pr.execute("SELECT slice, decisions FROM noden WHERE node=? ORDER BY slice DESC", (nd,)).fetchall()
        prev = sorted(into.get(nd, []), key=lambda x: -x[1])
        nxt = [(b, y) for b, y, s, d in um.successors(nd, depth=1)]
        if not slices and not prev and not nxt:
            continue
        if nd != key:
            out += ["### Former § %s (as cited before %s)" % (key.split(":", 1)[1], nd.partition("@-")[2]), ""]
        if prev:
            out.append("Formerly: " + "; ".join("%s%s" % (note_link(a), " (moved %d)" % y if y else "") for a, y in prev[:8])
                       + (" and %d more" % (len(prev) - 8) if len(prev) > 8 else ""))
            out.append("")
        if nxt:
            out.append("Now: " + "; ".join("%s%s" % (note_link(b), " (moved %d)" % y if y else "") for b, y in nxt[:8])
                       + (" and %d more" % (len(nxt) - 8) if len(nxt) > 8 else ""))
            out.append("")
        if not slices:
            continue
        out += ["| Years | Decisions | Sub-clusters citing it most (share of the sub-cluster's decisions, lift) |",
                "|---|---|---|"]
        for s, n in slices:
            cells = []
            for sub, k, share, lift in pr.execute("SELECT sub, n, share, lift FROM prof WHERE slice=? AND node=? AND n >= ? "
                                                  "ORDER BY n DESC LIMIT ?", (s, nd, min_n, top)):
                name, lin, _ = cocite.unit_name(db, s, sub, "sub")
                name = (name or "sub-cluster %d" % sub).replace("|", "/")
                link = "[[%s\\|%s]]" % (vl_lin[lin], name) if lin in vl_lin else name
                cells.append("%s %d (%.0f%%, ×%.0f)" % (link, k, 100 * share, lift))
            out.append("| %d–%02d | %s | %s |" % (s, (s + 4) % 100, format(n, ","), "; ".join(cells) or "—"))
        out.append("")
    if not out:
        return []
    return (["## Cited with", "",
             "*Appellate decisions citing this section, grouped by the co-citation sub-cluster most of their other "
             "citations fall in. Derived from CourtListener citations by `cocite_usc.py`; not part of the law.*", ""]
            + out)



# ================================================================ cited by / recent appellate cites (usc-citedby)
# Two more derived sections for the US Code notes, from the same decision x section pairs as usc-profile:
#   ## Cited by                 the decisions (any court) citing the section, the most cited first
#   ## Recent appellate cites   appellate decisions citing it per year, and the newest ones
# Precomputed once into cocite-cache/usc/citedby.sqlite, so a note build only reads a few rows.

CITEDBY_VERSION = 2                             # 2: slug per case (CourtListener needs /opinion/<id>/<slug>/)
APPELLATE = (1, 2, 4, 5)                        # cocite.COURT_LEVELS codes: scotus, circuit, state-supreme, state-appellate
CAND = 400                                      # newest-candidates per section, sorted by filing date afterwards


def citedby_path():
    return usc_dir() / "citedby.sqlite"


def case_rows(con, want):
    """cluster id -> (name, best citation, date filed, slug). One sequential scan for many ids, as cocite.fill_names
    (on the hard disk each index lookup is a seek). The slug as case2md.opinion_url: the database's, else from the name."""
    from tribonian.case2md import slugify
    want = sorted({int(x) for x in want})
    info, cites = {}, {}
    if not want:
        return {}
    if len(want) > 20000:
        w = np.zeros(want[-1] + 1, bool)
        w[want] = True
        for cid, short, full, filed, slug in con.execute("SELECT id, case_name_short, case_name, date_filed, slug FROM clusters"):
            if cid is not None and 0 <= cid < len(w) and w[cid]:
                info[cid] = (short, full, filed, slug)
        for cid, v, rep, pg, typ in con.execute("SELECT cluster_id, volume, reporter, page, type FROM cites"):
            if cid is not None and 0 <= cid < len(w) and w[cid] and (cid not in cites or (typ or 99) < cites[cid][0]):
                cites[cid] = (typ or 99, "%s %s %s" % (v, rep, pg))
    else:
        for i in range(0, len(want), 500):
            ids = want[i:i + 500]
            q = ",".join("?" * len(ids))
            for r in con.execute("SELECT id, case_name_short, case_name, date_filed, slug FROM clusters WHERE id IN (%s)" % q, ids):
                info[r[0]] = r[1:]
            for cid, v, rep, pg, typ in con.execute(
                    "SELECT cluster_id, volume, reporter, page, type FROM cites WHERE cluster_id IN (%s)" % q, ids):
                if cid not in cites or (typ or 99) < cites[cid][0]:
                    cites[cid] = (typ or 99, "%s %s %s" % (v, rep, pg))
    out = {}
    for cid in want:
        short, full, filed, slug = info.get(cid, (None, None, None, None))
        name = short or full or "cluster %d" % cid
        if not short and full and len(full) > 60:
            name = full[:57] + "..."
        slug = slug or slugify(full or short or "", 75) or "case"
        out[cid] = (name, cites.get(cid, (0, ""))[1], filed or "", slug)
    return out


def groups(n_sorted):
    """start, end of each run of equal values in a sorted array"""
    if not len(n_sorted):
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    st = np.flatnonzero(np.r_[True, n_sorted[1:] != n_sorted[:-1]])
    return st, np.r_[st[1:], len(n_sorted)]


def cmd_usc_citedby(args):
    import shutil, tempfile
    from tribonian import cocite
    con = cocite.main_db()
    row = con.execute("SELECT file FROM loaded WHERE name='citation-map'").fetchone()
    m = re.search(r"\d{4}-\d\d-\d\d", row[0]) if row else None
    bulk = m.group(0) if m else ""
    um = load_map()
    dec, key, yr, keys = load_refs()                # every court
    t0 = time.time()
    cd = np.asarray(np.load(cocite.CACHE / "cited.npy", mmap_mode="r"))
    lvl = np.asarray(np.load(cocite.CACHE / "level.npy", mmap_mode="r"))
    n_cited = np.bincount(cd, minlength=int(dec.max()) + 1 if len(dec) else 0)   # decisions citing each case
    c = n_cited[dec].astype(np.int64)
    dl = np.where(dec < len(lvl), lvl[np.minimum(dec, len(lvl) - 1)], 0).astype(np.int64)

    # node = the section as cited; a reused number cited before its reuse is its own era node (as usc-profile)
    node = key.astype(np.int64)
    names = list(keys)
    extra = {}
    order = np.argsort(key, kind="stable")
    ks = key[order]
    for i, k in enumerate(keys):
        if not um.ends.get(k):
            continue
        rows = order[np.searchsorted(ks, i):np.searchsorted(ks, i, "right")]
        for y in np.unique(yr[rows]).tolist():
            nd = um.node(k, int(y))
            if nd != k:
                node[rows[yr[rows] == y]] = extra.setdefault(nd, len(keys) + len(extra))
    names += [nd for nd, _ in sorted(extra.items(), key=lambda x: x[1])]

    # most cited: per node by cited-by count, then newest decision id
    o = np.lexsort((-dec.astype(np.int64), -c, node))
    st, en = groups(node[o])
    total = dict(zip(node[o][st].tolist(), (en - st).tolist()))
    rank = np.arange(len(o)) - np.repeat(st, en - st)
    top_rows = o[rank < args.top]

    # newest appellate: per node by year, then the candidates are sorted by filing date
    a = np.flatnonzero(np.isin(dl, APPELLATE))
    oa = a[np.lexsort((-dec[a].astype(np.int64), -yr[a], node[a]))]
    st, en = groups(node[oa])
    app_total = dict(zip(node[oa][st].tolist(), (en - st).tolist()))
    size = en - st
    ya = yr[oa]
    thr = np.where(size >= args.top, ya[np.minimum(st + args.top - 1, len(oa) - 1)], 0) if len(oa) else np.zeros(0)
    rank = np.arange(len(oa)) - np.repeat(st, size)
    cand = oa[(ya >= np.repeat(thr, size)) & (rank < CAND)]

    # appellate decisions per year, the last --years years
    y0 = int(bulk[:4]) - args.years if bulk else int(yr.max()) - args.years
    k = a[yr[a] >= y0]
    py, pn = np.unique(node[k] * 10000 + yr[k], return_counts=True)

    info = case_rows(con, np.concatenate([dec[top_rows], dec[cand]]))
    lv = lambda l: cocite.COURT_LEVELS[l - 1] if 1 <= l <= len(cocite.COURT_LEVELS) else ""
    p = citedby_path()
    tmpdir = Path(tempfile.mkdtemp(prefix="usc-citedby-"))
    tmp = tmpdir / p.name
    out = sqlite3.connect(tmp)
    out.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE total(node TEXT PRIMARY KEY, decisions INTEGER, appellate INTEGER);
        CREATE TABLE top(node TEXT, kind TEXT, rank INTEGER, ref INTEGER, cited_by INTEGER, year INTEGER,
                         court TEXT, name TEXT, cite TEXT, filed TEXT, slug TEXT);
        CREATE TABLE peryear(node TEXT, year INTEGER, decisions INTEGER);
    """)
    out.executemany("INSERT INTO total VALUES (?,?,?)",
                    [(names[n], t, app_total.get(n, 0)) for n, t in total.items()])

    def row(n, kind, r, i):
        nm, ct, fd, sl = info[int(dec[i])]
        return (names[n], kind, r, int(dec[i]), int(c[i]), int(yr[i]), lv(int(dl[i])), nm, ct, fd, sl)
    rows, last, r = [], None, 0
    for i in top_rows.tolist():
        n = int(node[i])
        r = r + 1 if n == last else 1
        last = n
        rows.append(row(n, "cited", r, i))
    by = defaultdict(list)
    for i in cand.tolist():
        by[int(node[i])].append(i)
    for n, lst in by.items():
        lst.sort(key=lambda i: (info[int(dec[i])][2] or "%04d" % yr[i], int(dec[i])), reverse=True)
        rows += [row(n, "recent", j + 1, i) for j, i in enumerate(lst[:args.top])]
    out.executemany("INSERT INTO top VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    out.executemany("INSERT INTO peryear VALUES (?,?,?)",
                    [(names[int(v // 10000)], int(v % 10000), int(n)) for v, n in zip(py.tolist(), pn.tolist())])
    out.executescript("CREATE INDEX top_by_node ON top(node, kind, rank); CREATE INDEX peryear_by_node ON peryear(node, year);")
    out.executemany("INSERT INTO meta VALUES (?,?)", [
        ("version", str(CITEDBY_VERSION)), ("usc_version", str(USC_VERSION)), ("map_version", str(MAP_VERSION)),
        ("bulk", bulk), ("top", str(args.top)), ("first_year", str(y0)), ("built", time.strftime("%Y-%m-%d %H:%M"))])
    out.commit()
    out.close()
    part = p.with_name(p.name + ".part")
    shutil.copyfile(tmp, part)
    part.replace(p)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("wrote %s: %s sections, %s rows  (%.1f min)" % (p, format(len(total), ","), format(len(rows), ","),
                                                         (time.time() - t0) / 60))


_CB = {}


def _cb_state():
    """(citedby db, case-note index) once per process; db None when usc-citedby hasn't run"""
    if not _CB:
        import json
        p = citedby_path()
        try:
            _CB["db"] = sqlite3.connect("file:%s?mode=ro&immutable=1" % p, uri=True) if p.exists() else None
        except sqlite3.Error:
            _CB["db"] = None
        try:
            _CB["index"] = json.loads((tconfig.WORK / "cases" / "_index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _CB["index"] = {}
        _CB["meta"] = dict(_CB["db"].execute("SELECT key, value FROM meta")) if _CB["db"] else {}
        if _CB["meta"].get("version") != str(CITEDBY_VERSION):     # older file: leave the sections out until rebuilt
            if _CB["db"]:
                print("  %s is version %s, this code needs %d: rerun tribonian cocite usc-citedby"
                      % (p.name, _CB["meta"].get("version"), CITEDBY_VERSION), file=sys.stderr)
            _CB["db"] = None
    return _CB["db"], _CB["index"], _CB["meta"]


def case_cell(ref, name, index, slug=None):
    """the case note when there is one, else CourtListener's page, which needs the slug: /opinion/<id>/<slug>/"""
    name = (name or "cluster %d" % ref).replace("|", "/")
    e = index.get(str(ref))
    if e:
        return "[[%s\\|%s]]" % (e["slug"], name)
    return "[%s](https://www.courtlistener.com/opinion/%d/%s/)" % (name.replace("[", "(").replace("]", ")"), ref, slug or "case")


def _key_of(identifier):
    keys = id_keys(identifier)
    return keys[0] if len(keys) == 1 else None


def citedby_md(identifier, top=20):
    """'## Cited by': the most cited decisions (any court) citing this section; [] when nothing to show"""
    db, index, meta = _cb_state()
    key = _key_of(identifier) if db else None
    if not key:
        return []
    nodes = [key] + sorted(r[0] for r in db.execute("SELECT node FROM total WHERE node LIKE ?", (key + "@-%",)))
    out = []
    for nd in nodes:
        t = db.execute("SELECT decisions FROM total WHERE node=?", (nd,)).fetchone()
        rows = db.execute("SELECT ref, name, cite, year, court, cited_by, slug FROM top WHERE node=? AND kind='cited' "
                          "ORDER BY rank LIMIT ?", (nd, top)).fetchall()
        if not t or not rows:
            continue
        if nd != key:
            out += ["### As cited before %s (the number was later reused)" % nd.partition("@-")[2], ""]
        out += ["%s decisions cite it; the %d most cited:" % (format(t[0], ","), len(rows)), "",
                "| Case | Citation | Year | Court | Cited by |", "|---|---|---|---|---|"]
        out += ["| %s | %s | %s | %s | %s |" % (case_cell(ref, nm, index, sl), ct or "", y or "", co, format(n, ","))
                for ref, nm, ct, y, co, n, sl in rows]
        out.append("")
    if not out:
        return []
    return (["## Cited by", "",
             "*Decisions from any court citing this section, the most cited by later decisions first (CourtListener "
             "bulk data of %s; citations found in the opinion text by `cocite_usc.py`). Not part of the law.*"
             % (meta.get("bulk") or "?"), ""] + out)


def recent_md(identifier, top=20):
    """'## Recent appellate cites': appellate decisions citing this section per year, and the newest; [] when none"""
    db, index, meta = _cb_state()
    key = _key_of(identifier) if db else None
    if not key:
        return []
    rows = db.execute("SELECT ref, name, cite, filed, year, court, cited_by, slug FROM top WHERE node=? AND kind='recent' "
                      "ORDER BY rank LIMIT ?", (key, top)).fetchall()
    if not rows:
        return []
    bulk = meta.get("bulk") or ""
    y0 = int(meta.get("first_year") or 0)
    per = dict(db.execute("SELECT year, decisions FROM peryear WHERE node=?", (key,)))
    last = int(bulk[:4]) if bulk else max(per or [y0])
    years = " · ".join("%d%s %d" % (y, " (to %s)" % bulk[5:] if bulk and y == last and bulk[5:] != "12-31" else "",
                                    per.get(y, 0)) for y in range(y0, last + 1))
    t = db.execute("SELECT appellate FROM total WHERE node=?", (key,)).fetchone()
    out = ["## Recent appellate cites", "",
           "*Supreme Court, circuit, and state supreme and appellate decisions citing this section, to the CourtListener "
           "bulk data of %s; newer decisions are not in it. Not part of the law.*" % (bulk or "?"), "",
           "Per year: " + years + ("  (all years: %s)" % format(t[0], ",") if t else ""), "",
           "| Filed | Case | Citation | Court | Cited by |", "|---|---|---|---|---|"]
    out += ["| %s | %s | %s | %s | %s |" % (fd or y, case_cell(ref, nm, index, sl), ct or "", co, format(n, ","))
            for ref, nm, ct, fd, y, co, n, sl in rows]
    return out + [""]



def add_parser(sub):
    p = sub.add_parser("usc-sample", help="how US Code cites appear in opinion text (cocite_usc.py); writes nothing")
    p.add_argument("--n", type=int, default=2000, help="random opinions to read")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--examples", type=int, default=0, metavar="K", help="print K random examples of each kind")
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("usc-extract", help="every US Code reference, in positions' coordinates (cocite_usc.py)")
    p.add_argument("--show", type=int, metavar="OPINION_ID", help="one opinion's references with context")
    p.add_argument("--sample", type=int, metavar="N", help="N random opinions: rates; writes nothing")
    p.add_argument("--status", action="store_true")
    p.add_argument("--workers", type=int)
    p.add_argument("--shard-size", dest="shard_size", type=int, default=cp.SHARD)
    p.add_argument("--limit", type=int, help="at most this many shards (a trial run)")
    p.add_argument("--out", help="shard folder (default: cocite-cache/usc)")
    p.set_defaults(func=cmd_usc_extract)
    p = sub.add_parser("usc-map", help="old Code numbers -> current, from the notes in xml/ (cocite_usc.py)")
    p.add_argument("--xml", help="folder of usc*.xml (default: xml/ beside the scripts)")
    p.add_argument("--key", action="append", metavar="T:S[@YEAR]", help="show how one number resolves")
    p.add_argument("--dry", action="store_true", help="build and report, write nothing")
    p.set_defaults(func=cmd_usc_map)
    p = sub.add_parser("usc-check", help="the identity map against the citation data (cocite_usc.py)")
    p.add_argument("--courts", default="appellate", help="citing courts (default: appellate)")
    p.add_argument("--window", type=int, default=10, help="years before / after a move")
    p.add_argument("--min", type=int, default=20, help="citing decisions needed on each side")
    p.add_argument("--max-edges", dest="max_edges", type=int, default=3000, help="known edges tested (a random subset)")
    p.add_argument("--first", type=int, default=1940, help="first slice for candidate moves")
    p.add_argument("--last", type=int, default=2020)
    p.add_argument("--rise", type=float, default=0.25, help="a riser's earlier slice holds at most this share of its later one")
    p.add_argument("--min-cos", dest="min_cos", type=float, default=0.2,
                   help="candidate threshold (known moves: median 0.21, 10%% 0.06; random targets: 90%% 0.03)")
    p.add_argument("--holdout", help="target titles whose map edges are hidden, to measure recall, e.g. 34,52,54")
    p.add_argument("--show", type=int, default=15)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_usc_check)
    from tribonian import cocite
    p = sub.add_parser("usc-profile", help="which Code provisions each sub-cluster's decisions cite (cocite_usc.py)")
    p.add_argument("--view", default=cocite.NOTE_VIEW, help="results view (default: the case notes' view)")
    p.add_argument("--node", help="show one provision, e.g. 42:1983 (all its reuse eras)")
    p.add_argument("--sub", metavar="SLICE:SUB", help="show one sub-cluster, e.g. 2010:17")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--min-n", dest="min_n", type=int, default=5, help="decisions needed for a row")
    p.set_defaults(func=cmd_usc_profile)
    p = sub.add_parser("usc-citedby", help="most cited and newest appellate decisions per Code section (cocite_usc.py)")
    p.add_argument("--top", type=int, default=20, help="decisions kept per section and list")
    p.add_argument("--years", type=int, default=5, help="per-year appellate counts: this many years before the bulk year")
    p.set_defaults(func=cmd_usc_citedby)
    return p

