#!/usr/bin/env python3
"""Audit wikilink connectivity across the generated US Code markdown corpus.

Orphan = a note with zero outbound [[wikilinks]] AND zero inbound links.
Reports orphan counts by title/chapter and by heading archetype, so you can
tell structural plumbing (definitions, savings clauses, effective dates)
apart from genuinely missing link extraction.

Usage:  ./link_audit.py [md_root]        # default: ./md
"""
import re, sys, os, collections

ROOT = sys.argv[1] if len(sys.argv) > 1 else "md"

LINK = re.compile(r"\[\[([^\]|#]+)")
HEAD = re.compile(r'^heading:\s*"?(.*?)"?\s*$', re.M)

# heading archetypes -> ordered, first match wins
ARCHETYPES = [
    ("repealed/omitted",  r"repeal|omitted|transferred|renumbered|vacan"),
    ("definitions",       r"\bdefinition|\bdefined\b|meaning of"),
    ("savings/preemption",r"relation to other|preempt|savings|effect on other|construction|not affect"),
    ("effective date",    r"effective date|termination|sunset|expiration"),
    ("severability",      r"separabilit|severabilit"),
    ("short title",       r"short title|purpose|findings|policy|congressional"),
    ("appropriations",    r"appropriat|funding|authorization of"),
    ("rulemaking/admin",  r"regulation|rulemaking|implement|administra"),
    ("penalties/enforce", r"penalt|enforce|violation|injunct"),
    ("reports",           r"report to congress|annual report|\breport\b"),
]

notes = {}          # stem -> dict(path, heading, out:set)
for dirpath, _, files in os.walk(ROOT):
    for fn in files:
        if not fn.endswith(".md"):
            continue
        p = os.path.join(dirpath, fn)
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        h = HEAD.search(txt)
        body = txt.split("---", 2)[-1]
        notes[fn[:-3]] = {
            "path": p,
            "heading": (h.group(1) if h else "").strip(),
            "out": {m.strip() for m in LINK.findall(body)},
        }

# inbound: resolve link targets against stems and against aliases-as-stems
stems = set(notes)
inbound = collections.Counter()
for stem, n in notes.items():
    for tgt in n["out"]:
        t = os.path.basename(tgt)
        if t in stems:
            inbound[t] += 1

def bucket(dirpath):
    parts = dirpath.replace(ROOT, "").strip("/").split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else (parts[0] or "(root)")

def archetype(h):
    hl = h.lower()
    for name, pat in ARCHETYPES:
        if re.search(pat, hl):
            return name
    return "(other)"

orphans, no_out, total = [], 0, len(notes)
for stem, n in notes.items():
    if not n["out"]:
        no_out += 1
        if inbound[stem] == 0:
            orphans.append((stem, n))

by_bucket = collections.Counter(bucket(os.path.dirname(n["path"])) for _, n in orphans)
by_arch   = collections.Counter(archetype(n["heading"]) for _, n in orphans)
arch_all  = collections.Counter(archetype(n["heading"]) for n in notes.values())

print(f"corpus            : {total:,} notes under {ROOT}/")
print(f"zero outbound     : {no_out:,}  ({no_out/total:.1%})")
print(f"zero out AND in   : {len(orphans):,}  ({len(orphans)/total:.1%})  <- true orphans")
print()
print("orphans by heading archetype  (orphans / all notes of that archetype)")
for name, _ in ARCHETYPES + [("(other)", "")]:
    if by_arch[name]:
        share = by_arch[name] / arch_all[name] if arch_all[name] else 0
        print(f"  {name:<20} {by_arch[name]:>6,} / {arch_all[name]:>6,}   {share:>6.1%} of archetype orphaned")
print()
print("top 25 chapters by orphan count")
for b, c in by_bucket.most_common(25):
    print(f"  {b:<16} {c:>5,}")
print()
print("sample orphans (20)")
for stem, n in orphans[:20]:
    print(f"  {stem:<24} {n['heading'][:60]}")
