"""tree.py -- the common tree every parser family builds, and the numbering ladder that places labels.

A family reads a state's files into Nodes; writer.py turns Nodes into USLM; verify.py checks the result.
Families differ in how they find structure (HTML centering, Word styles, XML tags); the ladder logic is
shared, so a fix to label placement fixes every state.

Node.items keeps document order, which text conservation depends on:
    ("text", str)          law text: content / chapeau / continuation, decided by the writer
    ("node", Node)         a child level (container, section or numbered paragraph)
    ("note", tag, topic, [str, ...])   a note: sourceCredit, editorialNote, note (annotation, toc)
    ("cross", str)         an unnumbered heading among siblings (USLM crossHeading)
    ("unmapped", str)      text the family could not place: kept, marked, and reported
"""
import re

ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


class Node:
    __slots__ = ("tag", "num_text", "num", "heading", "ident", "items", "rank", "label")

    def __init__(self, tag, num_text="", num="", heading="", ident="", rank=99, label=""):
        self.tag, self.num_text, self.num, self.heading, self.ident, self.rank = tag, num_text, num, heading, ident, rank
        self.label = label or num          # as the ladder reads it: "a", "1.5", or "1." for a dotted label
        self.items = []

    def add(self, *item):
        self.items.append(item)

    def children(self):
        return [it[1] for it in self.items if it[0] == "node"]

    def last_text(self):
        for it in reversed(self.items):
            if it[0] == "text":
                return it[1]
            if it[0] == "node":
                return it[1].last_text()
        return ""


def roman_value(s):
    s = s.lower()
    if not s or any(ch not in ROMAN for ch in s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        v = ROMAN[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    # reject non-canonical forms ("iiii", "vx") so plain letters are not read as numerals
    return total if to_roman(total) == s else None


def to_roman(n):
    out = ""
    for v, r in ((100, "c"), (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out, n = out + r, n - v
    return out


def _split_frac(label):
    """'1.5' -> ('1', 5); 'a' -> ('a', 0). Insertion labels count as fractions too: 'a1' -> ('a', 1) (North
    Carolina's (a1) between (a) and (b)), '1a' -> ('1', 1) ((1a) between (1) and (2))."""
    base, dot, frac = label.partition(".")
    if dot:
        return (base, int(frac)) if frac.isdigit() else (None, None)
    m = re.fullmatch(r"([A-Za-z]+)-([ivxlc]{1,6})", label)     # DC's (B-i) (B-ii): an inserted roman after B
    if m and roman_value(m.group(2)):
        return m.group(1), roman_value(m.group(2))
    m = re.fullmatch(r"([A-Za-z0-9]+)-([a-zA-Z])", label)     # New Hampshire's II-a. between II. and III.;
    if m:                                                     # Maine's 1-A. and (1-A)
        return m.group(1), ord(m.group(2).lower()) - 96
    m = re.fullmatch(r"([A-Za-z]+)-(\d{1,2})", label)         # Maine's A-1. and (a-1)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r"([A-Za-z]+)(\d{1,2})", label)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r"(\d+)([a-zA-Z])", label)              # (8L) Wisconsin, 5A. Iowa: an inserted number
    if m:
        return m.group(1), ord(m.group(2).lower()) - 96
    return label, 0


def ordinal(kind, label):
    """A label's place in its sequence as (main, fraction), or None when it is not that kind.
    num: (1.5) -> (1, 5), (0.5) -> (0, 5); lower/upper: a -> 1 ... z -> 26, aa -> 27, bb -> 28; roman: iv -> 4.
    A kind ending in "." takes only dotted labels ("1.", "A." -- Maryland's deepest levels), and a plain kind
    only parenthesized ones, so (1) and 1. are never the same level."""
    if kind.endswith("."):
        if not (label.endswith(".") and len(label) > 1):
            return None
        kind, label = kind[:-1], label[:-1]
    elif label.endswith("."):
        return None
    base, frac = _split_frac(label)
    if base is None or not base:
        return None
    if kind == "num":
        return (int(base), frac) if base.isdigit() else None
    if kind in ("double_lower", "double_upper"):        # (aa) (bb) ... as a level of its own: aa -> 1, bb -> 2
        ok = base.islower() if kind == "double_lower" else base.isupper()
        if not (ok and base.isalpha() and len(base) == 2 and base[0] == base[1]):
            return None
        return (ord(base[0].lower()) - 96, frac)
    if kind in ("lower", "upper"):
        ok = base.islower() if kind == "lower" else base.isupper()
        if not (ok and base.isalpha() and len(set(base)) == 1 and len(base) <= 3):
            return None
        return ((len(base) - 1) * 26 + ord(base[0].lower()) - 96, frac)
    if kind in ("roman_lower", "roman_upper"):
        ok = base.islower() if kind == "roman_lower" else base.isupper()
        v = roman_value(base) if ok else None
        return (v, frac) if v else None
    raise ValueError("unknown label type %r" % kind)


def level_ordinal(types, label):
    """The label's ordinal under the first of a level's types that fits it, or None."""
    for t in ([types] if isinstance(types, str) else types):
        o = ordinal(t, label)
        if o is not None:
            return o
    return None


def is_first(o):
    return o[0] <= 1                       # (1), (a), (I) -- or an inserted (0.5) ahead of them


def follows(prev, o):
    """o can come after its sibling prev: later in order, skipping at most one whole number (a gap
    from a repealed paragraph is common; more than one is suspicious and reported by verify)."""
    return o > prev and o[0] <= prev[0] + 2


LABEL = re.compile(r"\[?\(([0-9]+(?:\.[0-9]+|[a-zA-Z]|-[A-Za-z])?|[A-Za-z]{1,8}(?:\.[0-9]+|-?[0-9]{1,2}|-[a-z]{1,6})?)\)\]?\s*")   # [(d)]: a label the revisor inserted (Hawaii)
# A dotted label ends at a space, the line's end, or a chained label: Florida's "1.a." / "2.a.(I)" open a level
# and its first item together. Only a lowercase or numeric dotted label, or "(", may follow (not "U.S.").
DOTTED = re.compile(r"([0-9]+(?:-[A-Za-z]|[A-Z])?\.|[IVXLC]{1,6}(?:-[a-z])?\.|[A-Z]{1,2}(?:-[0-9]{1,2})?\.|[a-z]{1,2}\.)"
                    r"(?=\s|$|\(|[a-z]{1,2}\.(?:\s|\(|$)|[0-9]{1,3}\.(?:\s|\(|$))\s*")   # 1. A. a. IV. (MD, DE, NH)


class Ladder:
    """Places paragraph labels on levels. types: one label type per level, outermost first.

    For each label, every level whose type fits it is a candidate. A candidate is plausible when the label
    continues the open sibling at that level, or opens the first child one level below the current one.
    Preference: continuing a sibling over opening a child (so (i) after (h) stays a letter), deepest first
    (so (I) after (H) is a letter, not roman one) -- unless the paragraph before ends with a colon or dash,
    which announces children. Anything decided between two plausible levels is logged in .ambiguous;
    a label no level fits in sequence is placed on its nearest level and logged in .anomalies."""

    def __init__(self, types):
        # one entry per level; an entry may list several types: ["roman_upper", "roman_lower"]
        self.types = [list(t) if isinstance(t, (list, tuple)) else [t] for t in types]
        self.dotted = any(t.endswith(".") for ts in self.types for t in ts)
        self.ambiguous, self.anomalies, self.versions = [], [], []

    def place(self, label, open_labels, prev_text, where="", hint=None):
        """open_labels: the label of the open node at each depth, outermost first (None where nothing is open:
        a section that starts at (a) has nothing at depth 0). Every type a level allows is tried, each against
        the open label read as that same type -- so a level of [letters, romans] can open with roman (i).
        -> depth, or None when the text in parentheses is not a label of any level."""
        cands = []                             # (depth, ordinal, type)
        for d, ts in enumerate(self.types):
            for t in ts:
                o = ordinal(t, label)
                if o is not None:
                    cands.append((d, o, t))
        if not cands:
            return None
        k = len(open_labels) - 1               # current depth (-1: none open)
        prev = lambda d, t: ordinal(t, open_labels[d]) if d <= k and open_labels[d] is not None else None
        sib = sorted({(d, o) for d, o, t in cands if prev(d, t) is not None and follows(prev(d, t), o)}, reverse=True)
        child = sorted({(d, o) for d, o, t in cands if d == k + 1 and is_first(o)})
        if k < 0 and not child:            # nothing open: a section may start lower on the ladder, e.g. at (a)
            child = sorted({(d, o) for d, o, t in cands if is_first(o)})[:1]
        announce = prev_text.rstrip().endswith((":", "\u2014", "--"))
        order = (child + sib) if announce else (sib + child)
        depths = list(dict.fromkeys(d for d, _ in order))
        if hint is not None and len(depths) > 1 and hint in depths:
            if depths[0] != hint:
                self.ambiguous.append("%s (%s): levels %s, indentation chose %d" % (where, label, depths, hint))
            return hint
        if hint is not None and not depths and any(d == hint for d, _, _ in cands):
            self.anomalies.append("%s (%s): out of sequence; placed at depth %d by its indentation" % (where, label, hint))
            return hint
        if depths:
            if len(depths) > 1:
                self.ambiguous.append("%s (%s): levels %s, chose %d" % (where, label, depths, depths[0]))
            return depths[0]
        same = [d for d, o, t in cands if prev(d, t) == o]       # a label repeated: keep it a sibling
        d = same[-1] if same else min(cands, key=lambda c: abs(c[0] - (k + 1)))[0]
        self.anomalies.append("%s (%s): out of sequence at depth %d" % (where, label, d))
        return d


def split_labels(text, dotted=False):
    """'(3) (a) In appointing ...' -> (['3', 'a'], ['(3) ', '(a) '], 'In appointing ...').
    With dotted, '1. A. Text' -> (['1.', 'A.'], ...) as well."""
    labels, raws, rest = [], [], text
    while True:
        m = LABEL.match(rest) or (DOTTED.match(rest) if dotted else None)
        if not m:
            return labels, raws, rest
        labels.append(m.group(1))
        raws.append(m.group(0))
        rest = rest[m.end():]
