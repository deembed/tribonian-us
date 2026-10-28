"""build.py -- source files -> USLM file + verify report, by the state's formula.

convert(): one file is one document (Colorado: a C.R.S. title per file). With wrap = {tag, id} in the formula,
the file's contents go inside one level of that tag (a chapter page that carries no chapter heading); the level
gets a number from the item and no text, so the conservation check is unaffected.
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


def _cp1252_fallback(err):
    """A byte that is not UTF-8 inside a UTF-8 page (Nebraska's stray Latin-1 degree sign): read it as
    Windows-1252 instead of losing it to U+FFFD."""
    bad = err.object[err.start:err.end]
    return bad.decode("cp1252", errors="replace"), err.end


import codecs
codecs.register_error("cp1252fallback", _cp1252_fallback)


def epub_join(raw_bytes):
    """An ePub (a zip) -> its XHTML documents in reading order, back to back (META-INF/container.xml names the
    package file; its <spine> gives the order -- the zip's own order is not it). The HTML family parses
    documents back to back as one page. New Mexico's NMSA chapters."""
    import io, posixpath, zipfile
    from lxml import etree
    z = zipfile.ZipFile(io.BytesIO(raw_bytes))
    cont = etree.fromstring(z.read("META-INF/container.xml"))
    opf_path = cont.find(".//{*}rootfile").get("full-path")
    opf = etree.fromstring(z.read(opf_path))
    base = posixpath.dirname(opf_path)
    items = {i.get("id"): i.get("href") for i in opf.iter("{*}item")}
    parts = [z.read(posixpath.normpath(posixpath.join(base, items[r.get("idref")])))
             for r in opf.iter("{*}itemref") if r.get("idref") in items]
    return b"\n".join(parts)


def _decode(raw_bytes, formula):
    if formula.get("container") == "epub":
        raw_bytes = epub_join(raw_bytes)
    from tribonian.uslm import transforms
    raw_bytes, done = transforms.apply(raw_bytes, formula)     # JSON / XML / text sources -> HTML blocks
    enc = "utf-8" if done else formula.get("encoding", "utf-8")
    return raw_bytes.decode(enc, errors="cp1252fallback" if enc.lower().replace("-", "") == "utf8" else "replace")


def convert(raw_bytes, formula, item, meta):
    """-> (USLM ElementTree, verify report)."""
    fam = families.get(formula["family"])
    raw = _decode(raw_bytes, formula)
    root, ladder = fam.parse(raw, formula, item)
    ladder.coverage = fam.parse_coverage(raw) if hasattr(fam, "parse_coverage") else None
    w = formula.get("wrap")
    if w:            # the page has no heading for its own unit (Nebraska's chapter pages): wrap what it holds in one
        num = str(item).lstrip("0") or "0"
        node = Node(w.get("tag", "chapter"), num=num, ident="%s/%s" % (root.ident, w.get("id", "ch{item}").format(item=num)), rank=1)
        node.items, root.items = root.items, [("node", node)]
        if w.get("rebase"):     # identifiers move under the wrapper (/us-mo/const/s1 -> /us-mo/const/a1/s1),
            _rebase(node, root.ident, node.ident)   # for codes whose section numbers restart in every file
    tree = writer.document(root, dict(meta, family=formula["family"], title=formula.get("name")))
    return tree, verify.verify(fam.source_text(raw, formula), tree, ladder, formula)


def _rebase(node, old, new):
    for it in node.items:
        if it[0] == "node":
            n = it[1]
            if n.ident.startswith(old + "/") and not n.ident.startswith(new + "/"):
                n.ident = new + n.ident[len(old):]
            _rebase(n, old, new)


def _sort_key(num, decimal=False):
    return verify.section_key(num, decimal)


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


def _is_container(n):
    return n.tag not in ("section", "level") and 0 < n.rank < 99


def _reprefix(node, old, new):
    """A node moved under another parent: its identifier and every descendant's that starts with the old one."""
    def walk(n):
        if n.ident == old or n.ident.startswith(old + "/"):
            n.ident = new + n.ident[len(old):]
        for x in n.items:
            if x[0] == "node":
                walk(x[1])
    walk(node)


def _merge(target, new):
    """Join a repeated container into the one already built: a nested container with the same identifier merges
    too, at any depth (Montana's section pages each repeat chapter > part), everything else appends in order."""
    have = {x[1].ident: x[1] for x in target.items if x[0] == "node" and _is_container(x[1])}
    for x in new.items:
        if x[0] == "node" and _is_container(x[1]) and x[1].ident in have:
            _merge(have[x[1].ident], x[1])
        else:
            target.items.append(x)


def _carry(it, title, stack, chapters, seen_here, path_of, log, name, scope_container=False):
    """carry = true (index builds of one page per section): a container opened on one page (a chapter heading on
    the chapter's first section page, an outline heading) holds the sections of the following pages until a
    container of the same or higher rank opens. A container moved under an open one takes that parent's
    identifier as prefix (an outline's second "Article 1" is /ch2/a1, not a repeat of /ch1/a1); with
    [sections].scope = "container", so do moved sections. Returns the open stack, outermost first."""
    def descend(st):
        while True:
            kids = [x[1] for x in st[-1].items if x[0] == "node"]
            if kids and _is_container(kids[-1]):
                st.append(kids[-1])
                path_of.setdefault(kids[-1].ident, list(st[:-1]))
            else:
                return st
    if it[0] == "node" and _is_container(it[1]):
        ch = it[1]
        st = list(stack)
        while st and st[-1].rank >= ch.rank:
            st = st[:-1]
        parent = st[-1] if st else title
        if parent is not title and not ch.ident.startswith(parent.ident + "/"):
            _reprefix(ch, ch.ident, parent.ident + "/" + ch.ident.rsplit("/", 1)[-1])
        if ch.ident in chapters and ch.ident in seen_here:          # the same container again: merge into it
            _merge(chapters[ch.ident], ch)
            log.setdefault("joined", []).append("%s <- %s" % (ch.ident, name))
            return descend(list(path_of.get(ch.ident, [])) + [chapters[ch.ident]])
        parent.add("node", ch)
        chapters[ch.ident] = ch
        seen_here.add(ch.ident)
        path_of[ch.ident] = list(st)
        return descend(st + [ch])
    target = stack[-1] if stack else title
    if scope_container and it[0] == "node" and target is not title and it[1].tag == "section":
        n = it[1]
        if not n.ident.startswith(target.ident + "/"):
            _reprefix(n, n.ident, target.ident + "/" + n.ident.rsplit("/", 1)[-1])
    target.items.append(it)
    return stack


def convert_layers(layers, formula, title_ident, title_num_text, title_heading, meta, title_num=None):
    """layers: [(layer name, [(file name, raw bytes), ...]), ...], base first.
    -> (USLM ElementTree, report with per-file results and the overlay log)."""
    fam = families.get(formula["family"])
    title = Node("title", num_text=title_num_text, num=title_num or re.sub(r"^[a-z]+(?=[0-9IVXL])", "", title_ident.rsplit("/", 1)[-1]), heading=title_heading,
                 ident=title_ident, rank=1)
    files, logs, versions, ambiguous, anomalies = [], {}, [], [], []
    chapters = {}
    carry = bool(formula.get("carry"))       # containers stay open across the pages of one item
    open_stack, path_of = [], {}
    for li, (layer, pages) in enumerate(layers):
        log = logs.setdefault(layer, {"replaced": [], "added": [], "chapters_added": []})
        seen_here = set()
        for name, raw_bytes in pages:
            raw = _decode(raw_bytes, formula)
            root, ladder = fam.parse(raw, formula, name, root_ident=title_ident)
            part = writer.document(root, {})
            r = verify.conservation(fam.source_text(raw, formula), part, getattr(ladder, "moved", ()))
            cov = fam.parse_coverage(raw) if hasattr(fam, "parse_coverage") else None
            files.append({"layer": layer, "file": name, "conservation": r, "dropped": getattr(ladder, "dropped", {}),
                          "parse_coverage": cov})
            ambiguous += ["%s/%s: %s" % (layer, name, a) for a in ladder.ambiguous]
            anomalies += ["%s/%s: %s" % (layer, name, a) for a in ladder.anomalies]
            versions += ladder.versions
            for it in root.items:
                if it[0] == "node" and it[1].ident == title_ident:
                    # the page names the file's own level (Utah's whole-title XML: <h1>Title 3 ...</h1> matched by a
                    # container rule with the title's id): it IS the title -- take its number and heading, splice
                    # in what it holds, instead of nesting a second /t3 inside /t3
                    t = it[1]
                    if li == 0:
                        title.num_text, title.heading = title.num_text or t.num_text, title.heading or t.heading
                    for x in t.items:
                        (title.items.append(x) if x[0] != "node" else None)
                        if x[0] == "node":
                            sub = x[1]
                            if carry and li == 0:
                                open_stack = _carry(x, title, open_stack, chapters, seen_here, path_of, log, name,
                                                    formula.get("sections", {}).get("scope") == "container")
                            elif _is_container(sub) and sub.ident not in chapters:
                                chapters[sub.ident] = sub
                                title.add("node", sub)
                                seen_here.add(sub.ident)
                            elif _is_container(sub):
                                _overlay(chapters[sub.ident], sub, log) if li else _merge(chapters[sub.ident], sub)
                            else:
                                title.add("node", sub)
                    continue
                if carry and li == 0:
                    open_stack = _carry(it, title, open_stack, chapters, seen_here, path_of, log, name,
                                        formula.get("sections", {}).get("scope") == "container")
                    continue
                if it[0] != "node":                             # text before the chapter heading
                    if li == 0:
                        title.items.append(it)
                    continue
                ch = it[1]
                if ch.ident in chapters and ch.ident in seen_here:
                    # the same chapter again in the SAME layer (Delaware's subchapter pages repeat the chapter
                    # heading): everything it holds joins the chapter, in page order -- nothing is replaced
                    _merge(chapters[ch.ident], ch)          # nested repeats (chapter > part) merge too
                    log.setdefault("joined", []).append("%s <- %s" % (ch.ident, name))
                elif ch.ident in chapters:
                    _overlay(chapters[ch.ident], ch, log)
                elif li == 0:
                    chapters[ch.ident] = ch
                    title.add("node", ch)
                    seen_here.add(ch.ident)
                else:                                           # a chapter only the supplement has
                    chapters[ch.ident] = ch
                    title.add("node", ch)
                    seen_here.add(ch.ident)
                    log["chapters_added"].append(ch.ident)
    root = Node("lawDoc", ident=title_ident, rank=0)
    root.add("node", title)
    tree = writer.document(root, dict(meta, family=formula["family"], title=formula.get("name")))

    class L:                                                    # the assembled document's ladder log
        pass
    lad = L()
    lad.ambiguous, lad.anomalies, lad.versions, lad.dropped = ambiguous, anomalies, versions, {}
    covs = [f["parse_coverage"] for f in files if f.get("parse_coverage")]
    lad.coverage = min(covs, key=lambda c: c["ratio"]) if covs else None     # the worst file decides
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
    report["ok"] = report["conservation"]["ok"] and report["schema"]["ok"] and \
        (lad.coverage is None or lad.coverage["ratio"] >= verify.COVERAGE_MIN)
    return tree, report


def write(tree, report, xml_path):
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(xml_path), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    report = dict(report, built=date.today().isoformat(), file=xml_path.name)
    xml_path.with_suffix(".verify.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
