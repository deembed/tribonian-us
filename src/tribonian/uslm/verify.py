"""verify.py -- prove a USLM conversion lost nothing, instead of trusting the parser.

Checks, in order of weight:
    conservation   the source's text and the USLM's text, all whitespace removed, are the same string.
                   The source side is read straight from the raw file (not from the family's paragraphs),
                   so a paragraph the family skipped, duplicated or reordered fails here.
    schema         valid against USLM 1.0.15 (data/uslm/), the schema the US Code XML declares
    unmapped       text kept but not placed (role="unmapped"): counted, first few shown
    numbering      siblings in order: (1) (1.5) (2) ..., at most one whole number skipped
    identifiers    every identifier unique
plus the ladder's log: labels placed by choosing between two plausible levels, and labels out of sequence.
A build passes when conservation and schema pass; the rest is a list of places to look.
"""
import re
from pathlib import Path

from lxml import etree

from tribonian.uslm.tree import level_ordinal
from tribonian.uslm.writer import NS

SCHEMA = Path(__file__).resolve().parent.parent / "data" / "uslm" / "USLM-1.0.15.xsd"
_schema = None


COVERAGE_MIN = 0.98      # parsed text / raw visible text; below this the HTML parser lost part of the file


def schema():
    global _schema
    if _schema is None:
        _schema = etree.XMLSchema(etree.parse(str(SCHEMA)))
    return _schema


def squash(s):
    return re.sub(r"\s+", "", s)


def main_text(tree):
    main = tree.getroot().find("{%s}main" % NS)
    return " ".join(main.itertext()) if main is not None else ""


def conservation(source_text, tree):
    a, b = squash(source_text), squash(main_text(tree))
    if a == b:
        return {"ok": True, "chars": len(a)}
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    return {"ok": False, "source_chars": len(a), "uslm_chars": len(b), "first_difference": i,
            "source_at": a[max(0, i - 80):i + 80], "uslm_at": b[max(0, i - 80):i + 80]}


def _local(el):
    return etree.QName(el).localname


def numbering(tree, ladder_types, ladder_tags, decimal=False):
    """Out-of-order siblings: numbered paragraphs by the ladder type of their tag; sections by the
    numbers in their number (26.5-1-101 -> (26, 5, 1, 101))."""
    by_tag = dict(zip(ladder_tags, ladder_types))
    out = []
    for parent in tree.getroot().iter():
        if not isinstance(parent.tag, str):
            continue
        prev = {}
        for ch in parent:
            if not isinstance(ch.tag, str):
                continue
            tag = _local(ch)
            num = ch.find("{%s}num" % NS)
            value = num.get("value") if num is not None else None
            if not value:
                continue
            if tag in by_tag:
                o = level_ordinal(by_tag[tag], value)
                if o is None:                       # a dotted level stores "1" for the label "1."
                    o = level_ordinal(by_tag[tag], value + ".")
            elif tag == "section":
                o = section_key(value, decimal)
            else:
                continue
            if o is None:
                out.append("%s: (%s) is not a %s label" % (ch.get("identifier"), value, by_tag.get(tag)))
                continue
            p = prev.get(tag)
            if (ch.get("identifier") or "").rsplit("_v", 1)[-1].isdigit() and p is not None and o == p:
                continue               # another version of the same paragraph (effective-date variants)
            if p is not None and not o > p:
                out.append("%s: %s comes after %s" % (ch.get("identifier"), value, p))
            elif p is not None and tag in by_tag and o[0] > p[0] + 2:
                out.append("%s: jumps from %s to %s" % (ch.get("identifier"), p, value))
            prev[tag] = o
    return out


def section_key(num, decimal=False):
    """Section numbers in order: 1-1 < 1-1a < 1-1z < 1-1aa < 1-2 < 1-10; 26.5-1-101; "1-2d_to_1-2y" by its first
    number. Each run of digits with its letter suffix is (number, letters as base 26). With decimal
    ([sections].decimal, Nevada's NRS), digits after a point are a decimal fraction: 1.4253 < 1.426."""
    first = num.split("_")[0].lower()
    if decimal:
        first = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(8, "0"), first)   # fixed width: compare as fractions
    key = []
    for d, letters in re.findall(r"(\d+)([a-z]*)", first):
        n = 0
        for ch in letters:
            n = n * 26 + ord(ch) - 96
        key.append((int(d), n))
    return tuple(key)


def identifiers(tree):
    seen, dup = set(), []
    for el in tree.getroot().iter("{%s}*" % NS):
        if el is tree.getroot():
            continue                   # the document shares its identifier with its title, as in the US Code
        i = el.get("identifier") if isinstance(el.tag, str) else None
        if i:
            if i in seen:
                dup.append(i)
            seen.add(i)
    return dup


def unmapped(tree):
    return [" ".join(" ".join(el.itertext()).split())[:160] for el in tree.getroot().iter("{%s}note" % NS) if el.get("role") == "unmapped"]


def verify(source_text, tree, ladder, formula):
    s = schema()
    valid = s.validate(tree)
    um = unmapped(tree)
    r = {
        "conservation": conservation(source_text, tree) if source_text is not None else {"ok": None},
        "dropped": getattr(ladder, "dropped", {}),
        "schema": {"ok": valid, "errors": [str(e) for e in list(s.error_log)[:20]]},
        "unmapped": {"count": len(um), "first": um[:10]},
        "numbering": numbering(tree, formula["ladder"]["types"], formula["ladder"]["tags"],
                               formula.get("sections", {}).get("decimal", False)),
        "parse_coverage": getattr(ladder, "coverage", None),
        "duplicate_identifiers": identifiers(tree),
        "ambiguous_labels": ladder.ambiguous,
        "anomalies": ladder.anomalies,
        "versions": ladder.versions,
        "counts": {t: sum(1 for _ in tree.getroot().iter("{%s}%s" % (NS, t)))
                   for t in ["section"] + list(formula["ladder"]["tags"]) + ["sourceCredit", "editorialNote", "note"]},
    }
    cov = r["parse_coverage"]
    r["ok"] = bool(r["conservation"]["ok"]) and valid and (cov is None or cov["ratio"] >= COVERAGE_MIN)
    return r


def summary(name, r):
    c = r["conservation"]
    files = " in %d files" % c["files"] if c.get("files") else ""
    lines = ["%s: %s" % (name, "PASS" if r["ok"] else "FAIL"),
             "  conservation  %s" % ("ok, %d characters%s" % (c["chars"], files) if c["ok"] else
                                     ("FAILED%s at %d (source %d chars, USLM %d)\n    source: %s\n    uslm:   %s" %
                                     ((" in " + ", ".join(c["failed"][:5])) if c.get("failed") else "", c["first_difference"],
                                      c["source_chars"], c["uslm_chars"], c["source_at"], c["uslm_at"]))),
             "  dropped       %s" % (", ".join("%s %d chars" % kv for kv in r.get("dropped", {}).items()) or "nothing"),
             "  parse         %s" % ("not measured" if not r.get("parse_coverage") else "%.1f%% of the file's visible text%s" % (
                 100 * r["parse_coverage"]["ratio"], "" if r["parse_coverage"]["ratio"] >= COVERAGE_MIN else "  FAILED: the parser lost text")),
             "  schema        %s" % ("ok" if r["schema"]["ok"] else "FAILED\n    " + "\n    ".join(r["schema"]["errors"][:5])),
             "  unmapped      %d" % r["unmapped"]["count"] + "".join("\n    " + u for u in r["unmapped"]["first"][:3]),
             "  numbering     %d to look at" % len(r["numbering"]) + "".join("\n    " + u for u in r["numbering"][:5]),
             "  identifiers   %d repeated" % len(r["duplicate_identifiers"]),
             "  ladder        %d ambiguous (resolved), %d anomalies, %d versions" % (len(r["ambiguous_labels"]), len(r["anomalies"]), len(r["versions"])),
             "  counts        " + ", ".join("%s %d" % kv for kv in r["counts"].items())]
    return "\n".join(lines)
