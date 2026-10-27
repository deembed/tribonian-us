"""build.py -- source files -> USLM file + verify report, by the state's formula.

convert(): one file is one document (Colorado: a C.R.S. title per file).
convert_layers(): a title assembled from per-chapter files, in layers (Connecticut: the 2025 revision, then
the 2026 supplement, whose sections replace the revision's). Every file is converted and checked for
conservation on its own; the assembled document is then checked for schema, numbering and identifiers,
and the report lists which sections each layer supplied.
"""
import json
import re
from datetime import date

from tribonian.uslm import families, verify, writer
from tribonian.uslm.tree import Node


def _decode(raw_bytes, formula):
    return raw_bytes.decode(formula.get("encoding", "utf-8"), errors="replace")


def convert(raw_bytes, formula, item, meta):
    """-> (USLM ElementTree, verify report)."""
    fam = families.get(formula["family"])
    raw = _decode(raw_bytes, formula)
    root, ladder = fam.parse(raw, formula, item)
    tree = writer.document(root, dict(meta, family=formula["family"], title=formula.get("name")))
    return tree, verify.verify(fam.source_text(raw, formula), tree, ladder, formula)


def _sort_key(num):
    return verify.section_key(num)


def _sections(node):
    return [(i, it[1]) for i, it in enumerate(node.items) if it[0] == "node" and it[1].tag == "section"]


def _overlay(base_ch, sup_ch, log):
    """Sup's sections replace base's by identifier; new ones go in number order. Sup's other items (its
    table of contents) are not carried over: the base chapter's own stay."""
    have = {n.ident: i for i, n in _sections(base_ch)}
    for _, n in _sections(sup_ch):
        if n.ident in have:
            base_ch.items[have[n.ident]] = ("node", n)
            log["replaced"].append(n.ident)
            continue
        secs = _sections(base_ch)
        after = [i for i, m in secs if _sort_key(m.num) < _sort_key(n.num)]
        at = (after[-1] + 1) if after else (secs[0][0] if secs else len(base_ch.items))
        while at < len(base_ch.items) and base_ch.items[at][0] == "note":   # stay after the previous section's notes
            at += 1
        base_ch.items.insert(at, ("node", n))
        have = {m.ident: i for i, m in _sections(base_ch)}
        log["added"].append(n.ident)


def convert_layers(layers, formula, title_ident, title_num_text, title_heading, meta):
    """layers: [(layer name, [(file name, raw bytes), ...]), ...], base first.
    -> (USLM ElementTree, report with per-file results and the overlay log)."""
    fam = families.get(formula["family"])
    title = Node("title", num_text=title_num_text, num=title_ident.rsplit("/t", 1)[-1], heading=title_heading,
                 ident=title_ident, rank=1)
    files, logs, versions, ambiguous, anomalies = [], {}, [], [], []
    chapters = {}
    for li, (layer, pages) in enumerate(layers):
        log = logs.setdefault(layer, {"replaced": [], "added": [], "chapters_added": []})
        for name, raw_bytes in pages:
            raw = _decode(raw_bytes, formula)
            root, ladder = fam.parse(raw, formula, name, root_ident=title_ident)
            part = writer.document(root, {})
            r = verify.conservation(fam.source_text(raw, formula), part)
            files.append({"layer": layer, "file": name, "conservation": r, "dropped": getattr(ladder, "dropped", {})})
            ambiguous += ["%s/%s: %s" % (layer, name, a) for a in ladder.ambiguous]
            anomalies += ["%s/%s: %s" % (layer, name, a) for a in ladder.anomalies]
            versions += ladder.versions
            for it in root.items:
                if it[0] != "node":                             # text before the chapter heading
                    if li == 0:
                        title.items.append(it)
                    continue
                ch = it[1]
                if ch.ident in chapters:
                    _overlay(chapters[ch.ident], ch, log)
                elif li == 0:
                    chapters[ch.ident] = ch
                    title.add("node", ch)
                else:                                           # a chapter only the supplement has
                    chapters[ch.ident] = ch
                    title.add("node", ch)
                    log["chapters_added"].append(ch.ident)
    root = Node("lawDoc", ident=title_ident, rank=0)
    root.add("node", title)
    tree = writer.document(root, dict(meta, family=formula["family"], title=formula.get("name")))

    class L:                                                    # the assembled document's ladder log
        pass
    lad = L()
    lad.ambiguous, lad.anomalies, lad.versions, lad.dropped = ambiguous, anomalies, versions, {}
    for f in files:
        for k, v in f["dropped"].items():
            lad.dropped[k] = lad.dropped.get(k, 0) + v
    report = verify.verify(None, tree, lad, formula)
    report["files"] = files
    report["layers"] = logs
    bad = [f for f in files if not f["conservation"]["ok"]]
    report["conservation"] = {"ok": not bad, "chars": sum(f["conservation"].get("chars", 0) for f in files),
                              "files": len(files), "failed": [f["layer"] + "/" + f["file"] for f in bad]}
    if bad:
        report["conservation"].update({k: bad[0]["conservation"][k] for k in
                                       ("first_difference", "source_chars", "uslm_chars", "source_at", "uslm_at")})
    report["ok"] = report["conservation"]["ok"] and report["schema"]["ok"]
    return tree, report


def write(tree, report, xml_path):
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(xml_path), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    report = dict(report, built=date.today().isoformat(), file=xml_path.name)
    xml_path.with_suffix(".verify.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
