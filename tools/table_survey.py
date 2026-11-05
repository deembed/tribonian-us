#!/usr/bin/env python3
"""Survey: where are the tables in the filed briefs, and what kind (extract.find_tables, detection only).

For every PDF under cases/scotus/<docket>/ that has been extracted, each page (sideways pages turned upright
first, as extract.py does) -> build/table-survey/pages.tsv (one row per table-like region: filing, page, kind,
rows, columns, words per cell, the first two rows) and survey.log (counts by kind, the filings with the most).
Read-only. Run from the repo root with the tribonian venv active:

    python3 tools/table_survey.py -j 4
    python3 tools/table_survey.py 25-238 25-579
"""
import argparse, multiprocessing as mp, sys, tempfile, time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "build" / "table-survey"


def one(pdf):
    import logging, pdfplumber
    from tribonian import extract as x
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    try:
        with pdfplumber.open(str(pdf)) as doc:
            turns = {}
            sizes = Counter()
            for pi, pg in enumerate(doc.pages):
                ch = [c for c in pg.chars if c["text"].strip()]
                sizes.update(round(c["size"], 1) for c in ch)
                if x.turn_for(ch):
                    turns[pi] = x.turn_for(ch)
                pg.flush_cache()
        body = sizes.most_common(1)[0][0] if sizes else 10
        with tempfile.TemporaryDirectory() as tmp:
            src = x.turned_copy(pdf, turns, tmp) if turns else pdf
            found = []
            with pdfplumber.open(str(src)) as doc:
                for pi, pg in enumerate(doc.pages):
                    for t in x.find_tables(pg, body):
                        found.append(dict(t, page=pi + 1, turned=pi in turns))
                    pg.flush_cache()
        return pdf, len(turns), found, None
    except Exception as e:
        return pdf, 0, [], repr(e)[:150]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dockets", nargs="*")
    ap.add_argument("-j", type=int, default=2)
    a = ap.parse_args()
    base = ROOT / "cases" / "scotus"
    dirs = [base / d for d in a.dockets] if a.dockets else sorted(p for p in base.iterdir() if p.is_dir())
    pdfs = [p for d in dirs for p in sorted(d.glob("*.pdf")) if (d / "extracted" / (p.stem + ".txt")).exists()]
    OUT.mkdir(parents=True, exist_ok=True)
    kinds, filings, per, failed = Counter(), defaultdict(set), Counter(), []
    with open(OUT / "pages.tsv", "w", encoding="utf-8") as fh, mp.Pool(a.j) as pool:
        fh.write("filing\tpage\tturned\tkind\trows\tcols\twords_per_cell\tfirst rows\n")
        for i, (pdf, nturn, found, err) in enumerate(pool.imap_unordered(one, pdfs), 1):
            rel = str(pdf.relative_to(base).with_suffix(""))
            if err:
                failed.append((rel, err))
            for t in found:
                kinds[t["kind"]] += 1
                filings[t["kind"]].add(rel)
                per[(rel, t["kind"])] += 1
                fh.write("\t".join(str(v) for v in (rel, t["page"], int(t["turned"]), t["kind"], t["rows"], t["cols"],
                                                     t["cell"], t["sample"].replace("\t", " "))) + "\n")
            if i % 50 == 0:
                print("  %d/%d" % (i, len(pdfs)), flush=True)
    L = ["table survey, %s: %d filings, %d failed" % (time.strftime("%Y-%m-%d %H:%M"), len(pdfs), len(failed)), "",
         "%-12s %8s %8s" % ("kind", "regions", "filings")]
    for k, n in kinds.most_common():
        L.append("%-12s %8d %8d" % (k, n, len(filings[k])))
    for k in ("ruled", "aligned", "short-cells"):
        top = sorted(((n, f) for (f, kk), n in per.items() if kk == k), reverse=True)[:6]
        if top:
            L += ["", "most %s regions:" % k] + ["  %4d  %s" % (n, f[:90]) for n, f in top]
    L += ["  failed: %s %s" % f for f in failed[:10]]
    (OUT / "survey.log").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print("\nwrote build/table-survey/pages.tsv and survey.log")


if __name__ == "__main__":
    main()
