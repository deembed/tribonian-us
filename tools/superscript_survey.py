#!/usr/bin/env python3
"""Survey: can footnote markers be found from the PDF's own type (small, raised digits)?

For every brief under cases/scotus/<docket>/ that has both a .pdf and the .txt the builds read, compare
  - the text parser's footnotes (case2md.layout_pages on the brief's body, as tribonian scotus build reads it)
  - superscript digit runs in the PDF: digits set below 80% of the brief's body size (the most common size
    across the whole brief, not per page: a page of mostly footnotes would make the notes' size the "body")
    whose baseline sits above the full-size character before them.

Writes build/superscript-survey/survey.tsv (one row per brief) and survey.log (the summary), and prints the
summary. Read-only on the briefs. Run from the repo root with the tribonian venv active:

    python3 tools/superscript_survey.py              # every brief, 2 worker processes
    python3 tools/superscript_survey.py -j 4 25-238  # some dockets only
    python3 tools/superscript_survey.py --max-pages 300   # skip the big joint appendices
"""
import argparse
import contextlib
import io
import multiprocessing as mp
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "build" / "superscript-survey"


def supers(pdf_path, max_pages):
    """-> (pages, body size, [(page, number)] for every small raised digit run), or None if over max_pages."""
    import pdfplumber
    sizes, runs = Counter(), []                 # runs: (page, number, size, raised)
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        if max_pages and n > max_pages:
            return None
        for pn, pg in enumerate(pdf.pages, 1):
            ch = [c for c in pg.chars if c["text"].strip()]
            sizes.update(round(c["size"], 1) for c in ch)
            run, before, prev = [], None, None
            for c in ch + [None]:
                digit = c is not None and c["text"].isdigit()
                same = run and digit and abs(c["size"] - run[-1]["size"]) < 0.3 and abs(c["top"] - run[-1]["top"]) < 1
                if same:
                    run.append(c)
                else:
                    if run:
                        raised = before is not None and run[0]["bottom"] < before["bottom"] - 1
                        runs.append((pn, "".join(x["text"] for x in run), run[0]["size"], raised))
                    run, before = ([c], prev) if digit else ([], None)
                if c is not None:
                    prev = c
            pg.flush_cache()
    body = sizes.most_common(1)[0][0] if sizes else 0
    found = [(p, int(num)) for p, num, sz, raised in runs if raised and sz < body * 0.8 and len(num) <= 3]
    return n, body, found


def text_notes(txt_path):
    from tribonian import case2md as c, scotus as s
    t = txt_path.read_text(encoding="utf-8", errors="replace")
    with contextlib.redirect_stdout(io.StringIO()):
        sp = s.split_brief(t)
        keep = sp["qp"] + sp["body"]
        _, fns, _ = c.layout_pages("\f".join("\n".join(ls) for lab, ls in keep))
    return [int(f[0]) for f in fns if f[0] and f[0].isdigit()]


def prefix(nums):
    """Longest unbroken 1, 2, 3, ... found."""
    k = 0
    while k + 1 in nums:
        k += 1
    return k


def one(job):
    pdf, txt, max_pages = job
    t0 = time.time()
    row = {"brief": "%s/%s" % (pdf.parent.name, pdf.stem)}
    try:
        tn = text_notes(txt)
        r = supers(pdf, max_pages)
    except Exception as e:                      # a broken PDF is a row, not a stopped survey
        row.update(status="error", note=repr(e)[:120])
        return row
    if r is None:
        row.update(status="skipped", note="over --max-pages")
        return row
    pages, body, found = r
    sn = set(n for _, n in found)
    tset = set(tn)
    top = max(sn | tset, default=0)
    # the brief's footnotes: the longest run 1..k in either source (stray small digits past it are noise)
    k = max(prefix(sn), prefix(tset))
    noise = sorted(n for n in sn if n > k)
    row.update(
        status="ok", pages=pages, body=body, notes=k,
        text_found=len(tset & set(range(1, k + 1))),
        super_found=len(sn & set(range(1, k + 1))),
        super_missing=",".join(map(str, sorted(set(range(1, k + 1)) - sn)))[:200],
        text_missing=",".join(map(str, sorted(set(range(1, k + 1)) - tset)))[:200],
        noise=",".join(map(str, noise))[:120], top=top, secs=round(time.time() - t0, 1),
        either=len((sn | tset) & set(range(1, k + 1))))
    if k == 0:
        row["verdict"] = "no notes"
    elif row["super_found"] == k:
        row["verdict"] = "super all"
    elif row["super_found"] >= 0.9 * k:
        row["verdict"] = "super most"
    elif row["super_found"] == 0:
        row["verdict"] = "super none"
    else:
        row["verdict"] = "super some"
    return row


COLS = ["brief", "status", "verdict", "pages", "body", "notes", "text_found", "super_found",
        "either", "text_missing", "super_missing", "noise", "top", "secs", "note"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dockets", nargs="*", help="docket folders under cases/scotus (default: all)")
    ap.add_argument("-j", type=int, default=2, help="worker processes (default 2)")
    ap.add_argument("--max-pages", type=int, default=0, help="skip PDFs longer than this (0 = no limit)")
    a = ap.parse_args()
    base = ROOT / "cases" / "scotus"
    dirs = [base / d for d in a.dockets] if a.dockets else sorted(p for p in base.iterdir() if p.is_dir())
    jobs = [(p, p.with_suffix(".txt"), a.max_pages) for d in dirs for p in sorted(d.glob("*.pdf")) if p.with_suffix(".txt").exists()]
    OUT.mkdir(parents=True, exist_ok=True)
    tsv, log = OUT / "survey.tsv", OUT / "survey.log"
    rows = []
    print("%d briefs, %d workers -> %s" % (len(jobs), a.j, tsv.relative_to(ROOT)), flush=True)
    with open(tsv, "w", encoding="utf-8") as fh, mp.Pool(a.j) as pool:
        fh.write("\t".join(COLS) + "\n")
        for i, row in enumerate(pool.imap_unordered(one, jobs), 1):
            rows.append(row)
            fh.write("\t".join(str(row.get(c, "")) for c in COLS) + "\n")
            fh.flush()
            print("%4d/%d  %-10s %-11s %s" % (i, len(jobs), row["status"], row.get("verdict", ""), row["brief"][:80]), flush=True)
    ok = [r for r in rows if r["status"] == "ok"]
    noted = [r for r in ok if r["notes"]]
    v = Counter(r["verdict"] for r in ok)
    tot = sum(r["notes"] for r in noted)
    L = ["superscript survey, %s" % time.strftime("%Y-%m-%d %H:%M"),
         "briefs: %d (ok %d, skipped %d, error %d)" % (len(rows), len(ok), sum(r["status"] == "skipped" for r in rows),
                                                       sum(r["status"] == "error" for r in rows)),
         "verdicts: " + ", ".join("%s %d" % kv for kv in sorted(v.items())),
         "footnotes in briefs with notes: %d" % tot,
         "  found by the text parser: %d (%.1f%%)" % (sum(r["text_found"] for r in noted), 100 * sum(r["text_found"] for r in noted) / max(tot, 1)),
         "  found as superscripts:    %d (%.1f%%)" % (sum(r["super_found"] for r in noted), 100 * sum(r["super_found"] for r in noted) / max(tot, 1)),
         "  found by either:          %d" % sum(r["either"] for r in noted),
         "briefs where superscripts beat the text parser: %d; text beats superscripts: %d" % (
             sum(r["super_found"] > r["text_found"] for r in noted), sum(r["text_found"] > r["super_found"] for r in noted)),
         "", "worst for superscripts (brief, notes, super found, text found):"]
    for r in sorted(noted, key=lambda r: r["super_found"] / r["notes"])[:15]:
        L.append("  %-75s %4d %4d %4d" % (r["brief"][:75], r["notes"], r["super_found"], r["text_found"]))
    for r in rows:
        if r["status"] == "error":
            L.append("error: %s %s" % (r["brief"], r.get("note", "")))
    log.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n" + "\n".join(L))
    print("\nwrote %s and %s" % (tsv.relative_to(ROOT), log.relative_to(ROOT)))


if __name__ == "__main__":
    main()
