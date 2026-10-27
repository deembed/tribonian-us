"""build.py -- one source file -> USLM file + verify report, by the state's formula."""
import json
from datetime import date

from lxml import etree

from tribonian.uslm import families, verify, writer


def convert(raw_bytes, formula, item, meta):
    """-> (USLM ElementTree, verify report)."""
    fam = families.get(formula["family"])
    raw = raw_bytes.decode(formula.get("encoding", "utf-8"), errors="replace")
    root, ladder = fam.parse(raw, formula, item)
    tree = writer.document(root, dict(meta, family=formula["family"], title=formula.get("name")))
    return tree, verify.verify(fam.source_text(raw), tree, ladder, formula)


def write(tree, report, xml_path):
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(xml_path), xml_declaration=True, encoding="UTF-8", pretty_print=True)
    report = dict(report, built=date.today().isoformat(), file=xml_path.name)
    xml_path.with_suffix(".verify.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
