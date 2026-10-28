"""Parser families. Each module exposes parse(raw, formula, item) -> (root Node, Ladder) and
source_text(raw) -> the file's whole text, read independently of parse() for the conservation check."""
from importlib import import_module

FAMILIES = {"html-paragraphs": "html_paragraphs"}


def get(name):
    if name not in FAMILIES:
        raise SystemExit("no parser family %r (have: %s)" % (name, ", ".join(FAMILIES)))
    return import_module("tribonian.uslm.families." + FAMILIES[name])
