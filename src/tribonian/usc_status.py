"""usc_status.py -- is a US Code cite in an old opinion still good today? case2md.py marks the ones that aren't.

    tribonian usc-status "42 U.S.C. § 1973(b)" --year 1986      # one cite, as case2md would mark it
    tribonian usc-status --rebuild                                # re-read xml/ (automatic when an xml file changes)

Each cite gets one tag, judged at the year its text is as of: the edition in its parenthetical when there is one
("(1976 ed., Supp. V)" -> 1981, "(1982)" -> 1982, "18 U.S.C. (1940 ed.) § 80" -> 1940), else the decision year.
    mv    renumbered or transferred: the current number is given (usc_map.json chains, or the stub's own note)
    rep   repealed (with the successor when the stub says "See section ... of Title N")
    om    omitted from the Code
    pre   the cited number exists today, but its current section was enacted after the cite's year
          (number reused, or the provision re-enacted): the cite meant something else
    §amd  amended after the cite's year, but not in the cited subsection (lighter highlight): read the section's
          Amendments note to be sure the change doesn't bear on it
    rd    the cited subsection was redesignated (or a part above it was) after the cite's year: the same label may now
          be different text
    amd   same number, amended after the cite's year (year of the latest amendment shown). With a subsection in the
          cite, only amendments the Amendments note puts on an overlapping part count: (b), (b)(6), (b)(6)(A) for a
          cite to (b)(6), not (a)(2); a line that names no part, or "amended section generally", counts for all.
          A source-credit year after the cite with no Amendments-note line near it still counts (older amendments)
    nf    not in the current Code and no move known (the title's XML is present, the section isn't)
    ok    current, not amended since: not marked
Amendment years come from the section's source credit (dates after the first) and its Amendments note (which part
each act changed). A cite with no subsection is judged at section level.
Edition years are approximate (an edition's Supplement N is taken as edition year + N). The tag means "look closer".

Sources: xml/*.xml (status, source-credit dates, transferred/renumbered stubs) and, when present,
cocite-cache/usc/usc_map.json (tribonian cocite usc-map). Index cached in build/usc_status.json.
"""
import argparse, html, json, re, sys
from pathlib import Path

from tribonian import tconfig

ROOT = tconfig.WORK
XML = ROOT / "xml"
CACHE = ROOT / "build" / "usc_status.json"
VERSION = 4

SEC = r"\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?"
SUB = r"(?:\([A-Za-z0-9]{1,5}\))*"
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
# title, U.S.C. (any spacing; .A / .S. annotated), optional "(1940 ed.)", § or §§ (or none), a section list, then
# an optional edition parenthetical: (1976 ed.) (1976 ed., Supp. V) (Supp. IV 1980) (1982) (1970 ed. and Supp. V)
CITE_RE = re.compile(
    r"\b(?P<title>\d{1,2})\s*U\.?\s?S\.?\s?C\.?(?:\s?[AS]\.?)?"
    r"(?:\s*\((?P<ed0>(?:19|20)\d\d)(?:\s*ed\.)?\))?"
    r"\s*(?P<sign>§§?|[Ss]ec(?:tions?|s?\.))?\s*"
    r"(?P<secs>" + SEC + SUB + r"(?:(?:,\s*(?:and\s+|or\s+)?|\s+(?:and|or|through)\s+)" + SEC + SUB
    + r"(?!\s*U\.?\s?S\.?\s?C|\s*[A-Z][a-z]*\.))*)"
    r"(?P<paren>\s*\((?:(?:19|20)\d\d(?:\s*ed\.)?)?(?:,?\s*(?:and\s+)?Supp\.\s*[IVX]+(?:,?\s*(?:19|20)\d\d)?)?\))?")
MONTHS = r"(?:Jan|Feb|Mar|Apr|May|June|July|Aug|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},\s+((?:17|18|19|20)\d\d)"
MOVE_RE = re.compile(r"(?:reclassified|transferred|renumbered|redesignated)\s+(?:as|to)\s*(?:<[^>]+>\s*)*?"
                     r"(?:<ref\s+href=\"/us/usc/t(\w+)/s([^\"/]+)\")", re.S)


def ukey(title, sec):
    return "%s:%s" % (str(title).upper().lstrip("0") or "0", sec.replace("–", "-").lower())


def parts(cite_secs, sign):
    """'1981, 1983(c), and 1985(3)' -> [('1981', []), ('1983', ['c']), ('1985', ['3'])]: section and the subsection
    path cited; a list only after §§ (else just the first); a range '1981-1983' -> both ends, no path"""
    found = [(m.group(1), re.findall(r"\(([^)]+)\)", m.group(2)))
             for m in re.finditer(r"(?<![\w(])(" + SEC + r")(" + SUB + r")", cite_secs)]
    if sign != "§§" and not (sign or "").lower().startswith(("sections", "secs")):
        found = found[:1]
    out = []
    for n, path in found:
        a, _, b = n.partition("-")
        for part in ([(a, []), (b, [])] if b.isdigit() and a.isdigit() and int(b) > int(a) else [(n, path)]):
            if part[0] not in [o[0] for o in out]:
                out.append(part)
    return out


def sections(cite_secs, sign):
    return [n for n, _ in parts(cite_secs, sign)]


def cite_year(m):
    """the year the cite's text is as of, from its edition, or None"""
    if m.group("ed0"):
        return int(m.group("ed0"))
    p = m.group("paren") or ""
    years = [int(y) for y in re.findall(r"(?:19|20)\d\d", p)]
    supp = re.search(r"Supp\.\s*([IVX]+)", p)
    if supp and len(years) >= 2:
        return max(years)
    if supp and years and "ed" in p:
        return years[0] + ROMAN.get(supp.group(1), 0)
    if supp and years:                          # "(Supp. IV 1980)": the year given
        return years[0]
    return years[0] if years else None


# ---------------------------------------------------------------- index of the current Code
def credit_years(body):
    m = re.search(r"<sourceCredit\b.*?</sourceCredit>", body, re.S)
    if not m:
        return []
    t = html.unescape(re.sub(r"<[^>]+>", " ", m.group(0)))
    years = [int(y) for y in re.findall(MONTHS, t)]
    if re.match(r"\s*\(?\s*R\.\s?S\.", t):        # Revised Statutes, 1874: no date printed
        years.insert(0, 1874)
    return years


SEE_RE = re.compile(r"\bSee\s+sections?\s+(.*?)\s+of\s+(?:[Tt]itle\s+(\d+[A-Za-z]?)|(this title))")


DESIG_RE = re.compile(r"^(?:(\d{4})\s*—\s*)?(?:(Subsecs?|Pars?|Subpars?|Cls?|Subcls?|Items?)\.\s+"
                      r"((?:\([^)]{1,6}\)|,|\s|and|to|through)+?)\.\s)?")


def expand(a, b):
    """last labels of a range: 'f','i' -> f g h i; '3','5' -> 3 4 5; else the two ends"""
    if a.isdigit() and b.isdigit() and int(a) < int(b) <= int(a) + 30:
        return [str(i) for i in range(int(a), int(b) + 1)]
    if len(a) == len(b) == 1 and a.isalpha() and b.isalpha() and a < b:
        return [chr(c) for c in range(ord(a), ord(b) + 1)]
    return [a, b]


def desig_paths(text):
    """'(a)(4)(A)(viii)' -> [['a','4','A','viii']]; '(f) to (i)' -> f..i; '(b)(2)(A), (B)' -> (b)(2)(A), (b)(2)(B)"""
    out, first = [], None
    pieces = re.split(r"\s*(,\s*(?:and\s+)?|\s+and\s+|\s+(?:to|through)\s+)\s*", text.strip())
    seps, items = pieces[1::2], pieces[0::2]
    for i, it in enumerate(items):
        path = re.findall(r"\(([^)]+)\)", it)
        if not path:
            continue
        if first is None:
            first = path
        elif len(path) < len(first):                # relative to the first: same parents
            path = first[:len(first) - len(path)] + path
        if i and re.search(r"to|through", seps[i - 1]) and out:
            prev = out[-1]
            for lab in expand(prev[-1], path[-1])[1:]:
                out.append(prev[:-1] + [lab])
            continue
        out.append(path)
    return out


def amendments(body):
    """the Amendments note -> [(year, [paths] or None = the whole section, redesignation?)], one per paragraph"""
    out = []
    for note in re.findall(r'<note\b[^>]*topic="amendments"[^>]*>(.*?)</note>', body, re.S):
        year, paths = 0, None
        for p in re.findall(r"<p\b[^>]*>(.*?)</p>", note, re.S):
            t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", p))).strip()
            if not t or t[0] in "“\"(":                    # quoted former text
                continue
            m = DESIG_RE.match(t)
            if m.group(1):
                year, paths = int(m.group(1)), None
            if m.group(3):
                paths = desig_paths(m.group(3)) or None
            if not year:
                continue
            whole = paths is None or re.search(r"amended (?:the )?section generally|section was amended generally", t)
            out.append((year, None if whole else paths, bool(re.search(r"redesignat", t))))
    return out


def see_also(title, body):
    """a repealed stub's "See section(s) ... of Title N": where the provision went, if anywhere"""
    head = html.unescape(re.sub(r"<[^>]+>", " ", body.split("role=\"crossHeading\"", 1)[0]))
    out = []
    for m in SEE_RE.finditer(re.sub(r"\s+", " ", head)):
        t = m.group(2) or title
        for n in re.findall(r"(?<![\w(])" + SEC, re.sub(r"\([^)]*\)", "", m.group(1))):
            k = ukey(t, n)
            if k not in out:
                out.append(k)
    return out


def build_index():
    secs, titles = {}, set()
    for f in sorted(XML.glob("usc*.xml")):
        s = open(f, encoding="utf-8").read()
        s = re.sub(r"<quotedContent\b.*?</quotedContent>", "", s, flags=re.S)
        for m in re.finditer(r"<section\b([^>]*)>(.*?)</section>", s, re.S):
            ident = re.search(r'identifier="([^"]+)"', m.group(1))
            if not ident:
                continue
            st = re.search(r'status="(\w+)"', m.group(1))
            status = st.group(1) if st else ""
            body = m.group(2)
            for part in ident.group(1).split():
                k = re.fullmatch(r"/us/usc/t(\w+?)/s([^/]+)", part)
                if not k:
                    continue
                titles.add(ukey(k.group(1), "0").split(":")[0])
                key = ukey(k.group(1), k.group(2))
                e = {"st": status}
                if status in ("transferred", "renumbered"):
                    e["to"] = list(dict.fromkeys(ukey(t, n) for t, n in MOVE_RE.findall(body)))
                elif status == "repealed":
                    e["see"] = see_also(k.group(1), body)
                elif not status:
                    e["y"] = credit_years(body)
                    am = amendments(body)
                    if am:
                        e["am"] = am
                if key not in secs or not status:          # a live section wins over a stub with the same number
                    secs[key] = e
    return {"version": VERSION, "stamp": stamp(), "titles": sorted(titles), "secs": secs}


def stamp():
    fs = sorted(XML.glob("usc*.xml"))
    return "%d:%d" % (len(fs), max((int(f.stat().st_mtime) for f in fs), default=0))


_IDX = []


def index():
    if _IDX:
        return _IDX[0]
    idx = None
    if CACHE.exists():
        try:
            idx = json.loads(CACHE.read_text())
            if idx.get("version") != VERSION or idx.get("stamp") != stamp():
                idx = None
        except (ValueError, OSError):
            idx = None
    if idx is None:
        if not XML.is_dir():
            idx = {"titles": [], "secs": {}}
        else:
            print("usc_status: indexing xml/ (once per release point)", file=sys.stderr)
            idx = build_index()
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE.with_name(CACHE.name + ".%d.tmp" % id(idx))
            tmp.write_text(json.dumps(idx))
            tmp.replace(CACHE)
    idx["titles"] = set(idx["titles"])
    _IDX.append(idx)
    return idx


_MAP = []


def usc_map():
    """cocite_usc.UscMap over cocite-cache/usc/usc_map.json, or None"""
    if not _MAP:
        um = None
        try:
            from tribonian import cocite_usc
            if cocite_usc.map_path().exists():
                um = cocite_usc.load_map()
        except Exception:
            um = None
        _MAP.append(um)
    return _MAP[0]


# ---------------------------------------------------------------- one cite
def overlaps(a, b):
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def judge(key, year, path=()):
    """as-cited key 'title:section', year the cite is as of (0 = unknown), cited subsection path ['b','6'] ->
    (tag, new keys, year shown). With a path, only amendments to an overlapping part count (Amendments note);
    a source-credit year after the cite with no note line at all still counts for the whole section."""
    idx = index()
    title = key.split(":")[0]
    if title not in idx["titles"]:
        return "ok", [], None                          # no XML for this title: nothing to say
    um = usc_map()
    if um:
        got = um.resolve(key, year or 0)
        if got != [key]:
            return "mv", got, None
    e = idx["secs"].get(key)
    if e is None:
        return "nf", [], None
    st = e["st"]
    if st in ("transferred", "renumbered"):
        return ("mv", e["to"], None) if e.get("to") else ("rep", [], None)
    if st == "repealed":
        return "rep", e.get("see") or [], None
    if st in ("omitted", "reserved"):
        return "om", [], None
    ys = e.get("y") or []
    if year and ys:
        if year < ys[0] - 1:                       # a year's slack: an act and the cite can share a year
            return "pre", [], ys[0]
        later = [y for y in ys[1:] if y > year]
        if not later:
            return "ok", [], None
        if not path:
            return "amd", [], max(later)
        am = [a for a in e.get("am", []) if a[0] > year]
        noted = {a[0] for a in am}
        hits = [a for a in am if a[1] is None or any(overlaps(path, q) for q in a[1])]
        rd = [a[0] for a in hits if a[2] and a[1] and any(len(q) <= len(path) and overlaps(path, q) for q in a[1])]
        if rd:
            return "rd", [], max(rd)
        unnoted = [y for y in later if y not in noted and y - 1 not in noted and y + 1 not in noted]
        if hits or unnoted:
            return "amd", [], max([a[0] for a in hits] + unnoted)
        return "§amd", [], max(later)                 # amended, but elsewhere in the section
    return "ok", [], None


def mark(m, decided=0):
    """a CITE_RE match + decision year -> (tag, [(old key, new keys)], year shown); the worst tag across a list"""
    yr = cite_year(m) or decided or 0
    if not decided and not cite_year(m):
        yr = 0
    rank = ["ok", "§amd", "amd", "rd", "pre", "nf", "om", "rep", "mv"]
    worst, moves, shown = "ok", [], None
    for s, path in parts(m.group("secs"), m.group("sign")):
        k = ukey(m.group("title"), s)
        tag, new, y = judge(k, yr, path)
        if new:
            moves.append((k, new))
        if rank.index(tag) > rank.index(worst):
            worst, shown = tag, y
        elif tag == worst and y and (shown is None or y > shown):
            shown = y
    return worst, moves, shown


def label(key):
    t, s = key.split(":")
    return "%s U.S.C. § %s" % (t, s)


def note_name(key):
    t, s = key.split(":")
    return "%s-usc-%s" % (t.lower(), s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cite", nargs="*")
    ap.add_argument("--year", type=int, default=0, help="decision year")
    ap.add_argument("--rebuild", action="store_true")
    a = ap.parse_args()
    if a.rebuild and CACHE.exists():
        CACHE.unlink()
    idx = index()
    print("index: %d sections, %d titles; map: %s" % (len(idx["secs"]), len(idx["titles"]), "yes" if usc_map() else "no"))
    for c in a.cite:
        m = CITE_RE.search(c)
        if not m:
            print("%-40s  not a Code cite" % c)
            continue
        tag, moves, y = mark(m, a.year)
        print("%-40s  as of %s: %s%s%s" % (m.group(0), cite_year(m) or a.year or "?", tag, " %d" % y if y else "",
                                          "".join("  %s -> %s" % (label(o), ", ".join(map(label, n))) for o, n in moves)))


if __name__ == "__main__":
    main()
