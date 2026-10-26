"""lawcites.py -- statutes, regulations and constitutional provisions cited in a text.

One key per provision, at section level (the subsection is kept as the pin, not the key):
    usc:42:1983            42 U.S.C. § 1983 (cocite_usc's extractor: no-§ forms, lists, ranges)
    const:amend-1          U.S. Const. amend. I; also by name ("the First Amendment",
                           "the Free Exercise Clause"), counted apart as `named`
    const:amend-14:1       U.S. Const. amend. XIV, § 1
    const:art-3:2          U.S. Const. art. III, § 2
    cfr:34:300.324         34 C.F.R. § 300.324;  cfr:34:pt-300  34 C.F.R. pt. 300
    co-crs:26.5-4-205      Colo. Rev. Stat. § 26.5-4-205, C.R.S. § ..., § 26.5-4-205, C.R.S.
    co-ccr:8-1404-1:4.109  8 CCR 1404-1 § 4.109, 8 Colo. Code Regs. 1404-1 § 4.109;  co-ccr:8-1404-1 alone
    co-const:art-9:7       Colo. Const. art. IX, § 7

A short form ("§ 26.5-4-205(2)", "§ 1983") takes the code of the last full cite with the same
shape. Regular expressions only; no language model.
"""
import re
from collections import Counter, defaultdict

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
         "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18,
         "XIX": 19, "XX": 20, "XXI": 21, "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27}
ORD = {w: i for i, w in enumerate("First Second Third Fourth Fifth Sixth Seventh Eighth Ninth Tenth Eleventh Twelfth "
                                  "Thirteenth Fourteenth Fifteenth Sixteenth Seventeenth Eighteenth Nineteenth Twentieth".split(), 1)}
CLAUSES = {"free exercise": "amend-1", "establishment": "amend-1", "free speech": "amend-1", "speech": "amend-1",
           "equal protection": "amend-14:1", "due process": None,      # Fifth or Fourteenth: ambiguous, not counted
           "supremacy": "art-6", "spending": "art-1:8", "commerce": "art-1:8"}

SUBS = r"(?:\s?\([A-Za-z0-9]{1,4}\))*"
NUM = r"(?:\d+|[IVXL]+)"
CONST_RE = re.compile(r"U\.\s?S\.\s?Const\.?,?\s*(?:(amend(?:ment)?s?\.?)\s*(" + NUM + r")|(art(?:icle)?\.?)\s*(" + NUM + r"))"
                      r"(?:,?\s*§+\s*(\d+))?(?:,?\s*cl\.\s*\d+)?" + SUBS, re.I)
NAMED_AMEND_RE = re.compile(r"\b(" + "|".join(ORD) + r")\s+Amendment\b")
NAMED_CLAUSE_RE = re.compile(r"\b(Free Exercise|Establishment|Free Speech|Equal Protection|Supremacy|Spending|Commerce)\s+Clauses?\b", re.I)
CFR_RE = re.compile(r"\b(\d{1,2})\s*C\.\s?F\.\s?R\.?\s*(?:(pts?\.|[Pp]arts?)\s*(\d+)|(§§?)?\s*(\d+)\.(\d+[a-z]?))" + SUBS)
CRS_SECT = r"\d+(?:\.\d+)?-\d+(?:\.\d+)?-\d+(?:\.\d+)?"
CRS_RE = re.compile(r"(?:Colo\.\s?Rev\.\s?Stat\.(?:\s?Ann\.)?|C\.\s?R\.\s?S\.|Colorado\s+Revised\s+Statutes)\s*(?:§§?|[Ss]ec(?:tion)?s?\.?)?\s*(" + CRS_SECT + ")" + SUBS)
CRS_AFTER_RE = re.compile(r"(?:§§?|[Ss]ection)\s*(" + CRS_SECT + ")" + SUBS + r",?\s*(?:C\.\s?R\.\s?S\.|Colo\.\s?Rev\.\s?Stat\.)")
CCR_RE = re.compile(r"\b(\d{1,2})\s*(?:CCR|C\.C\.R\.|Colo\.\s?Code\s?Regs?\.|Code\s+of\s+Colorado\s+Regulations)"
                    r"(?:\s*\(\d{4}\))?:?\s*(?:§\s*)?(\d{3,4}-\d+)"
                    r"(?:\s*(?:,\s*)?(?:§§?|[Rr]ule(?:\s+[Ss]ections?)?|:)\s*(\d+\.\d+))?")
COCONST_RE = re.compile(r"Colo\.\s?Const\.?,?\s*art\.?\s*(" + NUM + r")(?:,?\s*§+\s*(\d+))?", re.I)
ETSEQ_RE = re.compile(r"\s*,?\s*et\s+seq\.")
BARE_RE = re.compile(r"(?<![\w.§])(?:§§?)\s*(" + CRS_SECT + r"|\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?)(?![\w.-]\d)" + SUBS)


def num(s):
    s = s.upper()
    return ROMAN.get(s) or (int(s) if s.isdigit() else None)


def flatten(text):
    """pdftotext lines -> one line: a number broken after its hyphen ("26.5-4-" / "205") is rejoined. A Code
    cite cut at a line's end and followed by a line opening with a small number and a word ("20 U.S.C." /
    "5 In limited circumstances": a footnote, the cite's section on the next page) is not read as § 5."""
    t = re.sub(r"(\d-)\s*\n\s*(\d)", r"\1\2", text)
    t = re.sub(r"(U\.\s?S\.\s?C\.?[AS]?\.?)[ \t]*\n\s*(?=\d{1,3}\s+[A-Z][a-z])", "\\1\u2063 ", t)
    return re.sub(r"\s+", " ", t)


def find(text, flat=True):
    """-> dict(cites=Counter key -> formal citations, named=Counter key -> mentions by name,
               spans=[(start, end, key)] in the flattened text, text=the flattened text)"""
    from tribonian import cocite_usc as U
    t = flatten(text) if flat else text
    hits = []                                   # (start, end, key, kind)
    taken = []

    def free(a, b):
        return not any(a < y and x < b for x, y in taken)

    def add(a, b, key, kind="cite"):
        if key and free(a, b):
            hits.append((a, b, key, kind))
            taken.append((a, b))

    for m in CONST_RE.finditer(t):
        n = num(m.group(2) or m.group(4))
        if n:
            key = "const:%s-%d" % ("amend" if m.group(1) else "art", n) + (":%s" % m.group(5) if m.group(5) else "")
            add(m.start(), m.end(), key)
    for m in COCONST_RE.finditer(t):
        n = num(m.group(1))
        if n:
            add(m.start(), m.end(), "co-const:art-%d" % n + (":%s" % m.group(2) if m.group(2) else ""))
    for m in CFR_RE.finditer(t):
        key = "cfr:%s:pt-%s" % (m.group(1), m.group(3)) if m.group(2) else "cfr:%s:%s.%s" % (m.group(1), m.group(5), m.group(6))
        add(m.start(), m.end(), key)
    for rx in (CRS_RE, CRS_AFTER_RE):
        for m in rx.finditer(t):
            add(m.start(), m.end(), "co-crs:%s" % m.group(1))
    for m in CCR_RE.finditer(t):
        add(m.start(), m.end(), "co-ccr:%s-%s" % (m.group(1), m.group(2)) + (":%s" % m.group(3) if m.group(3) else ""))
    for m in U.FULL_RE.finditer(t):
        refs, end = U.full_refs(t, m)
        if free(m.start(), end):
            for k, (pos, title, sec, part) in enumerate(refs):
                if part != 3:                    # a range's end: the range is listed by its start
                    hits.append((pos, end if k == len(refs) - 1 else pos + 1, "usc:%s:%s" % (title.lower(), sec.lower()), "cite"))
            taken.append((m.start(), end))
    # short forms: the code of the last full cite with the same shape. A bare "§ N" after a U.S.C. cite is taken as
    # that title's only near a section the text cites in full (within 100): "§ 1414" after "20 U.S.C. § 1412" is,
    # "§ 702" after "42 U.S.C. § 2000e" is not (Title VII's own section numbering); what is not placed is counted
    hits.sort()
    full = sorted(hits)
    unresolved = Counter()
    base = lambda sec: int(re.match(r"\d+", sec).group(0)) if re.match(r"\d+", sec) else None
    full_usc = defaultdict(set)
    for a, b, key, kind in full:
        if key.startswith("usc:"):
            n = base(key.split(":")[2])
            if n is not None:
                full_usc[key.split(":")[1]].add(n)
    for m in BARE_RE.finditer(t):
        if not free(m.start(), m.end()) or U.NOT_SHORT.search(t[max(0, m.start() - 30):m.start()]) or \
                re.search(r"(?:\b[Cc]h\.|\b[Cc]hapter|P\.\s?L\.|Pub\.\s?L\.|Act|Laws)[^§;]{0,16}$", t[max(0, m.start() - 30):m.start()]):
            continue                             # a session law, a state code or an Act's own section: not a short form
        s = m.group(1)
        crs = re.fullmatch(CRS_SECT, s) is not None
        prev = [h for h in full if h[1] <= m.start()]
        placed = False
        for a, b, key, kind in reversed(prev):
            if crs and key.startswith("co-crs:"):
                add(m.start(), m.end(), "co-crs:%s" % s, "short")
                placed = True
                break
            if not crs and key.startswith("usc:"):
                t_, n = key.split(":")[1], base(s)
                if n is not None and any(abs(n - x) <= 100 for x in full_usc[t_]):
                    add(m.start(), m.end(), "usc:%s:%s" % (t_, s.lower()), "short")
                    placed = True
                break
            if not crs and not key.startswith("usc:"):
                break                            # the last full cite was another code: leave it
        if not placed:
            unresolved["§ " + s] += 1
    # by name: "the First Amendment", "the Free Exercise Clause"
    named = Counter()
    for m in NAMED_AMEND_RE.finditer(t):
        if free(m.start(), m.end()):
            named["const:amend-%d" % ORD[m.group(1)]] += 1
    for m in NAMED_CLAUSE_RE.finditer(t):
        key = CLAUSES.get(m.group(1).lower())
        if key and free(m.start(), m.end()):
            named["const:" + key] += 1
    cites = Counter(k for _, _, k, _ in hits)
    # "§§ 26.5-4-201 et seq.": the cite stands for the part it opens
    etseq = {k for a, b, k, _ in hits if ETSEQ_RE.match(t, b)}
    return {"cites": cites, "named": named, "etseq": etseq, "spans": sorted((a, b, k) for a, b, k, _ in hits), "text": t,
            "unresolved": unresolved}


def covers(key, other, etseq=()):
    """Does citing `key` take in `other`? The same provision at section level, or a part opened with et seq."""
    if section_of(key) == section_of(other):
        return True
    if key in etseq and key.startswith("co-crs:") and other.startswith("co-crs:"):
        # C.R.S. numbers title-article-section, the section's hundreds being its part: "26.5-4-201 et seq." is part 2
        (ka, ks), (oa, os_) = (x.split(":")[1].rsplit("-", 1) for x in (key, other))
        try:
            return ka == oa and float(os_) >= float(ks) and int(float(os_)) // 100 == int(float(ks)) // 100
        except ValueError:
            return False
    return False


def section_of(key):
    """'co-ccr:8-1404-1:4.109' -> 'co-ccr:8-1404-1'; 'const:amend-14:1' -> 'const:amend-14' (the provision's parent)"""
    p = key.split(":")
    return ":".join(p[:2]) if p[0] in ("co-ccr", "const", "co-const") and len(p) > 2 else key


_USC_CASE = None


def usc_section(title, sec):
    """A section number as the Code writes it: keys are lower case ('25f'), the Code has '25F' and '2000e'.
    From the identifiers in the US Code notes (md/, usc2md.py), read once; the key itself when there are none."""
    global _USC_CASE
    if not re.search(r"[a-z]", sec):
        return sec
    if _USC_CASE is None:
        _USC_CASE = {}
        try:
            from tribonian import tconfig
            for f in (tconfig.WORK / "md").rglob("*-usc-*.md"):
                if not re.search(r"[a-z]$", f.stem):
                    continue
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        m = re.match(r"identifier: /us/usc/t(\w+?)/s(\S+)", line)
                        if m:
                            _USC_CASE[(m.group(1).lower(), m.group(2).lower())] = m.group(2)
                            break
                        if line.startswith("aliases") or line.startswith("# "):
                            break
        except OSError:
            pass
    return _USC_CASE.get((title.lower(), sec.lower()), sec)


def label(key):
    """A readable citation for a key."""
    p = key.split(":")
    kind = p[0]
    if kind == "usc":
        return "%s U.S.C. § %s" % (p[1].upper(), usc_section(p[1], p[2]))
    if kind == "const":
        a, n = p[1].split("-")
        r = {v: k for k, v in ROMAN.items()}[int(n)]
        return "U.S. Const. %s %s" % ("amend." if a == "amend" else "art.", r) + (", § %s" % p[2] if len(p) > 2 else "")
    if kind == "co-const":
        n = int(p[1].split("-")[1])
        return "Colo. Const. art. %s" % {v: k for k, v in ROMAN.items()}[n] + (", § %s" % p[2] if len(p) > 2 else "")
    if kind == "cfr":
        return "%s C.F.R. pt. %s" % (p[1], p[2][3:]) if p[2].startswith("pt-") else "%s C.F.R. § %s" % (p[1], p[2])
    if kind == "co-crs":
        return "Colo. Rev. Stat. § %s" % p[1]
    if kind == "co-ccr":
        t, rule = p[1].split("-", 1)
        return "%s Colo. Code Regs. %s" % (t, rule) + (" § %s" % p[2] if len(p) > 2 else "")
    return key


def note_name(key):
    """The Obsidian note a key links to, where one is built: U.S.C. sections ('42-usc-1983'), C.F.R. sections
    and parts ('34-cfr-104.4', '34-cfr-pt-104'; cfr.py notes)."""
    p = key.split(":")
    if p[0] == "usc":
        return "%s-usc-%s" % (p[1], p[2])
    if p[0] == "cfr":
        return "%s-cfr-%s" % (p[1], p[2])
    # state law and the Constitution (statelaw.py notes): a C.R.S. section; a constitution's article or
    # amendment and a Colorado rule as one note each, whatever section the cite pins
    if p[0] == "co-crs":
        return "co-crs-%s" % p[1]
    if p[0] in ("co-const", "co-ccr"):
        return "%s-%s" % (p[0], p[1])
    if p[0] == "const":
        return "us-const-%s" % p[1]
    return None


ORDER = {"const": 0, "usc": 1, "cfr": 2, "co-const": 3, "co-crs": 4, "co-ccr": 5}


def sort_key(key):
    p = key.split(":")
    nums = [int(x) if x.isdigit() else x for x in re.split(r"[.:-]", ":".join(p[1:])) if x]
    return (ORDER.get(p[0], 9), [(0, x) if isinstance(x, int) else (1, x) for x in nums])


def spans_in(text):
    """Full and same-text short cites in one run of text, offsets into that text: [(start, end, key)]."""
    return find(text, flat=False)["spans"]
