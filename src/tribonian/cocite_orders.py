"""Supreme Court orders-list entries out of the co-citation edges (added 2026-11-10).

CourtListener holds one cluster per orders-list page entry ("Certiorari denied", "Rehearing denied",
a dissent from denial). A citation's subsequent history ("Skillern v. Estelle, 720 F.2d 839 (5th Cir. 1983),
cert. denied, 469 U.S. 873 (1984)") then links the citing decision both to the case and to that entry, and one
entry covers every case on its page: 459 U.S. 1188 "Horton v. Georgia" is the denial for Horton (Ga.) and for
State v. Blair and State v. Trimble (Mo.), so Missouri capital appeals citing Blair "co-cite" a Georgia case.
About 5,700 such entries were nodes of the case-note view, some cited by 700 decisions in a slice, and they
bridged unrelated lineages. The edge to the decision under review is untouched, so nothing true is lost.

Merits rulings stay: opinions, summary affirmances, GVRs, dismissals for want of a substantial federal
question or of jurisdiction (they bind lower courts), and dissents from denial long enough (LIMIT) to be
cited for their reasoning.
"""
import html, re, sqlite3, time
from pathlib import Path

import numpy as np

from tribonian import casedb, textcodec

VERSION = 1           # bump when classify() changes: the cache's edges are filtered again from a fresh prep
LIMIT = 6000          # characters of opinion text; longer entries are kept
ORDER = re.compile(r"certiorari\b.{0,160}?\bdenied|dissenting from (?:the )?denial|rehearing\b.{0,60}?\bdenied|"
                   r"certiorari dismissed|\bdismissed\b|leave to file\b.{0,80}?\bdenied|certiorari (?:is |are )?granted|"
                   r"probable jurisdiction (?:is )?noted|postponed to the hearing|"
                   r"writs? of (?:mandamus|habeas corpus|prohibition)\b.{0,40}?\bdenied", re.I | re.S)
MERITS = re.compile(r"\b(affirmed|reversed|vacated|remanded)\b|want of (?:a )?substantial federal question|"
                    r"want of jurisdiction", re.I)
DISSENT = re.compile(r"dissenting from (?:the )?denial", re.I)
ASIDE = re.compile(r"(?:would|(?:is|are) of the opinion that|believes? that|dissent)[^.]*\.", re.I)


def plain(blob):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", textcodec.unpack(blob))).split())


def classify(tx):
    """'order' (an orders-list entry), 'short merits', 'short other', 'long' or 'no text'."""
    if not tx:
        return "no text"
    if len(tx) >= LIMIT:
        return "long"
    t = re.sub(r"(\w)-\s*(\w)", r"\1\2", tx)          # OCR hyphenation: "Certio-rari", "Cer- tiorari"
    court = ASIDE.sub(" ", t)                         # what some Justices would have done is not the disposition
    if ORDER.search(t) and (DISSENT.search(t) or not MERITS.search(court)):
        return "order"
    return "short merits" if MERITS.search(court) else "short other"


def find_orders(con):
    """Cluster ids of every Supreme Court orders-list entry (about 420,000 of 500,000; two minutes)."""
    t0 = time.time()
    ops = sqlite3.connect("file:%s?mode=ro" % casedb.OPS_DB, uri=True)
    ids = [r[0] for r in con.execute("SELECT c.id FROM clusters c JOIN dockets d ON d.id = c.docket_id "
                                     "WHERE d.court_id = 'scotus'")]
    out, n = [], {}
    for cid in ids:
        tx = " ".join(plain(r[0]) for r in ops.execute("SELECT text FROM opinions WHERE cluster_id = ?", (cid,)) if r[0])
        k = classify(tx)
        n[k] = n.get(k, 0) + 1
        if k == "order":
            out.append(cid)
    print("Supreme Court clusters: %s; orders-list entries: %s  (%s; %.1f min)" % (
        format(len(ids), ","), format(len(out), ","), ", ".join("%s %s" % (k, format(v, ",")) for k, v in sorted(n.items())),
        (time.time() - t0) / 60), flush=True)
    return np.array(sorted(out), np.int64)


def tag():
    return " | orders v%d" % VERSION


def applied(cache):
    stamp = cache / "stamp.txt"
    return stamp.exists() and stamp.read_text().endswith(tag())


def apply(con, cache):
    """Drop every edge to an orders-list entry from citing/cited/year.npy, and mark the cache's stamp so
    every view built from the unfiltered cache counts as stale. Returns the number of edges dropped."""
    if applied(cache):
        return 0
    t0 = time.time()
    orders = find_orders(con)
    np.save(cache / "orders.npy", orders)
    ci, cd, y = np.load(cache / "citing.npy"), np.load(cache / "cited.npy"), np.load(cache / "year.npy")
    drop = np.isin(cd, orders.astype(cd.dtype))
    keep = ~drop
    np.save(cache / "citing.npy", ci[keep])
    np.save(cache / "cited.npy", cd[keep])
    np.save(cache / "year.npy", y[keep])
    stamp = cache / "stamp.txt"
    stamp.write_text(stamp.read_text() + tag())
    print("orders-list entries: %s edges to %s entries dropped of %s  (%.1f min)" % (
        format(int(drop.sum()), ","), format(len(np.unique(cd[drop])), ","), format(len(cd), ","),
        (time.time() - t0) / 60), flush=True)
    return int(drop.sum())
