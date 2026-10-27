"""writer.py -- the common tree (tree.py) -> USLM 1.0 XML, the schema the OLRC's US Code files use,
so usc2md's reader takes state law as it takes the Code.

Text placement follows USLM's level model: a level with no child levels holds its text in <content>
(one <p> per paragraph); a level with children puts text before the first child in <chapeau> and text
after a child in <continuation>. Notes go where they occur. Nothing is added to the text: verify.py
checks that the USLM's characters are exactly the source's, in order.
"""
from lxml import etree

NS = "http://xml.house.gov/schemas/uslm/1.0"
DC = "http://purl.org/dc/elements/1.1/"
NOTE_TAGS = {"sourceCredit", "editorialNote", "statutoryNote", "note"}


def E(tag, text=None, **attrs):
    el = etree.Element("{%s}%s" % (NS, tag), **{k: str(v) for k, v in attrs.items() if v not in (None, "")})
    if text:
        el.text = text
    return el


def _ps(parent_tag, texts, **attrs):
    el = E(parent_tag, **attrs)
    for t in texts:
        el.append(E("p", t))
    return el


def _note(item):
    _, tag, topic, texts = item
    tag = tag if tag in NOTE_TAGS else "note"
    return _ps(tag, texts, topic=topic if tag in ("note", "editorialNote") else None)


def level(node):
    el = E(node.tag, identifier=node.ident)
    if node.num_text or node.num:
        el.append(E("num", node.num_text, value=node.num))
    if node.heading:
        el.append(E("heading", node.heading))
    has_children = any(it[0] == "node" for it in node.items)
    texts = []                         # consecutive text runs become one block

    def flush(kind):
        if texts:
            el.append(_ps(kind, texts))
            texts.clear()

    seen_child = False
    for it in node.items:
        kind = it[0]
        if kind in ("text", "unmapped"):
            if kind == "unmapped":
                flush("chapeau" if has_children and not seen_child else "continuation" if has_children else "content")
                el.append(_ps("note", [it[1]], role="unmapped"))
                continue
            texts.append(it[1])
            continue
        flush("content" if not has_children else "continuation" if seen_child else "chapeau")
        if kind == "node":
            el.append(level(it[1]))
            seen_child = True
        elif kind == "note":
            el.append(_note(it))
        elif kind == "cross":
            el.append(E("crossHeading", it[1]))
    flush("content" if not has_children else "continuation" if seen_child else "chapeau")
    _fix_leaf_order(el)
    return el


def _fix_leaf_order(el):
    """USLM lets a leaf hold <content>... then notes, but not content, note, content. When a note or a
    crossHeading splits a leaf's text, its text blocks become <continuation> (the schema's text group),
    which may interleave with notes. Order is kept either way."""
    kids = [k for k in el if etree.QName(k).localname not in ("num", "heading")]
    tags = [etree.QName(k).localname for k in kids]
    if "content" not in tags:
        return
    first_other = next((i for i, t in enumerate(tags) if t != "content"), len(tags))
    if all(t != "content" for t in tags[first_other:]) and "crossHeading" not in tags:
        return                          # content..., then notes only: valid as it is
    for k in kids:                      # interleaved: every text block becomes a <continuation> (text group)
        if etree.QName(k).localname == "content":
            k.tag = "{%s}continuation" % NS


def document(root, meta):
    """root Node (tag lawDoc) + meta dict -> an lxml ElementTree."""
    doc = etree.Element("{%s}lawDoc" % NS, nsmap={None: NS, "dc": DC}, identifier=root.ident)
    m = etree.SubElement(doc, "{%s}meta" % NS)
    for k in ("title", "publisher", "source"):
        if meta.get(k):
            etree.SubElement(m, "{%s}%s" % (DC, k)).text = str(meta[k])
    for k in ("edition", "retrieved", "url", "family", "formula"):
        if meta.get(k):
            m.append(E("property", str(meta[k]), role=k))
    main = etree.SubElement(doc, "{%s}main" % NS)
    for it in root.items:
        if it[0] == "node":
            main.append(level(it[1]))
        elif it[0] == "note":
            main.append(_note(it))
        elif it[0] in ("text", "unmapped", "cross"):
            main.append(_ps("note", [it[1]], role="unmapped" if it[0] != "cross" else "crossHeading"))
    return etree.ElementTree(doc)
