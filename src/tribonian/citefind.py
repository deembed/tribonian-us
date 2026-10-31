"""citefind.py -- case citations in a text, by the engine the configuration names.

    cite_engine   CL_CITE_ENGINE   "patterns"   (patterns | eyecite | both)

  patterns  our own regular expressions over the reporter list in data/reporters.json (made from reporters-db by
            tools/make_reporters.py): full cites ("494 U.S. 872"), pin cites ("494 U.S. at 890", "494 U. S., at 890"),
            "Id." and "Name, supra". No dependency; the place to fix what our text gets wrong ("at6 2132").
  eyecite   Free Law Project's eyecite (pip install eyecite), which makes CourtListener's own markup.
  both      the two together: every patterns cite, plus eyecite's where patterns found nothing.

find(text) -> [Cite], in order. Resolving a cite to a case is not done here (scotus.resolve and the fallbacks);
an Id. or supra cite is resolved against the cites before it in the same document (Antecedents).
"""
import json, re, sys
from dataclasses import dataclass, field

from tribonian import tconfig

ENGINES = ("patterns", "eyecite", "both")


@dataclass
class Cite:
    start: int
    end: int
    kind: str                       # full | pin | id | supra
    vol: str = ""
    reps: tuple = ()                # the edition(s) the reporter spelling can mean, CourtListener's form first
    page: str = ""                  # the first page (full) or the pin page (pin, id, supra)
    name: str = ""                  # supra: the name before it
    text: str = ""

    @property
    def rep(self):
        return self.reps[0] if self.reps else ""


def engine():
    v = str(tconfig.get("cite_engine", "patterns") or "patterns").strip().lower()
    if v not in ENGINES:
        sys.exit("cite_engine %r: use one of %s (CL_CITE_ENGINE or the config file)" % (v, ", ".join(ENGINES)))
    return v


# ---------------------------------------------------------------- the reporter list
def _rep_rx(r):
    r"""'F. Supp. 2d' -> F\.\s?Supp\.\s?2d: a space or none wherever the spelling has one, after every period
    ("F. 4th" and "F.4th") and between letters and an edition ("AD 2d", "AD2d"); "Ohio St. 3d", "Ohio St.3d" and
    "OhioSt.3d" alike; any apostrophe."""
    out, s = "", " ".join(r.split())
    for k, ch in enumerate(s):
        if ch == " ":
            if not out.endswith(r"\s?"):
                out += r"\s?"
            continue
        out += {".": r"\.", "'": "['’]"}.get(ch, re.escape(ch))
        nxt = s[k + 1] if k + 1 < len(s) else ""
        if nxt and nxt != " " and (ch in ".," or (ch.isalpha() and nxt.isdigit())):
            out += r"\s?"
    return out


def _key(s):
    return re.sub(r"[\s.,'’]", "", s).lower()


def _load():
    d = json.loads((tconfig.PKG / "data" / "reporters.json").read_text(encoding="utf-8"))["spellings"]
    canon = {}
    for sp, eds in d.items():
        canon.setdefault(_key(sp), [])
        for e in eds:
            if e not in canon[_key(sp)]:
                canon[_key(sp)].append(e)
    rx = sorted({_rep_rx(sp) for sp in d}, key=len, reverse=True)
    return canon, "|".join(rx)


CANON, REP_RX = _load()


def editions(spelling):
    """'U. S.' -> ('U.S.',); 'B.R.' -> both editions it can mean; unknown -> (spelling,)."""
    return tuple(CANON.get(_key(spelling)) or [spelling])


# "494 U.S. 872"; "494 U.S. at 890" and "494 U. S., at 890" (a pin cite); "at6 2132": a stray mark the text layer
# glued onto "at" (Viramontes ECF 82) is still a pin; up to 8 digits: Westlaw numbers ("2022 WL 18145414")
# "5 U.S. (1 Cranch) 137": the early reporter's own volume in parentheses, between the U.S. volume and the page;
# the old style puts a comma or the state there too ("1 Utah, 226", "11 Leigh (Va.), 659")
# California writes "10 Cal.Rptr.3d at p. 296": "at p." / "at pp." is a pin too
CITE_RE = re.compile(r"(?<![\d.])(\d{1,4})\s+(%s)(?:\s*\(\d{1,3}\s+[A-Z][\w.]*\.?\)|\s*\([A-Z][\w. ]{0,10}\))?(?:,?\s+(at\d{0,2}\s+(?:pp?\.\s*)?)|,?\s+)(\d{1,8})(?![\da-zA-Z])"
                     % REP_RX)
ID_RE = re.compile(r"(?<![\w.])(?:[Ii]d|[Ii]bid)\.(?:,?\s+at\s+\*?(\d+))?")
SUPRA_RE = re.compile(r"((?:[A-Z][\w'’&.-]*)(?:\s+(?:of|the|and|for|in|&|v\.|[A-Z][\w'’&.-]*)){0,7}),?\s+supra\b"
                      r"(?:,?\s*(?:at|note)\s+\*?(\d+))?")


def _wordlike(spelling):
    """A reporter spelled as plain words, no period, digit or capitals-only ("Idaho", "Cranch", "App Div"): prose can
    look like a cite around it."""
    return "." not in spelling and not any(ch.isdigit() for ch in spelling) and not spelling.replace(" ", "").isupper()


def _patterns(text):
    out = []
    for m in CITE_RE.finditer(text):
        if _wordlike(m.group(2).strip()) and (int(m.group(1)) >= 1700 or re.match(r"\s+[a-z]", text[m.end():])):
            continue                            # "in 2004 Idaho 12 ways": a year, or words after the "page"
        if not m.group(3) and re.search(r",\s*\d+$", m.group(0)) and int(m.group(1)) >= 1700:
            continue                            # "In 1990 U.S., 45 states": the old comma style never has a year volume
        out.append(Cite(m.start(), m.end(), "pin" if m.group(3) else "full", m.group(1), editions(m.group(2)),
                        m.group(4), text=m.group(0)))
    for m in ID_RE.finditer(text):
        if re.match(r"\s*§", text[m.end():m.end() + 3]):
            continue                            # "Id. § 3": a statute's Id., not a case's
        out.append(Cite(m.start(), m.end(), "id", page=m.group(1) or "", text=m.group(0)))
    for m in SUPRA_RE.finditer(text):
        if re.search(r"\bnote\s+\*?\d+\s*$", m.group(0)):
            continue                            # "Walls and Zheng, supra note 18": an article in a footnote, not a case
        out.append(Cite(m.start(), m.end(), "supra", page=m.group(2) or "", name=m.group(1), text=m.group(0)))
    return out


# ---------------------------------------------------------------- eyecite
_EY = []


def _eyecite():
    if not _EY:
        try:
            import logging
            logging.getLogger("eyecite").setLevel(logging.ERROR)
            from eyecite import get_citations
            from eyecite import models
            _EY.append((get_citations, models))
        except ImportError:
            sys.exit("cite_engine %s needs eyecite: pip install eyecite (or set cite_engine = \"patterns\")" % engine())
    return _EY[0]


def _eyecite_cites(text):
    get_citations, M = _eyecite()
    if not text.strip():
        return []
    out = []
    for c in get_citations(text):
        a, b = c.span()
        g = getattr(c, "groups", {}) or {}
        if isinstance(c, M.FullCaseCitation) and str(g.get("page") or "").isdigit():
            rep = c.corrected_reporter() or g.get("reporter", "")
            out.append(Cite(a, b, "full", g.get("volume", ""), (rep,) + tuple(e for e in editions(rep) if e != rep), g["page"], text=text[a:b]))
        elif isinstance(c, M.ShortCaseCitation) and str(g.get("page") or "").isdigit():
            rep = c.corrected_reporter() or g.get("reporter", "")
            out.append(Cite(a, b, "pin", g.get("volume", ""), (rep,) + tuple(e for e in editions(rep) if e != rep), g["page"], text=text[a:b]))
        elif isinstance(c, M.IdCitation):
            pin = re.search(r"\d+", c.metadata.pin_cite or "")
            out.append(Cite(a, b, "id", page=pin.group(0) if pin else "", text=text[a:b]))
        elif isinstance(c, M.SupraCitation):
            pin = re.search(r"\d+", c.metadata.pin_cite or "")
            out.append(Cite(a, b, "supra", page=pin.group(0) if pin else "", name=c.metadata.antecedent_guess or "",
                            text=text[a:b]))
    return out


def find(text, how=None):
    """Every case cite in text, in order, by the configured engine (or how)."""
    how = how or engine()
    if how == "patterns":
        cs = _patterns(text)
    elif how == "eyecite":
        cs = _eyecite_cites(text)
    else:
        cs = _patterns(text)
        cs += [e for e in _eyecite_cites(text) if not any(e.start < c.end and c.start < e.end for c in cs)]
    cs.sort(key=lambda c: (c.start, -c.end))
    out = []
    for c in cs:                                # overlapping finds (an Id. inside a supra's name): the first, longest
        if out and c.start < out[-1].end:
            continue
        out.append(c)
    return out


# ---------------------------------------------------------------- Id. and supra
NAME_STOP = {"see", "also", "accord", "compare", "with", "but", "the", "and", "inc", "corp", "company", "co", "v",
             "united", "states", "state", "people", "county", "city", "dept", "department", "e", "g", "cf", "of", "in",
             "for"}


def name_words(s):
    return [w for w in re.findall(r"[a-z]{3,}", (s or "").lower()) if w not in NAME_STOP]


class Antecedents:
    """The cites before this point in one document: the last one (what an Id. means) and every full cite found,
    by its name (what a supra means). A statute cite in between breaks the Id. chain (law())."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.last = None                        # (target or None, why): the cite just before
        self.full = []                          # [(name words, target)]
        self.volume = {}                        # (volume, reporter) -> (target, first page): for a pin cite

    def cite(self, target, why="", before="", where=None):
        self.last = (target, why)
        if target:
            self.full.append((name_words(before[-160:].split(";")[-1]), target))
            if where:
                self.volume[where[:2]] = (target, where[2])

    def in_volume(self, vol, rep):
        """The case this document cited in full in that volume of that reporter: (target, first page) or None."""
        return self.volume.get((vol, rep))

    def law(self):
        self.last = None

    def id(self):
        """-> target, or None (nothing before it, a statute before it, or it was unresolved)."""
        return self.last[0] if self.last else None

    def supra(self, name):
        """The one earlier full cite whose name has every word of name."""
        words = name_words(name)
        if not words:
            return None
        hits = {t for ws, t in self.full if all(w in ws for w in words)}
        return hits.pop() if len(hits) == 1 else None


DOC = Antecedents()
