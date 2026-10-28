"""statecites.py -- state statute, constitution and regulation cites, read from data: each formula's [cite] table.

A formula ([TX.codes] in data/formulas/TX.toml) gets a sub-table [TX.codes.cite] that says how the code is cited
and how a cite names the item (title, chapter, code) its text is fetched and stored by. A code with no source we
can use (Purdon's Pennsylvania Statutes, a LexisNexis-only state) gets a cite-only table: [PA.ps] with no
`fetch`, just `name`, `source_error` and [PA.ps.cite]: its cites are recognized and the appendix says why
there is no text.

[ST.code.cite] keys (regular expressions are Python's; write them in single quotes):
    full     forms that name the state: 'Tex\\.\\s*{sub}\\s*Code(?:\\s*Ann\\.)?'. The section follows
             (optional §, "sec.", "section", "art."), unless the form places it: with {section}, or its own
             (?P<sec>...) / (?P<art>...) group. Other named groups ((?P<vol>...), (?P<tit>...)) are kept for
             the key and item templates and do not stop the section being appended.
    bare     forms that do not name the state ('{sub}\\s+Code'): taken only when the case is from that state
             (its court; a circuit's states, the one the text names most first) or, with no court known, the
             state is the one the text names most.
    after    forms written after the number: "§ 26.5-4-205, C.R.S." (a § or "section" is required before it).
    section  the section number's shape: '\\d+[A-Z]?\\.\\d+[A-Za-z]*'.
    sub      { GV = ["Gov(?:'|’)?t\\.?", "Government"], ... }: codes inside the code (Texas's 30 codes, New
             York's consolidated laws); {sub} in a form matches any of them and the key keeps which.
    names    { GV = "Gov't" }: how the label writes each sub (default: the sub's code).
    key      the key after "<st>-<code>:"; default "{sub}/{sec}" with subs, else "{sec}". [..] is kept only when
             every field inside it matched: "{vol}-{rule}[:{sec}]".
    item     how a cite names its fetch item: a template ("{sub}", "{art}") or [regex on the section, template]
             (['^(\\d+[A-Z]?)\\.', '{sub}.{1}']: Gov't Code 311.001 -> GV.311). Default: the formula's single item.
    label    the readable cite: "Tex. {name} Code Ann. § {sec}".
    tail     constitutions only: what follows the name; the default reads "art. IX, § 7" (Roman or Arabic).
    also     other formulas holding the same items (["ilcsweb"]): the text is read from whichever has it.
    num      how the built USLM numbers the section, when it is not the cite's {sec}: "{tit}-{sec}" (Oklahoma's
             <num value="21-701.7">), "{roman}-{sec}" (Nebraska's constitution, "I-1"). A template with {art} or
             {roman} identifies the article itself, so a constitution's article is not matched separately.
    ident    text the section's identifier must contain when a number repeats in one file: "/act{act}/" (Illinois:
             740 ILCS 14/15 is "Sec. 15" of act 14, not of the other acts in chapter 740).
    examples [["Tex. Gov't Code Ann. § 311.001", "tx-codes:GV/311.001", "Tex. Gov't Code Ann. § 311.001"], ...]:
             cite text, the key it must give, optionally the label; `tribonian state cites TX` checks them.
Constitution formulas (code "const", or kind = "const") key as "art-<n>[:<sec>]".

The prior for bare forms: the court's state (courts.full_name in the CourtListener database: "District Court,
N.D. California"; a circuit gives its states) and the states the text names ("California", "Cal."), most first.
"""
import re
import sys
from collections import Counter

SUBS = r"(?:\s?\((?!(?:1[89]|20)\d\d\))[A-Za-z0-9]{1,4}\))*"     # (2)(a), not a year: "(2023)" is an edition
SEC_WORD = r"(?:§§?|[Ss]ec(?:tion)?s?\.|[Ss]ections?|[Aa]rts?\.|[Aa]rticles?)"
ROMAN_RX = r"[IVXLC]+"
CONST_TAIL = (r",?\s*(?:[Aa]rt(?:icle)?\.?)\s*(?P<art>[IVXLC]+|\d+[A-Za-z]?)\b"
              r"(?:,?\s*(?:§§?|[Ss]ec(?:tion)?s?\.?)\s*(?P<sec>\d+[a-z]?(?:[.-]\d+[a-z]?)?))?" + SUBS)
EDITION_RE = re.compile(r"\s*\((?:(?P<pub>[A-Z][A-Za-z.&'’ ]{0,40}?)\s)?(?:Supp\.\s*)?(?P<yr>(?:1[89]|20)\d\d)(?:\s*&\s*Supp\.\s*\d{4})?\)")

STATES = {   # postal: (name, Bluebook abbreviation)
    "AL": ("Alabama", "Ala."), "AK": ("Alaska", "Alaska"), "AZ": ("Arizona", "Ariz."), "AR": ("Arkansas", "Ark."),
    "CA": ("California", "Cal."), "CO": ("Colorado", "Colo."), "CT": ("Connecticut", "Conn."), "DE": ("Delaware", "Del."),
    "DC": ("District of Columbia", "D.C."), "FL": ("Florida", "Fla."), "GA": ("Georgia", "Ga."), "HI": ("Hawaii", "Haw."),
    "ID": ("Idaho", "Idaho"), "IL": ("Illinois", "Ill."), "IN": ("Indiana", "Ind."), "IA": ("Iowa", "Iowa"),
    "KS": ("Kansas", "Kan."), "KY": ("Kentucky", "Ky."), "LA": ("Louisiana", "La."), "ME": ("Maine", "Me."),
    "MD": ("Maryland", "Md."), "MA": ("Massachusetts", "Mass."), "MI": ("Michigan", "Mich."), "MN": ("Minnesota", "Minn."),
    "MS": ("Mississippi", "Miss."), "MO": ("Missouri", "Mo."), "MT": ("Montana", "Mont."), "NE": ("Nebraska", "Neb."),
    "NV": ("Nevada", "Nev."), "NH": ("New Hampshire", "N.H."), "NJ": ("New Jersey", "N.J."), "NM": ("New Mexico", "N.M."),
    "NY": ("New York", "N.Y."), "NC": ("North Carolina", "N.C."), "ND": ("North Dakota", "N.D."), "OH": ("Ohio", "Ohio"),
    "OK": ("Oklahoma", "Okla."), "OR": ("Oregon", "Or."), "PA": ("Pennsylvania", "Pa."), "RI": ("Rhode Island", "R.I."),
    "SC": ("South Carolina", "S.C."), "SD": ("South Dakota", "S.D."), "TN": ("Tennessee", "Tenn."), "TX": ("Texas", "Tex."),
    "UT": ("Utah", "Utah"), "VT": ("Vermont", "Vt."), "VA": ("Virginia", "Va."), "WA": ("Washington", "Wash."),
    "WV": ("West Virginia", "W. Va."), "WI": ("Wisconsin", "Wis."), "WY": ("Wyoming", "Wyo."),
}
CIRCUITS = {"ca1": "ME NH MA RI", "ca2": "NY VT CT", "ca3": "PA NJ DE", "ca4": "MD VA WV NC SC", "ca5": "TX LA MS",
            "ca6": "OH MI KY TN", "ca7": "IL IN WI", "ca8": "MN IA MO AR NE ND SD", "ca9": "CA OR WA AZ NV ID MT AK HI",
            "ca10": "CO KS NM OK UT WY", "ca11": "FL GA AL", "cadc": "DC"}
# "West Virginia" before "Virginia", "New Mexico" before "Mexico" ...: longest names first, and a name inside a
# longer one is not counted twice (the longer match takes the span)
_NAME_RE = None


def _roman(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    s = s.upper()
    if not s or any(ch not in vals for ch in s):
        return None
    n = 0
    for a, b in zip(s, s[1:] + " "):
        n += -vals[a] if b in vals and vals[b] > vals[a] else vals[a]
    return n


def arabic(s):
    """'IX' -> '9', '12' -> '12', '1-A' kept."""
    s = str(s).strip().rstrip(".")
    if re.fullmatch(ROMAN_RX, s.upper()) and s.upper() == s:
        return str(_roman(s))
    return s


class Table:
    def __init__(self, st, code, f, c):
        self.st, self.code, self.f, self.c = st, code, f, c
        self.prefix = "%s-%s" % (st.lower(), code)
        self.kind = f.get("kind") or ("const" if code == "const" or code.endswith("const") else "statute")
        self.source_error = f.get("source_error") or c.get("source_error")
        self.subs = list((c.get("sub") or {}).items())                     # [(code, [patterns])]
        self.names = c.get("names") or {}
        self.section = c.get("section", r"\d+[a-z]?(?:\.\d+)?" if self.kind == "const" else r"\d+[A-Za-z]?(?:[.-]\d+[A-Za-z]?)*")
        self.key_t = c.get("key") or ("art-{art}[:{sec}]" if self.kind == "const" else "{sub}/{sec}" if self.subs else "{sec}")
        self.item = c.get("item")
        self.label_t = c.get("label")
        self.also = list(c.get("also") or [])
        self.num_t = c.get("num")
        self.ident_t = c.get("ident")
        self.forms = []                                                     # (compiled, bare, after)
        for which in ("full", "bare", "after"):
            for form in _list(c.get(which)):
                try:
                    self.forms.append((self._compile(form, which == "after"), which == "bare", which == "after"))
                except re.error as e:
                    print("cite table %s.%s: bad %s form %r: %s" % (st, code, which, form, e), file=sys.stderr)
        self.key_re = _template_re(self.key_t)

    def _compile(self, form, after):
        rx = form
        if "{sub}" in rx:
            alts = "|".join("(?P<s%d>%s)" % (i, "|".join(_list(p))) for i, (_, p) in enumerate(self.subs))
            rx = rx.replace("{sub}", "(?:%s)" % alts)
        sec = "(?P<sec>%s)" % self.section
        if "{section}" in rx:
            rx = rx.replace("{section}", sec) + SUBS
        elif "{tail}" in rx:
            rx = rx.replace("{tail}", self.c.get("tail", CONST_TAIL))
        elif "(?P<sec>" in rx or "(?P<art>" in rx:              # the form places the section itself
            pass
        elif self.kind == "const":
            rx = rx + self.c.get("tail", CONST_TAIL)
        elif after:
            rx = SEC_WORD + r"\s*" + sec + SUBS + r",?\s*(?:" + rx + ")"
        else:
            rx = "(?:" + rx + r"),?\s*(?:" + SEC_WORD + r"\s*)?" + sec + r"(?![\w])" + SUBS
        return re.compile(r"(?<![\w.])" + rx)

    def vals(self, m):
        g = {k: v for k, v in m.groupdict().items() if v is not None}
        out = {k: v for k, v in g.items() if not re.fullmatch(r"s\d+", k)}
        for i, (code, _) in enumerate(self.subs):
            if ("s%d" % i) in g:
                out["sub"] = code
                break
        if "sec" in out:
            out["sec"] = out["sec"].rstrip(".")
        if "art" in out:
            out["art"] = arabic(out["art"])
        return out

    def key(self, vals):
        body = fill(self.key_t, vals)
        return "%s:%s" % (self.prefix, body) if body is not None else None

    def parse(self, key):
        """'tx-codes:GV/311.001' -> {'sub': 'GV', 'sec': '311.001'}, or None."""
        body = key.split(":", 1)[1] if ":" in key else ""
        m = self.key_re.fullmatch(body)
        return {k: v for k, v in m.groupdict().items() if v} if m else None

    def fetch_code(self):
        """The formula that fetches this code's items: its own, else the first of `also` that has a formula."""
        from tribonian import statelaw
        for code in [self.code] + self.also:
            if (self.st, code) in statelaw.formulas():
                return code
        return None

    def item_of(self, vals):
        rule = self.item
        vals = with_roman(vals)
        if rule is None:
            items = self.f.get("items") or []
            return str(items[0]) if len(items) == 1 else None
        if isinstance(rule, list):
            m = re.match(rule[0], vals.get("sec", ""))
            if not m:
                return None
            v = dict(vals)
            v.update({"_g%d" % i: x for i, x in enumerate(m.groups(), 1) if x is not None})
            v.update({k: x for k, x in m.groupdict().items() if x is not None})
            return fill(re.sub(r"\{(\d+)\}", r"{_g\1}", rule[1]), v)
        return fill(rule, vals)

    def label(self, vals):
        v = dict(vals)
        if "sub" in v:
            v["name"] = self.names.get(v["sub"], v["sub"])
        if "art" in v:
            try:
                v["roman"] = _to_roman(int(v["art"]))
            except ValueError:
                v["roman"] = v["art"]
        t = self.label_t
        if not t:
            if self.kind == "const":
                t = STATES.get(self.st, (self.st, self.st))[1] + " Const. art. {roman}[, § {sec}]"
            else:
                t = "%s %s[ {name}] § {sec}" % (STATES.get(self.st, (self.st, self.st))[1], self.f.get("name", self.code))
        return fill(t, v) or ""


def with_roman(vals):
    """vals plus 'roman' (IX) when there is an arabic article: for item and num templates ("{roman}")."""
    v = dict(vals)
    if "art" in v and "roman" not in v:
        try:
            v["roman"] = _to_roman(int(v["art"]))
        except ValueError:
            v["roman"] = v["art"]
    return v


def _to_roman(n):
    out = ""
    for v, s in ((100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out, n = out + s, n - v
    return out


def _list(x):
    return [] if x is None else [x] if isinstance(x, str) else list(x)


def fill(t, vals):
    """'{vol}-{rule}[:{sec}]' with vals; a [..] part is dropped when a field in it is missing. None if a field
    outside brackets is missing."""
    t = re.sub(r"\[([^\[\]]*)\]", lambda m: m.group(1) if all(k in vals for k in re.findall(r"\{(\w+)\}", m.group(1))) else "", t)
    try:
        return t.format_map(vals)
    except (KeyError, IndexError, ValueError):
        return None


def _template_re(t):
    """'{sub}/{sec}' -> a regex with named groups that reads a key body back."""
    out, i = [], 0
    for m in re.finditer(r"\[([^\[\]]*)\]|\{(\w+)\}", t):
        out.append(re.escape(t[i:m.start()]))
        if m.group(1) is not None:
            out.append("(?:%s)?" % _template_re(m.group(1)).pattern)
        else:
            out.append("(?P<%s>.+?)" % m.group(2))
        i = m.end()
    out.append(re.escape(t[i:]))
    return re.compile("".join(out))


_TABLES = None


def tables():
    """[Table] for every formula and cite-only code that has a [cite] table."""
    global _TABLES
    if _TABLES is None:
        _TABLES = []
        try:
            from tribonian import statelaw
            every = dict(statelaw.formulas())
            every.update(statelaw.cite_only())
        except (ImportError, SystemExit) as e:          # no tomllib/tomli, or a broken formula file: no state cites
            print("state cites off: %s" % e, file=sys.stderr)
            return _TABLES
        for (st, code), f in sorted(every.items()):
            if isinstance(f.get("cite"), dict):
                _TABLES.append(Table(st, code, f, f["cite"]))
    return _TABLES


def table(prefix):
    for t in tables():
        if t.prefix == prefix:
            return t
    return None


def is_state(key):
    k = key.split(":", 1)[0]
    return "-" in k and table(k) is not None


def parse(key):
    """key -> (Table, vals) or (None, None)."""
    t = table(key.split(":", 1)[0])
    if t is None:
        return None, None
    return t, t.parse(key)


# ---------------------------------------------------------------- the case's states

def states_named(text):
    """Counter postal -> how often the text names the state ("California", "Cal.")."""
    global _NAME_RE
    if _NAME_RE is None:
        alts = []
        for st, (name, abbr) in STATES.items():
            alts.append((name, st))
            if abbr != name:
                alts.append((abbr, st))
        alts.sort(key=lambda x: -len(x[0]))
        _NAME_RE = (re.compile(r"(?<![\w.])(%s)(?![a-z])" % "|".join(re.escape(a) for a, _ in alts)), {a: s for a, s in alts})
    rx, back = _NAME_RE
    return Counter(back[m.group(1)] for m in rx.finditer(text))


def court_states(court_id, con=None):
    """A court id -> its state(s): a circuit's states; else the state named in the court's full name."""
    if not court_id:
        return []
    if court_id in CIRCUITS:
        return CIRCUITS[court_id].split()
    name = ""
    try:
        if con is None:
            from tribonian import case2md
            con = case2md.local_db()
        row = con.execute("SELECT full_name FROM courts WHERE id=?", (court_id,)).fetchone()
        name = row[0] if row and row[0] else ""
    except Exception:
        pass
    got = states_named(name)
    if got:
        return [got.most_common(1)[0][0]]
    m = re.match(r"([a-z]{2})(?:[nsewcm]|mb|nb|sb|eb|wb|cb)?[db]$", court_id)    # cand, nysd, txnb: district, bankruptcy
    if m and m.group(1).upper() in STATES:
        return [m.group(1).upper()]
    if court_id in FEDERAL_IDS or court_id.startswith("bap"):
        return []
    for pre in sorted(STATE_COURT_PREFIX, key=len, reverse=True):   # no database: CourtListener's state court ids
        if court_id.startswith(pre):
            return [STATE_COURT_PREFIX[pre]]
    return []


FEDERAL_IDS = {"scotus", "uscfc", "cit", "tax", "jpml", "armfor", "cavc", "cafc", "fiscr", "fisc", "bia", "olc", "ag",
               "mspb", "asbca", "nmcca", "acca", "afcca", "cgcoca", "uscgcoca", "bva", "cc", "ccpa", "cusc", "eca", "tecoa"}
# CourtListener's state court ids start with these ("cal", "calctapp", "texapp", "nyappdiv", "mass", "massappct")
STATE_COURT_PREFIX = {
    "ala": "AL", "alaska": "AK", "ariz": "AZ", "ark": "AR", "cal": "CA", "colo": "CO", "conn": "CT", "del": "DE",
    "dc": "DC", "fla": "FL", "ga": "GA", "haw": "HI", "idaho": "ID", "ill": "IL", "ind": "IN", "iowa": "IA", "kan": "KS",
    "ky": "KY", "la": "LA", "me": "ME", "md": "MD", "mass": "MA", "mich": "MI", "minn": "MN", "miss": "MS", "mo": "MO",
    "mont": "MT", "neb": "NE", "nev": "NV", "nh": "NH", "nj": "NJ", "nm": "NM", "ny": "NY", "nc": "NC", "nd": "ND",
    "ohio": "OH", "okla": "OK", "or": "OR", "pa": "PA", "ri": "RI", "sc": "SC", "sd": "SD", "tenn": "TN", "tex": "TX",
    "utah": "UT", "vt": "VT", "va": "VA", "wash": "WA", "wva": "WV", "wis": "WI", "wyo": "WY"}


ORDINAL_CIRCUIT = {w: "ca%d" % i for i, w in enumerate("First Second Third Fourth Fifth Sixth Seventh Eighth Ninth Tenth Eleventh".split(), 1)}


def court_name_states(name):
    """A court's name as a docket writes it ("United States Court of Appeals for the Ninth Circuit", "Supreme Court of
    Colorado") -> its state(s)."""
    m = re.search(r"\b(%s|D\.\s?C\.)\s+Circuit" % "|".join(ORDINAL_CIRCUIT), name or "")
    if m:
        return CIRCUITS["cadc" if m.group(1).startswith("D") else ORDINAL_CIRCUIT[m.group(1)]].split()
    got = states_named(name or "")
    return [got.most_common(1)[0][0]] if got else []


CONTEXT = {"states": []}        # set by a build: the case's court states (lawcites.find adds what the text names)


def prior(text):
    """The states bare forms may mean, most likely first: the court's (CONTEXT), or with no court known the state
    the text names most (ties: all of them). A state merely mentioned is not enough: "Wash." in a California
    opinion's case cites must not make "Const. art. I, § 4" Washington's."""
    court = list(CONTEXT.get("states") or [])
    named = states_named(text).most_common()
    if court:
        return sorted(court, key=lambda st: -dict(named).get(st, 0))
    return [st for st, n in named if n == named[0][1]] if named else []


# ---------------------------------------------------------------- finding cites

def find_full(t, free):
    """Full cites (state named) and, where the prior allows, bare ones. -> [(start, end, key, table, vals)]
    in no order; `free(a, b)` says a span is not taken yet (the caller marks what it keeps)."""
    out, bare = [], []
    for tb in tables():
        for rx, is_bare, after in tb.forms:
            for m in rx.finditer(t):
                v = tb.vals(m)
                key = tb.key(v)
                if not key:
                    continue
                (bare if is_bare else out).append((m.start(), m.end(), key, tb, v))
                if ("§§" in m.group(0) or re.search(r"[Ss]ections", m.group(0))) and "sec" in v:
                    (bare if is_bare else out).extend(_more(t, m.end(), tb, v))   # "§§ 12940, 12941"
    if bare:
        order = prior(t)
        rank = {st: i for i, st in enumerate(order)}
        states_named("")                                   # builds _NAME_RE
        named_before = re.compile(r"(?:%s)\s*$" % _NAME_RE[0].pattern[len(r"(?<![\w.])"):-len(r"(?![a-z])")])
        # "Cal. Penal Code § 187": the bare "Penal Code" form of another state must not take a cite that names its state
        bare = [b for b in bare if b[3].st in rank and not named_before.search(t[max(0, b[0] - 24):b[0]])]
        bare.sort(key=lambda b: (b[0], rank[b[3].st], -(b[1] - b[0])))
        out += bare
    return out


def _more(t, at, tb, v):
    rx = re.compile(r"\s*(?:,|,?\s*and|,?\s*&|,?\s*or)\s*(?P<sec>%s)(?![\w])%s" % (tb.section, SUBS))
    while True:
        m = rx.match(t, at)
        if not m:
            return
        w = dict(v, sec=m.group("sec").rstrip("."))
        key = tb.key(w)
        if key:
            yield (m.start("sec"), m.end(), key, tb, w)
        at = m.end()


def short_key(tb, vals, sec):
    """A bare "§ N" after a full cite of this code: the same code (and sub), section N, if N has the code's shape."""
    if not re.fullmatch(tb.section, sec):
        return None
    return tb.key(dict(vals, sec=sec))


def edition_after(t, end):
    """'(West 2019)' right after a cite -> ('West', '2019'), else None."""
    m = EDITION_RE.match(t, end)
    return (m.group("pub") or "", m.group("yr")) if m else None
