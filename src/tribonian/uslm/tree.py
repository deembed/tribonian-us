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
    """'1.5' -> ('1', 5); 'a' -> ('a', 0)."""
    base, dot, frac = label.partition(".")
    if dot and not frac.isdigit():
        return None, None
    return base, int(frac) if dot else 0


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


LABEL = re.compile(r"\(([0-9]+(?:\.[0-9]+)?|[A-Za-z]{1,6}(?:\.[0-9]+)?)\)\s*")
DOTTED = re.compile(r"([0-9]+\.|[A-Z]{1,2}\.)(?=\s)\s*")


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

    def place(self, label, open_ords, prev_text, where="", hint=None):
        """open_ords: the ordinal of the open node at each depth, outermost first (None where no node is
        open: a section that starts at (a) has nothing at depth 0). -> depth, or None when the text in
        parentheses is not a label of any level."""
        cands = [(d, level_ordinal(ts, label)) for d, ts in enumerate(self.types)]
        cands = [(d, o) for d, o in cands if o is not None]
        if not cands:
            return None
        k = len(open_ords) - 1                 # current depth (-1: none open)
        sib = [(d, o) for d, o in cands if d <= k and open_ords[d] is not None and follows(open_ords[d], o)]
        child = [(d, o) for d, o in cands if d == k + 1 and is_first(o)]
        if k < 0 and not child:            # nothing open: a section may start lower on the ladder, e.g. at (a)
            child = [(d, o) for d, o in cands if is_first(o)][:1]
        announce = prev_text.rstrip().endswith((":", "—", "--"))
        order = (child + sorted(sib, reverse=True)) if announce else (sorted(sib, reverse=True) + child)
        if hint is not None and len(order) > 1 and any(d == hint for d, _ in order):
            if order[0][0] != hint:
                self.ambiguous.append("%s (%s): levels %s, indentation chose %d" % (where, label, [d for d, _ in order], hint))
            return hint
        if hint is not None and not order and any(d == hint for d, _ in cands):
            self.anomalies.append("%s (%s): out of sequence; placed at depth %d by its indentation" % (where, label, hint))
            return hint
        if order:
            if len(order) > 1:
                self.ambiguous.append("%s (%s): levels %s, chose %d" % (where, label, [d for d, _ in order], order[0][0]))
            return order[0][0]
        same = [c for c in cands if c[0] <= k and open_ords[c[0]] == c[1]]     # a label repeated: keep it a sibling
        d = (same or [min(cands, key=lambda c: abs(c[0] - (k + 1)))])[-1][0]
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
