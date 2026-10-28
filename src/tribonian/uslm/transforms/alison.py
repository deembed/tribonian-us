"""ALISON GraphQL JSON -> HTML blocks for the html-paragraphs family (reference for a shared transform "alison-json").

Input: the JSON body of a GET to https://alison.legislature.state.al.us/graphql (see AL.status.md), any of
    {"data": {"sections": {"data": [node, ...]}}}            print-style query (aliased "sections")
    {"data": {"codesOfAlabama": {"data": [node, ...]}}}      un-aliased Code query
    {"data": {"constitutionItems": {"data": [...]}}} / {"data": {"constitutionItem": node}}
node fields (all optional except displayId): codeId parentId displayId title catchLine content history
    effectiveDate supersessionDate isBranchNode
Output (UTF-8): <div id="al"> with, per node in the order given:
    branch node (isBranchNode, or a title starting Title/Chapter/Article/Part/Subpart/Division):
        <h2>{title}</h2>                                    container heading ("Article 1 General Provisions.")
    section:
        <p class="section"><b>{title}</b></p>               "Section 1-1-1 Meaning of Certain Words and Terms."
                                                            (no title: "Section {displayId} {catchLine}")
        {content}                                           the site's own HTML (<p> per paragraph), re-serialized
                                                            per node so an unclosed tag (a stray <span>) cannot
                                                            (CALS <row>/<entry> become <tr>/<td>)
                                                            swallow the following sections
        <p class="history">{history}</p>                    source credit, when present
Text is never dropped: content goes through verbatim (HTML comments like <!--StartFragment--> carry no text).
"""
import html, json, re, sys

from lxml import html as lhtml

BRANCH = re.compile(r"^(Title|Chapter|Article|Part|Subpart|Division|Subdivision|Amendment)\s", re.I)


def nodes(doc):
    d = doc.get("data") or {}
    for v in d.values():
        if isinstance(v, dict) and isinstance(v.get("data"), list):
            yield from v["data"]
        elif isinstance(v, list):
            yield from (x for x in v if isinstance(x, dict))
        elif isinstance(v, dict) and "displayId" in v:
            yield v


def balanced(frag):
    """the fragment parsed on its own and written back: same text, every tag closed inside it"""
    try:
        root = lhtml.fragment_fromstring(frag, create_parent="div")
    except Exception:                      # (lxml refuses an empty / comment-only fragment)
        return frag
    for tag, new in (("row", "tr"), ("entry", "td")):     # CALS tables (tgroup/row/entry) -> HTML rows/cells, so a
        for el in root.iter(tag):                          # verbatim table reads cell by cell (text unchanged)
            el.tag = new
    return html.escape(root.text or "", quote=False) + "".join(lhtml.tostring(c, encoding="unicode") for c in root)


def node_html(n):
    t = (n.get("title") or "").strip()
    if n.get("isBranchNode") or (t and BRANCH.match(t) and not n.get("content")):
        return ["<h2>%s</h2>" % html.escape(t, quote=False)]
    if not t:
        t = ("Section %s %s" % (n.get("displayId", ""), n.get("catchLine") or "")).strip()
    out = ['<p class="section"><b>%s</b></p>' % html.escape(t, quote=False)]
    if n.get("content"):
        out.append(balanced(n["content"]))
    if n.get("history"):
        out.append('<p class="history">%s</p>' % html.escape(n["history"].strip(), quote=False))
    return out


def to_html(raw):
    doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if doc.get("errors") and not doc.get("data"):
        raise SystemExit("ALISON GraphQL error: %s" % doc["errors"][0].get("message"))
    body = [h for n in nodes(doc) for h in node_html(n)]
    return ('<html><body><div id="al">\n%s\n</div></body></html>' % "\n".join(body)).encode("utf-8")


if __name__ == "__main__":
    sys.stdout.buffer.write(to_html(open(sys.argv[1], "rb").read()))
