#!/usr/bin/env python3
"""cocite.py -- co-citation clusters, five years at a time, from the local bulk database.

Two cases are co-cited when one later decision cites both. The citing decisions are sliced
by filing year (2005-09, 2010-14, ...), and for each slice:

  1. Nodes: cited cases with enough citing decisions in the slice: --min-cited-frac of the
     slice's citing decisions (0.002%), but never fewer than --min-cited (5). The share keeps
     slices comparable as the courts' output and CourtListener's coverage grow; the floor
     keeps the early slices from treating two cites as a pattern.
  2. Pairs: support = how many citing decisions cite both. Pairs under --min-support are
     dropped. Lift = support * N / (n_a * n_b): how many times more often the pair occurs
     than it would if the two were cited independently (1 = no association).
     Cosine = support / sqrt(n_a * n_b) ranks a case's partners without favouring either
     the giants (raw support) or the rarities (lift).
  3. Clusters: Leiden community detection, modularity objective, on support weights.
     Modularity already scores a grouping against chance (k_a * k_b / 2m, the same
     baseline lift uses), so dividing by chance first would correct twice.
  4. Strength: every case gets a share in every cluster = the part of its pair weight that
     goes to that cluster's members. Home cluster = where Leiden put it; a case is also
     listed in any other cluster that takes at least --share of its weight.
  5. Lineage: clusters are matched to the next slice by shared members (Jaccard over the
     cases present in both slices). A mutual best match at >= --carry continues the
     earlier cluster's lineage, and with it the lineage's name.
  6. Sub-clusters: modularity merges groups that are tight but small next to the whole graph
     (the "resolution limit"), so in modern slices one cluster holds most of federal civil
     litigation. Every cluster of --split-over (20) cases or more is clustered again on its own
     pairs: 2010-14:1.3 is sub-cluster 3 of cluster 1. Leiden always finds *some* split, and
     random graphs score high modularity by chance (sparse ones 0.35-0.4), so a split is kept
     only if it beats chance: the cluster's pairs are rewired --nulls (5) times, keeping every
     case's number of partners and shuffling the weights, Leiden is run on each, and the real
     split must score --sub-z (4) standard deviations above them. An unsplit cluster is its own
     single sub-cluster (2010-14:5). Sub-clusters get their own lineages.
  7. The same test, run on the whole slice (3 rewirings), gives each slice its modularity above
     chance, which, unlike raw modularity, can be compared across slices.

The citing unit is the decision (cluster), not the opinion, so a majority and a dissent
that both cite A and B count once. A decision citing more than --max-cites of the slice's
nodes is left out of that slice (string-cite compilations swamp the pair counts); build
reports how many.

Commands
    ./cocite.py prep                                 # one pass over the database -> cache (rerun after a bulk load)
    ./cocite.py build [--slices 2005,2010] [--force] # cluster every slice (or just these), then lineage
    ./cocite.py status
    ./cocite.py case --cite "463 U.S. 29" [--slice 2005] [--top 8]
    ./cocite.py cluster 2005-09:3                    # members of one cluster, its sub-clusters, before and after
    ./cocite.py cluster 2010-14:1.3                  # one sub-cluster
    ./cocite.py tree 2010                            # one slice: clusters with their sub-clusters
    ./cocite.py note --cite "463 U.S. 29"            # preview the '## Co-citation' section of a case note
    ./cocite.py areas --courts appellate             # areas: sub-cluster lineages grouped once, over all slices
    ./cocite.py area-evidence --courts appellate     # build/cocite/areas-*.jsonl, for naming them
    ./cocite.py lineage 17                           # one lineage, slice by slice
    ./cocite.py together --cite "550 U.S. 544" --cite "556 U.S. 662"
                                                     # falsification check: same cluster? pair support, lift
    ./cocite.py shift --cite "375 U.S. 384" --courts appellate
                                                     # good-law signals from cluster change (cocite_shift.py)
    ./cocite.py shift --eval --courts appellate      # the same, scored on goodlaw_eval.py's cases
    ./cocite.py positions [--show ID | --sample N]   # where every citation occurs (cocite_positions.py)
    ./cocite.py prox-evidence --courts appellate --slice 2010
                                                     # blind evidence: pairs by sentence share, for a reader
    ./cocite.py diag                                 # citing decisions per slice by court level
    ./cocite.py evidence --courts appellate          # build/cocite/evidence-*.jsonl: what to describe, and the evidence
    ./cocite.py labels import labels.jsonl           # descriptions -> cocite_labels.json, attached to lineages
    ./cocite.py labels list --courts appellate

Proximity weights: --weight on build and the read commands replaces "decisions citing both" with
a weight from where the citations sit (same sentence, same string cite, ...), in its own results
file (cocite-appellate-sentence.sqlite); see cocite_positions.py. Needs ./cocite.py positions --index.
    ./cocite.py build --courts appellate --weight sentence
    ./cocite.py build --courts appellate --weight sentence --mix 0.2   # binary edges, re-weighted
    ./cocite.py areas --courts appellate --weight sentence --grid "cpm:0.5@10"

Court views: --courts on build and the read commands keeps only citing decisions from those
courts (the cited cases can come from anywhere), in its own results file:
    ./cocite.py build --courts appellate             # -> cocite-appellate.sqlite
    ./cocite.py tree 2010 --courts appellate
Levels: scotus, circuit (federal courts of appeals), district (federal district courts),
state-supreme, state-appellate, other (bankruptcy, specialty, state trial, tribal, military...).
Shortcuts: appellate = scotus,circuit,state-supreme,state-appellate; federal = scotus,circuit,district.

Files (beside the main database, like search.sqlite; CL_COCITE_DB overrides):
    cocite.sqlite   results; derived, disposable, rebuilt when the bulk load changes
    cocite-cache/   *.npy from prep: citing decision, cited case, year per edge; court level per case

Needs numpy, scipy and python-igraph (Linux Mint: sudo apt install python3-numpy python3-scipy python3-igraph).
"""
import argparse, hashlib, json, math, os, random, re, sqlite3, sys, time
from collections import Counter
from pathlib import Path

import numpy as np

import casedb

BASE_DB = Path(os.environ.get("CL_COCITE_DB") or casedb.DB.with_name("cocite.sqlite"))
COCITE_DB = BASE_DB                      # cocite-<view>.sqlite under --courts (set in main)
CACHE = BASE_DB.with_name("cocite-cache")

COURT_LEVELS = ["scotus", "circuit", "district", "state-supreme", "state-appellate", "other"]   # codes 1..6
COURT_SETS = {"appellate": ["scotus", "circuit", "state-supreme", "state-appellate"],
              "federal": ["scotus", "circuit", "district"]}


def court_code(court_id, jurisdiction):
    """courts.jurisdiction: F federal appellate, FD district, S state supreme, SA state appellate, ..."""
    if court_id == "scotus":
        return 1
    return {"F": 2, "FD": 3, "S": 4, "SA": 5}.get(jurisdiction or "", 6)


def parse_courts(text):
    """'appellate' or 'scotus,circuit' -> (codes, file tag); None -> all courts."""
    if not text or text == "all":
        return None, "all"
    # a view tag may carry a weighting ("appellate-sentence-m0.2"): the courts are what comes before it
    text = re.sub(r"-(?:decision|paragraph|sentence|string|k\d+)(?:-m[\d.]+)?$", "", text)
    if not text:
        return None, "all"
    names = []
    for t in text.split(","):
        t = t.strip()
        names += COURT_SETS.get(t, [t])
    bad = [n for n in names if n not in COURT_LEVELS]
    if bad:
        sys.exit("unknown court level %s; levels: %s; shortcuts: %s" % (", ".join(bad), ", ".join(COURT_LEVELS),
                                                                       ", ".join(COURT_SETS)))
    codes = sorted({COURT_LEVELS.index(n) + 1 for n in names})
    tag = text if text in COURT_SETS else "-".join(COURT_LEVELS[c - 1] for c in codes)
    return codes, tag
WIDTH = 5

DEFAULTS = dict(min_cited=5, min_cited_frac=2e-5, min_support=5, max_cites=150, resolution=1.0, share=0.15,
                top=20, min_size=3, seed=0, split_over=20, sub_z=4.0, nulls=5)
SCHEMA_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS slices(slice INTEGER PRIMARY KEY, label TEXT, stamp TEXT, params TEXT,
    citing INTEGER, skipped INTEGER, nodes INTEGER, pairs INTEGER, clusters INTEGER, clustered INTEGER,
    modularity REAL, seconds REAL, built TEXT, subs INTEGER, min_cited INTEGER, q_null REAL, q_null_sd REAL);
CREATE TABLE IF NOT EXISTS nodes(slice INTEGER, ref INTEGER, n INTEGER, cl INTEGER, weight REAL, sub INTEGER, subw REAL);
CREATE TABLE IF NOT EXISTS clusters(slice INTEGER, cl INTEGER, size INTEGER, anchor INTEGER,
    internal REAL, lineage INTEGER);
CREATE TABLE IF NOT EXISTS subs(slice INTEGER, sub INTEGER, size INTEGER, anchor INTEGER,
    internal REAL, lineage INTEGER);
CREATE TABLE IF NOT EXISTS members(slice INTEGER, cl INTEGER, ref INTEGER, home INTEGER, share REAL, weight REAL);
CREATE TABLE IF NOT EXISTS partners(slice INTEGER, ref INTEGER, partner INTEGER, support INTEGER, lift REAL, cosine REAL);
CREATE TABLE IF NOT EXISTS names(ref INTEGER PRIMARY KEY, name TEXT, cite TEXT, year INTEGER);
CREATE TABLE IF NOT EXISTS links(level TEXT, slice_from INTEGER, cl_from INTEGER, slice_to INTEGER, cl_to INTEGER,
    shared INTEGER, jaccard REAL);
CREATE TABLE IF NOT EXISTS lineages(lineage INTEGER PRIMARY KEY, level TEXT, first_slice INTEGER, first_cl INTEGER,
    last_slice INTEGER, anchor INTEGER);
CREATE TABLE IF NOT EXISTS areas(area INTEGER PRIMARY KEY, lineages INTEGER, weight REAL, anchor_lineage INTEGER,
    first_slice INTEGER, last_slice INTEGER, built TEXT);
CREATE TABLE IF NOT EXISTS lineage_area(lineage INTEGER PRIMARY KEY, area INTEGER, weight REAL);
CREATE TABLE IF NOT EXISTS area_fp(area INTEGER, rank INTEGER, ref INTEGER, weight REAL);
CREATE TABLE IF NOT EXISTS area_label(area INTEGER PRIMARY KEY, key TEXT, jaccard REAL);
CREATE INDEX IF NOT EXISTS nodes_by_ref ON nodes(ref, slice);
CREATE INDEX IF NOT EXISTS nodes_by_slice ON nodes(slice, ref);
CREATE INDEX IF NOT EXISTS nodes_by_sub ON nodes(slice, sub);
CREATE INDEX IF NOT EXISTS clusters_by_key ON clusters(slice, cl);
CREATE INDEX IF NOT EXISTS subs_by_key ON subs(slice, sub);
CREATE INDEX IF NOT EXISTS members_by_ref ON members(ref, slice);
CREATE INDEX IF NOT EXISTS members_by_cl ON members(slice, cl);
CREATE INDEX IF NOT EXISTS partners_by_ref ON partners(ref, slice);
"""
# ("ref" = a cited case's cluster id; "cl" = a co-citation cluster's number within its slice.
#  CourtListener calls a case a cluster too, so the case side is called ref here.
#  A sub-cluster's number is cl * 1000 + j; j = 0 for a cluster too small to split.)


def label(s):
    return "%d-%02d" % (s, (s + WIDTH - 1) % 100)


def sub_label(code):
    return "%d.%d" % divmod(code, 1000) if code % 1000 else "%d" % (code // 1000)


def parse_label(t):
    """'2005-09:3' -> (2005, 3, None); '2005-09:3.2' -> (2005, 3, 3002)."""
    sl, _, cl = t.partition(":")
    k, _, j = cl.partition(".")
    return int(sl.split("-")[0]), int(k) if k else None, int(k) * 1000 + int(j) if j else None


# ================================================================ databases

def main_db():
    if not casedb.DB.exists():
        sys.exit("No database at %s (./casedb.py status)." % casedb.DB)
    if Path(str(casedb.DB) + ".loading").exists():
        sys.exit("casedb.py is loading right now; run this when it has finished.")
    con = sqlite3.connect("file:%s?mode=ro" % casedb.DB, uri=True, timeout=30)
    done = {r[0] for r in con.execute("SELECT name FROM loaded")}
    need = {"citation-map", "opinion-clusters", "opinions", "citations"} - done
    if need:
        sys.exit("cocite needs these loaded first: ./casedb.py load %s" % " ".join(sorted(need)))
    return con


def source_stamp(con):
    """What the results were computed from: the finish times of the source tables."""
    return "|".join(str(r) for r in con.execute(
        "SELECT name, finished FROM loaded WHERE name IN ('citation-map', 'opinion-clusters', 'opinions') ORDER BY name"))


def out_db():
    COCITE_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(COCITE_DB, timeout=60)
    db.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);")
    if (meta(db, "schema") or "1") != SCHEMA_VERSION:       # results are derived: drop and rebuild (names are kept)
        for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('meta', 'names')").fetchall():
            db.execute("DROP TABLE %s" % t)
        db.executescript(SCHEMA)
        meta(db, "schema", SCHEMA_VERSION)
        db.commit()
    db.executescript(SCHEMA)
    return db


def meta(db, key, value=None):
    if value is None:
        r = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


# ================================================================ prep: one pass over the database

def fetch_array(cur, ncol, chunk=1_000_000):
    parts = []
    while True:
        rows = cur.fetchmany(chunk)
        if not rows:
            break
        parts.append(np.array(rows, dtype=np.int64).reshape(-1, ncol))
    return np.concatenate(parts) if parts else np.zeros((0, ncol), np.int64)


def cmd_prep(args):
    con = main_db()
    stamp = source_stamp(con)
    CACHE.mkdir(parents=True, exist_ok=True)
    done = CACHE / "stamp.txt"
    if done.exists() and done.read_text() == stamp and not args.force:
        added = False
        if not (CACHE / "level.npy").exists():
            prep_levels(con)                   # cache from before court levels existed: add them only
            added = True
        if not (CACHE / "by_cited.npy").exists():
            prep_order()
            added = True
        if not added:
            print("cache is current (%s); --force to redo" % CACHE)
        return
    t0 = time.time()
    # Opinion -> case, from the two indexes on the 60 GB text table alone: each holds rowid,
    # so pairing them on rowid never touches the text.
    if not casedb.OPS_DB.exists():
        sys.exit("No opinion database at %s" % casedb.OPS_DB)
    ops = sqlite3.connect("file:%s?mode=ro" % casedb.OPS_DB, uri=True)
    a = fetch_array(ops.execute("SELECT rowid, id FROM opinions INDEXED BY opinions_by_id"), 2)
    b = fetch_array(ops.execute("SELECT rowid, coalesce(cluster_id, -1) FROM opinions INDEXED BY opinions_by_cluster"), 2)
    a = a[np.argsort(a[:, 0], kind="stable")]
    b = b[np.argsort(b[:, 0], kind="stable")]
    if len(a) != len(b) or not np.array_equal(a[:, 0], b[:, 0]):
        sys.exit("the two opinion indexes disagree (%d vs %d rows); is opinions.sqlite complete?" % (len(a), len(b)))
    op2ref = np.full(int(a[:, 1].max()) + 1, -1, np.int64)
    op2ref[a[:, 1]] = b[:, 1]
    print("opinions: %s -> cases  (%.1f min)" % (format(len(a), ","), (time.time() - t0) / 60), flush=True)
    del a, b

    t1 = time.time()
    yrs = fetch_array(con.execute("SELECT id, coalesce(CAST(substr(date_filed, 1, 4) AS INTEGER), 0) FROM clusters"), 2)
    ref_year = np.zeros(int(yrs[:, 0].max()) + 1, np.int16)
    yrs[(yrs[:, 1] < 1700) | (yrs[:, 1] > 2100), 1] = 0          # junk dates (one says year 19): no year
    ref_year[yrs[:, 0]] = yrs[:, 1]
    print("cases: %s filing years  (%.1f min)" % (format(len(yrs), ","), (time.time() - t1) / 60), flush=True)
    del yrs

    t1 = time.time()
    cur = con.execute("SELECT citing, cited FROM edges WHERE citing IS NOT NULL AND cited IS NOT NULL")
    keep_ci, keep_cd, keep_y, n_in = [], [], [], 0

    def ref_of(ops_ids):
        out = np.full(len(ops_ids), -1, np.int64)
        ok = ops_ids < len(op2ref)
        out[ok] = op2ref[ops_ids[ok]]
        return out

    while True:
        rows = cur.fetchmany(2_000_000)
        if not rows:
            break
        e = np.array(rows, dtype=np.int64)
        n_in += len(e)
        ci, cd = ref_of(e[:, 0]), ref_of(e[:, 1])
        ok = (ci >= 0) & (cd >= 0) & (ci != cd)
        ci, cd = ci[ok], cd[ok]
        y = np.zeros(len(ci), np.int16)
        inr = ci < len(ref_year)
        y[inr] = ref_year[ci[inr]]
        ok = y > 0
        keep_ci.append(ci[ok].astype(np.int32))
        keep_cd.append(cd[ok].astype(np.int32))
        keep_y.append(y[ok])
        if n_in % 10_000_000 < 2_000_000:
            print("  edges read: %s" % format(n_in, ","), flush=True)
    ci, cd, y = np.concatenate(keep_ci), np.concatenate(keep_cd), np.concatenate(keep_y)
    # one row per (citing decision, cited case): opinions of one decision collapse together
    key = np.unique((ci.astype(np.int64) << 32) | cd.astype(np.int64))
    ci, cd = (key >> 32).astype(np.int32), (key & 0xFFFFFFFF).astype(np.int32)
    y = ref_year[ci]
    np.save(CACHE / "citing.npy", ci)
    np.save(CACHE / "cited.npy", cd)
    np.save(CACHE / "year.npy", y)
    done.write_text(stamp)
    yrs_seen = y[y > 0]
    print("edges: %s opinion->opinion read, %s decision->case kept, citing years %d-%d  (%.1f min)" % (
        format(n_in, ","), format(len(ci), ","), yrs_seen.min(), yrs_seen.max(), (time.time() - t1) / 60))
    prep_levels(con)
    prep_order()
    print("prep done in %.1f min -> %s" % ((time.time() - t0) / 60, CACHE))


def prep_levels(con):
    """Court level of every case: clusters.docket_id -> dockets.court_id -> courts.jurisdiction.
    Two sequential scans (a join would seek on the hard disk once per case)."""
    t0 = time.time()
    if not con.execute("SELECT 1 FROM loaded WHERE name='dockets'").fetchone():
        sys.exit("court levels need the dockets table: ./casedb.py load dockets")
    con.execute("CREATE TEMP TABLE lv(id TEXT PRIMARY KEY, code INTEGER)")
    con.executemany("INSERT INTO temp.lv VALUES (?,?)",
                    [(cid, court_code(cid, jur)) for cid, jur in con.execute("SELECT id, jurisdiction FROM courts")])
    top = con.execute("SELECT max(id) FROM dockets").fetchone()[0] or 0
    dl = np.full(top + 1, 6, np.int8)
    cur = con.execute("SELECT d.id, coalesce(l.code, 6) FROM dockets d LEFT JOIN temp.lv l ON l.id = d.court_id "
                      "WHERE d.id IS NOT NULL")
    n = 0
    while True:
        rows = cur.fetchmany(2_000_000)
        if not rows:
            break
        a = np.array(rows, dtype=np.int64)
        dl[a[:, 0]] = a[:, 1]
        n += len(a)
    print("dockets: %s court levels  (%.1f min)" % (format(n, ","), (time.time() - t0) / 60), flush=True)
    top = con.execute("SELECT max(id) FROM clusters").fetchone()[0] or 0
    lvl = np.full(top + 1, 6, np.int8)
    cur = con.execute("SELECT id, coalesce(docket_id, -1) FROM clusters WHERE id IS NOT NULL")
    while True:
        rows = cur.fetchmany(2_000_000)
        if not rows:
            break
        a = np.array(rows, dtype=np.int64)
        ok = (a[:, 1] >= 0) & (a[:, 1] < len(dl))
        lvl[a[ok, 0]] = dl[a[ok, 1]]
    np.save(CACHE / "level.npy", lvl)
    print("cases: court level for %s  (%.1f min)" % (format(top, ","), (time.time() - t0) / 60))


def prep_order():
    """Edges sorted by cited case, so 'every decision citing X' is a binary search, not a scan.
    (The edges themselves are already sorted by citing decision.)"""
    t0 = time.time()
    cd = np.load(CACHE / "cited.npy")
    order = np.argsort(cd, kind="stable").astype(np.int32)
    np.save(CACHE / "by_cited.npy", order)
    np.save(CACHE / "cited_sorted.npy", cd[order])
    print("cited-case index: %s edges  (%.1f min)" % (format(len(cd), ","), (time.time() - t0) / 60))


def load_cache(stamp=None):
    s = CACHE / "stamp.txt"
    if not s.exists():
        sys.exit("No cache yet: run ./cocite.py prep")
    if stamp is not None and s.read_text() != stamp:
        sys.exit("The cache is from an older bulk load: run ./cocite.py prep")
    if not (CACHE / "level.npy").exists():
        sys.exit("The cache has no court levels yet: run ./cocite.py prep (it adds only those)")
    return (np.load(CACHE / "citing.npy", mmap_mode="r"), np.load(CACHE / "cited.npy", mmap_mode="r"),
            np.load(CACHE / "year.npy", mmap_mode="r"), s.read_text(), np.load(CACHE / "level.npy", mmap_mode="r"))


# ================================================================ one slice

def slice_pairs(ci, cd, p):
    """Citing decisions x cited cases for one slice -> nodes, pair arrays, counts."""
    from scipy import sparse
    rows_u, r = np.unique(ci, return_inverse=True)
    cols_u, c, n = np.unique(cd, return_inverse=True, return_counts=True)
    mc = max(p["min_cited"], int(np.ceil(p["min_cited_frac"] * len(rows_u))))
    keep = n >= mc
    newcol = np.full(len(cols_u), -1, np.int64)
    newcol[keep] = np.arange(keep.sum())
    c = newcol[c]
    ok = c >= 0
    r, c = r[ok], c[ok]
    refs = cols_u[keep]
    per_row = np.bincount(r, minlength=len(rows_u))
    pop = (per_row >= 1) & (per_row <= p["max_cites"])
    skipped = int((per_row > p["max_cites"]).sum())
    ok = pop[r]
    r, c = r[ok], c[ok]
    N = int(pop.sum())
    n = np.bincount(c, minlength=len(refs))          # citing decisions per node, inside the population
    if p.get("weight") and p.get("mix") is None:     # proximity weighting: pair weights from citation positions
        import cocite_positions
        ea, eb, es = cocite_positions.weighted_pairs(p["weight"], rows_u[pop].astype(np.int64),
                                                     refs.astype(np.int64), p["min_support"])[:3]
        return refs, n, N, skipped, ea, eb, es, mc
    M = sparse.csc_matrix((np.ones(len(r), np.int32), (r, c)), shape=(len(rows_u), len(refs)))
    MT = M.T.tocsr()
    ea, eb, es = [], [], []
    blk = 2000
    for j0 in range(0, len(refs), blk):
        j1 = min(j0 + blk, len(refs))
        B = (MT[j0:j1] @ M).tocoo()                   # (j1-j0) x nodes, entry = support
        a = B.row.astype(np.int64) + j0
        keep = (B.col > a) & (B.data >= p["min_support"])
        ea.append(a[keep])
        eb.append(B.col[keep].astype(np.int64))
        es.append(B.data[keep].astype(np.int64))
    if not ea:                                       # no case cited --min-cited times
        ea = eb = es = np.zeros(0, np.int64)
    else:
        ea, eb, es = np.concatenate(ea), np.concatenate(eb), np.concatenate(es)
    if p.get("weight") and len(ea):                  # --mix: the binary pairs, weights mix * support + (1 - mix) * proximity
        import cocite_positions
        pa, pb, pw = cocite_positions.weighted_pairs(p["weight"], rows_u[pop].astype(np.int64),
                                                     refs.astype(np.int64), 1e-12)[:3]
        bits = max(1, len(refs).bit_length())
        kb_ = (ea << bits) | eb
        kp = (pa << bits) | pb
        o = np.argsort(kp)
        kp, pw = kp[o], pw[o]
        j = np.minimum(np.searchsorted(kp, kb_), max(len(kp) - 1, 0))
        prox = np.where((len(kp) > 0) & (kp[j] == kb_), pw[j], 0.0) if len(kp) else np.zeros(len(ea))
        m = float(p["mix"])
        es = m * es + (1 - m) * prox
    return refs, n, N, skipped, ea, eb, es, mc


def leiden(nn, ea, eb, es, p):
    import igraph as ig
    random.seed(p["seed"])
    g = ig.Graph(n=nn, edges=np.column_stack([ea, eb]).tolist(), directed=False)
    g.es["w"] = es.astype(float).tolist()
    part = g.community_leiden(objective_function="modularity", weights="w",
                              resolution=p["resolution"], n_iterations=-1)
    memb = np.array(part.membership, np.int64)
    q = g.modularity(memb.tolist(), weights="w", resolution=p["resolution"]) if len(ea) else 0.0
    return memb, q


def null_q(nn, ea, eb, es, p, reps):
    """Modularity Leiden finds in the same graph rewired at random: every node keeps its number
    of partners, the weights are shuffled across edges. Mean and standard deviation over reps."""
    import igraph as ig
    rng = np.random.default_rng(p["seed"])
    edges = np.column_stack([ea, eb]).tolist()
    qs = []
    for r in range(reps):
        random.seed(p["seed"] + 1 + r)
        g = ig.Graph(n=nn, edges=edges, directed=False)
        g.rewire(n=10 * len(ea))
        g.es["w"] = rng.permutation(es).astype(float).tolist()
        part = g.community_leiden(objective_function="modularity", weights="w",
                                  resolution=p["resolution"], n_iterations=-1)
        qs.append(g.modularity(part.membership, weights="w", resolution=p["resolution"]))
    return float(np.mean(qs)), float(np.std(qs, ddof=1)) if reps > 1 else 0.0


def build_slice(db, s, ci, cd, p, stamp):
    t0 = time.time()
    refs, n, N, skipped, ea, eb, es, mc = slice_pairs(ci, cd, p)
    nn = len(refs)
    memb = q = None
    if nn and len(ea):
        memb, q = leiden(nn, ea, eb, es, p)
    if memb is None or np.bincount(memb).max() < p["min_size"]:
        if N:                                    # empty slices (junk dates before 1790) say nothing
            print("%s: %s citing decisions, %d cases cited %d+ times, %d pairs: too thin to cluster, skipped" % (
                label(s), format(N, ","), nn, mc, len(ea)))
        for t in ("nodes", "clusters", "subs", "members", "partners", "slices"):
            db.execute("DELETE FROM %s WHERE slice=?" % t, (s,))
        db.commit()
        return
    # renumber clusters by size, 1 = largest; drop the small ones (-1 = unclustered)
    sizes = np.bincount(memb, minlength=1) if nn else np.zeros(0, np.int64)
    order = np.argsort(-sizes, kind="stable")
    rank = np.full(len(sizes), -1, np.int64)
    big = order[sizes[order] >= p["min_size"]]
    rank[big] = np.arange(1, len(big) + 1)
    memb = rank[memb] if nn else memb
    K = len(big)

    # strength: each node's pair weight, split by the cluster at the other end
    src, oth, w = np.concatenate([ea, eb]), np.concatenate([eb, ea]), np.concatenate([es, es]).astype(float)
    W = np.bincount(src, weights=w, minlength=nn)
    oc = memb[oth] if nn else oth
    ok = oc > 0
    key = src[ok] * (K + 1) + oc[ok]
    uk, inv = np.unique(key, return_inverse=True)
    sw = np.bincount(inv, weights=w[ok])
    node, clus = uk // (K + 1), uk % (K + 1)
    share = sw / np.maximum(W[node], 1)
    home = clus == memb[node]
    keep = home | (share >= p["share"])

    # partners: top --top per node by cosine
    nf = n.astype(float)
    cos = es / np.sqrt(nf[ea] * nf[eb]) if len(ea) else es.astype(float)
    lift = es * N / (nf[ea] * nf[eb]) if len(ea) else es.astype(float)
    ps, po = np.concatenate([ea, eb]), np.concatenate([eb, ea])
    pc, pl, psu = np.concatenate([cos, cos]), np.concatenate([lift, lift]), np.concatenate([es, es])
    o = np.lexsort((-pc, ps))
    ps, po, pc, pl, psu = ps[o], po[o], pc[o], pl[o], psu[o]
    starts = np.r_[0, np.flatnonzero(np.diff(ps)) + 1] if len(ps) else np.zeros(0, np.int64)
    first = np.repeat(starts, np.diff(np.r_[starts, len(ps)])) if len(ps) else starts
    top = (np.arange(len(ps)) - first) < p["top"]

    # anchor (the cluster's name): the home member with the most weight inside the cluster,
    # times the share of its weight that stays inside, so a case straddling two clusters
    # does not name either
    internal = np.zeros(nn)
    hn = node[home]
    internal[hn] = sw[home]
    score = internal ** 2 / np.maximum(W, 1)
    anchors, inner = {}, {}
    for i in np.flatnonzero(memb > 0):
        k = int(memb[i])
        if k not in anchors or score[i] > score[anchors[k]]:
            anchors[k] = i
        inner[k] = inner.get(k, 0.0) + internal[i]
    csize = np.bincount(memb[memb > 0], minlength=K + 1) if K else np.zeros(1, np.int64)

    # sub-clusters: every cluster over --split-over is clustered again on its own pairs
    sub = np.where(memb > 0, memb * 1000, -1)
    inside = (memb[ea] == memb[eb]) & (memb[ea] > 0)
    ia, ib, iw, ik = ea[inside], eb[inside], es[inside], memb[ea][inside]
    o = np.argsort(ik, kind="stable")
    ia, ib, iw, ik = ia[o], ib[o], iw[o], ik[o]
    by_cl = np.argsort(memb, kind="stable")
    msorted = memb[by_cl]
    loc = np.full(nn, -1, np.int64)
    split = unsplit = 0
    for k in range(1, K + 1):
        if csize[k] < p["split_over"]:
            continue
        mem = by_cl[np.searchsorted(msorted, k):np.searchsorted(msorted, k + 1)]
        lo, hi = np.searchsorted(ik, k), np.searchsorted(ik, k + 1)
        loc[mem] = np.arange(len(mem))
        la, lb = loc[ia[lo:hi]], loc[ib[lo:hi]]
        loc[mem] = -1
        if len(la) < 3:
            continue
        sm, sq = leiden(len(mem), la, lb, iw[lo:hi], p)
        m0, sd0 = null_q(len(mem), la, lb, iw[lo:hi], p, p["nulls"])
        if sq <= m0 or (sq - m0) < p["sub_z"] * max(sd0, 1e-4):     # no better than chance: keep it whole
            unsplit += 1
            continue
        sz = np.bincount(sm)
        so = np.argsort(-sz, kind="stable")
        rk = np.zeros(len(sz), np.int64)
        keep_s = so[sz[so] >= p["min_size"]][:999]
        rk[keep_s] = np.arange(1, len(keep_s) + 1)
        sub[mem] = np.where(rk[sm] > 0, k * 1000 + rk[sm], -1)
        split += 1
    same_s = (sub[ea] == sub[eb]) & (sub[ea] > 0)
    subw = (np.bincount(ea[same_s], weights=es[same_s], minlength=nn) +
            np.bincount(eb[same_s], weights=es[same_s], minlength=nn)).astype(float)
    sscore = subw ** 2 / np.maximum(W, 1)
    s_anchor, s_inner, s_size = {}, {}, {}
    for i in np.flatnonzero(sub > 0):
        j = int(sub[i])
        if j not in s_anchor or sscore[i] > sscore[s_anchor[j]]:
            s_anchor[j] = i
        s_inner[j] = s_inner.get(j, 0.0) + subw[i]
        s_size[j] = s_size.get(j, 0) + 1

    R = refs.astype(np.int64)
    db.execute("BEGIN")
    for t in ("nodes", "clusters", "subs", "members", "partners"):
        db.execute("DELETE FROM %s WHERE slice=?" % t, (s,))
    db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
                   zip([s] * nn, R.tolist(), n.tolist(), memb.tolist(), W.tolist(), sub.tolist(), subw.tolist()))
    db.executemany("INSERT INTO subs VALUES (?,?,?,?,?,NULL)",
                   [(s, j, s_size[j], int(R[s_anchor[j]]), s_inner[j] / 2) for j in sorted(s_anchor)])
    db.executemany("INSERT INTO clusters VALUES (?,?,?,?,?,NULL)",
                   [(s, k, int(csize[k]), int(R[anchors[k]]), inner[k] / 2) for k in sorted(anchors)])
    db.executemany("INSERT INTO members VALUES (?,?,?,?,?,?)",
                   zip([s] * int(keep.sum()), clus[keep].tolist(), R[node[keep]].tolist(),
                       home[keep].astype(int).tolist(), share[keep].tolist(), sw[keep].tolist()))
    db.executemany("INSERT INTO partners VALUES (?,?,?,?,?,?)",
                   zip([s] * int(top.sum()), R[ps[top]].tolist(), R[po[top]].tolist(),
                       psu[top].tolist(), pl[top].tolist(), pc[top].tolist()))
    clustered = int((memb > 0).sum())
    q0, q0sd = null_q(nn, ea, eb, es, p, 3)
    db.execute("INSERT OR REPLACE INTO slices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?,?,?,?)",
               (s, label(s), stamp, json.dumps(p, sort_keys=True), N, skipped, nn, len(ea), K, clustered,
                q, time.time() - t0, len(s_anchor), mc, q0, q0sd))
    db.commit()
    print("%s: %s citing decisions (%d left out, > %d cites), %s cases cited %d+ times, %s pairs, %d clusters "
          "holding %s cases, %d split (%d no better than chance), %d sub-clusters, modularity %.3f vs %.3f by chance"
          "  (%.1f min)" % (
              label(s), format(N, ","), skipped, p["max_cites"], format(nn, ","), mc, format(len(ea), ","), K,
              format(clustered, ","), split, unsplit, len(s_anchor), q, q0, (time.time() - t0) / 60), flush=True)


# ================================================================ lineage across slices

LEVELS = (("cluster", "clusters", "cl"), ("sub", "subs", "sub"))


def build_lineage(db, carry):
    built = [r[0] for r in db.execute("SELECT slice FROM slices ORDER BY slice")]
    db.execute("BEGIN")
    db.execute("DELETE FROM links")
    db.execute("DELETE FROM lineages")
    next_id = 1
    for level, table, col in LEVELS:
        db.execute("UPDATE %s SET lineage=NULL" % table)
        prev_lin = {}
        for idx, s in enumerate(built):
            cur = {k: None for (k,) in db.execute("SELECT %s FROM %s WHERE slice=?" % (col, table), (s,))}
            t = built[idx - 1] if idx and built[idx - 1] == s - WIDTH else None
            if t is not None:
                here = dict(db.execute("SELECT ref, %s FROM nodes WHERE slice=?" % col, (s,)))
                before = dict(db.execute("SELECT ref, %s FROM nodes WHERE slice=?" % col, (t,)))
                both = here.keys() & before.keys()
                size_t, size_s, shared = {}, {}, {}
                for ref in both:
                    kt, ks = before[ref], here[ref]
                    if kt > 0:
                        size_t[kt] = size_t.get(kt, 0) + 1
                    if ks > 0:
                        size_s[ks] = size_s.get(ks, 0) + 1
                    if kt > 0 and ks > 0:
                        shared[(kt, ks)] = shared.get((kt, ks), 0) + 1
                jac = {(kt, ks): m / (size_t[kt] + size_s[ks] - m) for (kt, ks), m in shared.items()}
                db.executemany("INSERT INTO links VALUES (?,?,?,?,?,?,?)",
                               [(level, t, kt, s, ks, m, jac[(kt, ks)]) for (kt, ks), m in shared.items()
                                if m >= 3 and jac[(kt, ks)] >= 0.05])
                best_fwd, best_back = {}, {}
                for (kt, ks), j in jac.items():
                    if j > best_fwd.get(kt, (None, -1))[1]:
                        best_fwd[kt] = (ks, j)
                    if j > best_back.get(ks, (None, -1))[1]:
                        best_back[ks] = (kt, j)
                for ks in cur:
                    kt, j = best_back.get(ks, (None, 0))
                    if kt is not None and j >= carry and best_fwd[kt][0] == ks and kt in prev_lin:
                        cur[ks] = prev_lin[kt]
            for ks in sorted(cur):
                if cur[ks] is None:
                    anchor = db.execute("SELECT anchor FROM %s WHERE slice=? AND %s=?" % (table, col), (s, ks)).fetchone()[0]
                    db.execute("INSERT INTO lineages VALUES (?,?,?,?,?,?)", (next_id, level, s, ks, s, anchor))
                    cur[ks] = next_id
                    next_id += 1
                else:
                    db.execute("UPDATE lineages SET last_slice=? WHERE lineage=?", (s, cur[ks]))
                db.execute("UPDATE %s SET lineage=? WHERE slice=? AND %s=?" % (table, col), (cur[ks], s, ks))
            prev_lin = cur
        n_lin = db.execute("SELECT count(*) FROM lineages WHERE level=?", (level,)).fetchone()[0]
        print("lineage (%s level): %d lineages over %d slices" % (level, n_lin, len(built)), flush=True)
    db.commit()


# ================================================================ names, for display

def fill_names(db, con):
    """Name, best citation and year for every node case. For many cases, one sequential scan of
    clusters and cites beats index lookups: on the hard disk each lookup is a seek."""
    if COCITE_DB != BASE_DB and BASE_DB.exists():          # a court view: borrow the names already looked up
        db.execute("ATTACH DATABASE ? AS base", (str(BASE_DB),))
        db.execute("INSERT OR IGNORE INTO names SELECT * FROM base.names")
        db.commit()
        db.execute("DETACH DATABASE base")
    have = {r[0] for r in db.execute("SELECT ref FROM names")}
    want = sorted({r[0] for r in db.execute("SELECT DISTINCT ref FROM nodes")} - have)
    if not want:
        return
    t0 = time.time()
    info, cites = {}, {}
    if len(want) > 20000:
        wanted = np.zeros(max(want) + 1, bool)
        wanted[want] = True
        print("names: scanning clusters and cites for %s cases" % format(len(want), ","), flush=True)
        for cid, short, full, filed in con.execute("SELECT id, case_name_short, case_name, date_filed FROM clusters"):
            if cid is not None and cid < len(wanted) and wanted[cid]:
                info[cid] = (short, full, filed)
        for cid, v, rep, pg, typ in con.execute("SELECT cluster_id, volume, reporter, page, type FROM cites"):
            if cid is not None and cid < len(wanted) and wanted[cid] and (cid not in cites or (typ or 99) < cites[cid][0]):
                cites[cid] = (typ or 99, "%s %s %s" % (v, rep, pg))
    else:
        for i in range(0, len(want), 500):
            ids = want[i:i + 500]
            q = ",".join("?" * len(ids))
            for r in con.execute("SELECT id, case_name_short, case_name, date_filed FROM clusters WHERE id IN (%s)" % q, ids):
                info[r[0]] = r[1:]
            for cid, v, rep, pg, typ in con.execute(
                    "SELECT cluster_id, volume, reporter, page, type FROM cites WHERE cluster_id IN (%s)" % q, ids):
                if cid not in cites or (typ or 99) < cites[cid][0]:
                    cites[cid] = (typ or 99, "%s %s %s" % (v, rep, pg))
    rows = []
    for cid in want:
        short, full, filed = info.get(cid, (None, None, None))
        name = short or full or "cluster %d" % cid
        if not short and full and len(full) > 60:
            name = full[:57] + "..."
        rows.append((cid, name, cites.get(cid, (0, ""))[1], int(filed[:4]) if filed and filed[:4].isdigit() else None))
    db.executemany("INSERT OR REPLACE INTO names VALUES (?,?,?,?)", rows)
    db.commit()
    print("names: %s added  (%.1f min)" % (format(len(rows), ","), (time.time() - t0) / 60))



# ================================================================ the case-note section

NOTE_VIEW = "appellate-sentence-m0.2"   # the view case notes are drawn from: appellate courts, pair weights
                                        # 0.2 x decisions citing both + 0.8 x decisions citing both in one sentence.
                                        # Chosen 2026-10-02 on the area benchmark (consensus areas, 65 common names:
                                        # F1 0.914 vs 0.849 binary, precision 1.000 vs 0.953); see ARCHITECTURE.md 7.4


def open_view(view=NOTE_VIEW):
    """Read-only connection to a view's results, or None when it hasn't been built.
    (Read-only, so parallel note builds never lock each other or a running build.)"""
    path = BASE_DB if view in (None, "all") else BASE_DB.with_name("cocite-%s.sqlite" % view)
    if not path.exists():
        return None
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)


def unit_name(db, s, k, level="cluster"):
    """The label's name for a cluster or sub-cluster, else its anchor case's short name."""
    table, col = ("subs", "sub") if level == "sub" else ("clusters", "cl")
    r = db.execute("SELECT anchor, lineage FROM %s WHERE slice=? AND %s=?" % (table, col), (s, k)).fetchone()
    if not r:
        return None, None, None
    lab = label_of(db, r[1])
    lin = db.execute("SELECT first_slice FROM lineages WHERE lineage=?", (r[1],)).fetchone()
    return (lab[0] if lab else "around %s" % short(db, r[0])), r[1], (lin[0] if lin else None)


def citing_breakdown(ref, db, view=NOTE_VIEW):
    """Each slice's decisions citing this case, sorted by the cluster (and sub-cluster) most of their
    other cites fall in: what kinds of cases use it. Cached per case under cocite-cache/notes/."""
    if not (CACHE / "by_cited.npy").exists():
        return None
    built = db.execute("SELECT max(built) FROM slices").fetchone()[0]
    cpath = CACHE / "notes" / (view or "all") / ("%d.json" % ref)
    try:
        hit = json.loads(cpath.read_text())
        if hit.get("built") == built:
            return {int(k): v for k, v in hit["slices"].items()}
    except (FileNotFoundError, ValueError):
        pass
    ci = np.load(CACHE / "citing.npy", mmap_mode="r")
    cd = np.load(CACHE / "cited.npy", mmap_mode="r")
    y = np.load(CACHE / "year.npy", mmap_mode="r")
    lvl = np.load(CACHE / "level.npy", mmap_mode="r")
    order = np.load(CACHE / "by_cited.npy", mmap_mode="r")
    cds = np.load(CACHE / "cited_sorted.npy", mmap_mode="r")
    lo, hi = np.searchsorted(cds, ref), np.searchsorted(cds, ref, side="right")
    rows = np.asarray(order[lo:hi]).astype(np.int64)
    D, Y = np.asarray(ci)[rows] if len(rows) else np.zeros(0, np.int32), np.asarray(y)[rows] if len(rows) else np.zeros(0)
    codes, _ = parse_courts(view)
    if codes and len(D):
        keep = np.isin(np.asarray(lvl)[D], codes)
        D, Y = D[keep], Y[keep]
    starts, ends = np.searchsorted(ci, D), np.searchsorted(ci, D, side="right")
    out = {}
    nodes_of = {}
    for i in range(len(D)):
        s = int(Y[i]) - int(Y[i]) % WIDTH
        if s not in nodes_of:
            nodes_of[s] = {r: (c, b) for r, c, b in db.execute("SELECT ref, cl, sub FROM nodes WHERE slice=?", (s,))}
        nm = nodes_of[s]
        cls, subs = Counter(), Counter()
        for r in np.asarray(cd[starts[i]:ends[i]]).tolist():
            if r != ref and r in nm:
                c, b = nm[r]
                if c and c > 0:
                    cls[c] += 1
                if b and b > 0:
                    subs[b] += 1
        slot = out.setdefault(s, {"n": 0, "cl": Counter(), "sub": Counter(), "none": 0})
        slot["n"] += 1
        if cls:
            slot["cl"][cls.most_common(1)[0][0]] += 1
            if subs:
                slot["sub"][subs.most_common(1)[0][0]] += 1
        else:
            slot["none"] += 1
    res = {s: {"n": v["n"], "none": v["none"], "cl": dict(v["cl"]), "sub": dict(v["sub"])} for s, v in out.items()}
    try:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        tmp = cpath.with_suffix(".%d.tmp" % os.getpid())
        tmp.write_text(json.dumps({"built": built, "slices": res}))
        tmp.replace(cpath)
    except OSError:
        pass
    return res


def pie_md(title, parts):
    """Mermaid pie: [(label, count)], largest first, the tail folded into 'other'."""
    parts = sorted(parts, key=lambda x: -x[1])
    head, tail = parts[:6], sum(n for _, n in parts[6:])
    if tail:
        head.append(("other", tail))
    colours = ["#2e8b57", "#4682b4", "#c9a227", "#b0506e", "#7b68ee", "#3fa7a0", "#8a8a8a"]
    init = '%%%%{init: {"themeVariables": {%s, "pieStrokeColor": "#222", "pieOpacity": "1"}}}%%%%' % ", ".join(
        '"pie%d": "%s"' % (i + 1, c) for i, c in enumerate(colours))
    return (["```mermaid", init, 'pie showData title %s' % title.replace('"', "'")]
            + ['    "%s" : %d' % (trim(lbl.replace('"', "'"), 64), n) for lbl, n in head if n] + ["```", ""])


def trim(t, n):
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(",:;") + "…"


_VAULT_NAMES = {}


def vault_links(db):
    """(lineage -> note, area -> note) from the last `vault` run over this view, or empty maps."""
    key = db.execute("PRAGMA database_list").fetchone()[2]
    if key not in _VAULT_NAMES:
        try:
            d = json.loads((HERE / "md-cocite" / ".names.json").read_text(encoding="utf-8"))
            ok = d.get("db") == Path(key).name
        except (FileNotFoundError, ValueError):
            d, ok = {}, False
        _VAULT_NAMES[key] = ({int(k): v for k, v in d.get("lineage", {}).items()},
                             {int(k): v for k, v in d.get("area", {}).items()}) if ok else ({}, {})
    return _VAULT_NAMES[key]


def case_md(ref, index=None, view=NOTE_VIEW, top=8, year=None):
    """Markdown lines for '## Co-citation' in a case note; [] when there is nothing to show."""
    db = open_view(view)
    if db is None:
        return []
    vl_lin, vl_area = vault_links(db)

    def vl(lin, text, cell=True):
        """text as a link to the lineage's vault note ('\\|' inside a table)."""
        nm = vl_lin.get(lin)
        if not nm or not text:
            return (text or "").replace("|", "/") if cell else text
        return "[[%s%s%s]]" % (nm, "\\|" if cell else "|", text.replace("|", "/"))

    def va(ar, text, cell=True):
        nm = vl_area.get(ar)
        if not nm or not text:
            return (text or "").replace("|", "/") if cell else text
        return "[[%s%s%s]]" % (nm, "\\|" if cell else "|", text.replace("|", "/"))
    rows = summary(ref, db, top) or []
    rows = [r for r in rows if r["home"]]
    if not rows:
        return []
    index = index or {}
    year = year or time.localtime().tm_year
    full = [r for r in rows if r["slice"] + WIDTH - 1 < year] or rows       # the newest complete slice
    now = full[-1]

    def link(ref2, text):
        e = index.get(str(ref2))
        return "[[%s|%s]]" % (e["slug"], text) if e else text

    sub_code = now["sub"]["code"] if now["sub"] else None
    c_name, c_lin, c_first = unit_name(db, now["slice"], now["home"])
    s_name, s_lin, s_first = unit_name(db, now["slice"], sub_code, "sub") if sub_code and sub_code % 1000 else (None, None, None)
    head_name, head_lin, head_first = (s_name, s_lin, s_first) if s_name else (c_name, c_lin, c_first)
    try:
        head_area = area_of(db, head_lin)
    except sqlite3.OperationalError:
        head_area = None
    L = ["## Co-citation", "",
         "*Which cases later %s decisions cite together with this one, in five-year slices of citing decisions. "
         "Clusters from `cocite.py`; cluster names and descriptions were written by Claude from each cluster's "
         "evidence and are not part of the record.*" % ("appellate" if view == "appellate" else "court"), "",
         "**%s:** %s%s. Since %s." % (now["label"], vl(head_lin, head_name, False),
                                     " — within *%s*" % vl(c_lin, c_name, False) if s_name else "",
                                     label(head_first) if head_first else now["label"]), ""]
    if head_area:
        try:
            c = db.execute("SELECT confidence FROM lineage_area_conf WHERE lineage=?", (head_lin,)).fetchone()
        except sqlite3.OperationalError:
            c = None
        edge = " *(boundary: only %d%% of its ties are inside this area)*" % round(100 * c[0]) if c and c[0] < 0.5 else ""
        L += ["**Area:** %s.%s" % (va(head_area, area_name(db, head_area), False), edge), ""]
    shown = []
    tag = lambda r: " (partial)" if r["slice"] + WIDTH - 1 >= year else ""
    # where it sits: the clusters
    L += ["### Where it sits", "", "*The cluster this case belongs to in each slice, and its share of the case's "
          "co-citation weight; then the sub-cluster within it.*", "",
          "| Slice | Cited by | Cluster | Share | Also in |", "|---|---:|---|---:|---|"]
    for r in rows:
        cn, cn_lin, _ = unit_name(db, r["slice"], r["home"])
        home_share = next((c["share"] for c in r["clusters"] if c["home"]), 0)
        also = "; ".join("`%d` %s %d%%" % (c["cl"], vl(*unit_name(db, r["slice"], c["cl"])[1::-1]), round(100 * c["share"]))
                         for c in r["clusters"] if not c["home"])
        L.append("| %s%s | %s | `%d` %s | %d%% | %s |" % (r["label"], tag(r), format(r["cited_by"], ","), r["home"],
                                                         vl(cn_lin, cn), round(100 * home_share), also))
    L += ["", "| Slice | Sub-cluster | Share | Area |", "|---|---|---:|---|"]
    for r in rows:
        sc = r["sub"]["code"] if r["sub"] else None
        if not (sc and sc % 1000):
            L.append("| %s%s | *cluster not split* | | |" % (r["label"], tag(r)))
            continue
        sn, sn_lin, _ = unit_name(db, r["slice"], sc, "sub")
        w = db.execute("SELECT weight, subw FROM nodes WHERE slice=? AND ref=?", (r["slice"], ref)).fetchone()
        sh = "%d%%" % round(100.0 * w[1] / w[0]) if w and w[0] else ""
        sl = db.execute("SELECT lineage FROM subs WHERE slice=? AND sub=?", (r["slice"], sc)).fetchone()
        if sl and sl[0] not in shown:
            shown.append(sl[0])
        try:
            a_id = area_of(db, sl[0]) if sl else None
            an = va(a_id, area_name(db, a_id)) if a_id else ""
        except sqlite3.OperationalError:
            an = ""
        L.append("| %s%s | `%s` %s | %s | %s |" % (r["label"], tag(r), sub_label(sc), vl(sn_lin, sn), sh, an or ""))
    L.append("")
    # who cites it
    who = citing_breakdown(ref, db, view)
    if who:
        L += ["### Who cites it", "", "*Each citing decision is placed in the cluster (and sub-cluster) most of its "
              "other citations fall in, so this shows what kinds of cases use this one.*", "",
              "| Slice | Decisions | Top citing clusters | Top citing sub-clusters |", "|---|---:|---|---|"]
        for s in sorted(who):
            v = who[s]
            if not v["n"]:
                continue
            fmt = lambda d, lvl: "; ".join("`%s` %s %d%%" % (sub_label(int(k)) if lvl == "sub" else k,
                                                             vl(*unit_name(db, s, int(k), lvl)[1::-1]) or "?",
                                                             round(100.0 * n / v["n"]))
                                           for k, n in sorted(d.items(), key=lambda x: -x[1])[:3])
            L.append("| %s%s | %s | %s | %s |" % (label(s), " (partial)" if s + WIDTH - 1 >= year else "",
                                                 format(v["n"], ","), fmt(v["cl"], "cluster"), fmt(v["sub"], "sub")))
        L.append("")
        cur = who.get(now["slice"])
        if cur and cur["n"]:
            L += pie_md("Who cites it, %s: clusters" % now["label"],
                        [(unit_name(db, now["slice"], int(k))[0] or k, n) for k, n in cur["cl"].items()])
            L += pie_md("Who cites it, %s: sub-clusters" % now["label"],
                        [(unit_name(db, now["slice"], int(k), "sub")[0] or k, n) for k, n in cur["sub"].items()])
    L += ["", "**Usually cited with, %s** (decisions citing both, and their share of the %s decisions citing this case):"
          % (now["label"], format(now["cited_by"], ",")), ""]
    for pt in sorted(now["partners"], key=lambda p: -p["support"]):     # chosen by cosine, listed by count
        e = index.get(str(pt["ref"]))
        text = "%s, %s (%s)" % (e["short"] or e["name"], e["cite"], e["year"]) if e else pt["name"]
        L.append("- %s · %s · %d%%" % (link(pt["ref"], text), format(pt["support"], ","),
                                      round(100.0 * pt["support"] / max(now["cited_by"], 1))))
    L.append("")
    for lin in shown:                     # one collapsed description per sub-cluster lineage in the table
        lab = label_of(db, lin)
        if lab:
            L += ["> [!info]- %s (written by Claude from the cluster evidence; not part of the record)" % lab[0],
                  "> " + lab[1], ""]
    L += ["*Codes like `5.3` are sub-cluster 3 of cluster 5 in that slice: `./cocite.py cluster %s:5.3 --courts %s`.*"
          % (now["label"], view), ""]
    return L


# ---------------------------------------------------------------- the same section for the PDF
COCITE_COLOR = "violet!10"        # box tint: like the headmatter's yellow, it marks text that is not the court's
CHART_COLOURS = ["2E8B57", "4682B4", "C9A227", "B0506E", "7B68EE", "3FA7A0"]   # = pie_md's, for the same eye
OTHER_COLOUR, NONE_COLOUR = "9A9A9A", "DDDDDD"
PDF_SLICES = 14                   # newest slices drawn in the PDF chart; the note has them all


def case_tex(ref, esc, view=NOTE_VIEW, top=8, year=None):
    """TeX for the PDF's co-citation box; '' when there is nothing to show. `esc` escapes text for
    pdflatex (case2md passes its own, so both outputs share one escaping path)."""
    db = open_view(view)
    if db is None:
        return ""
    rows = [r for r in (summary(ref, db, top) or []) if r["home"]]
    if not rows:
        return ""
    year = year or time.localtime().tm_year
    full = [r for r in rows if r["slice"] + WIDTH - 1 < year] or rows
    now = full[-1]
    partial = lambda s: s + WIDTH - 1 >= year
    E = lambda t, n=None: esc(trim(t, n) if n else t)
    lab = lambda s: esc(label(s).replace("-", "\u2013"))     # 2005\u201309, en dash as in print

    def lineage_of(s, k, level="cluster"):
        table, col = ("subs", "sub") if level == "sub" else ("clusters", "cl")
        r = db.execute("SELECT lineage FROM %s WHERE slice=? AND %s=?" % (table, col), (s, k)).fetchone()
        return r[0] if r else None

    sub_code = now["sub"]["code"] if now["sub"] else None
    c_name, c_lin, c_first = unit_name(db, now["slice"], now["home"])
    s_name, s_lin, s_first = unit_name(db, now["slice"], sub_code, "sub") if sub_code and sub_code % 1000 else (None, None, None)
    h_name, h_lin, h_first = (s_name, s_lin, s_first) if s_name else (c_name, c_lin, c_first)
    out = ["{\\scriptsize\\itshape Not part of the record. Which cases later %s decisions cite together with this one, "
           "in five-year slices of citing decisions (\\texttt{cocite.py}). Cluster names were written by Claude "
           "from each cluster's evidence.\\par}\\vspace{3pt}" % ("appellate" if view == "appellate" else "court")]
    out.append("\\textbf{%s:} %s%s. {\\footnotesize Since %s.}\\par" % (
        lab(now["slice"]), E(h_name), (" --- within \\emph{%s}" % E(c_name)) if s_name else "",
        lab(h_first or now["slice"])))
    try:
        ar = area_of(db, h_lin)
    except sqlite3.OperationalError:
        ar = None
    if ar:
        try:
            c = db.execute("SELECT confidence FROM lineage_area_conf WHERE lineage=?", (h_lin,)).fetchone()
        except sqlite3.OperationalError:
            c = None
        edge = " {\\footnotesize (boundary: %d\\%% of its ties inside)}" % round(100 * c[0]) if c and c[0] < 0.5 else ""
        out.append("\\textbf{Area:} %s.%s\\par" % (E(area_name(db, ar)), edge))

    # ---- chart 1: who cites it, slice by slice, coloured by the citing cluster's lineage
    who = citing_breakdown(ref, db, view) or {}
    sl = [s for s in sorted(who) if who[s]["n"]][-PDF_SLICES:]
    if sl:
        tot, name_of = Counter(), {}
        for s in sl:
            for k, n in who[s]["cl"].items():
                lin = lineage_of(s, int(k))
                tot[lin] += n
                name_of.setdefault(lin, unit_name(db, s, int(k))[0] or "cluster %s" % k)
        keys = [l for l, _ in tot.most_common(len(CHART_COLOURS)) if l is not None]
        col = {l: "cc%d" % i for i, l in enumerate(keys)}
        W, H, gap = 10.6, 0.36, 0.1                         # cm
        tz = []
        for i, s in enumerate(sl):
            v, y = who[s], -i * (H + gap)
            segs = [(col[l], n) for l in keys for k, n in v["cl"].items() if lineage_of(s, int(k)) == l]
            segs = [(c, sum(n for c2, n in segs if c2 == c)) for c in dict.fromkeys(c for c, _ in segs)]
            other = sum(v["cl"].values()) - sum(n for _, n in segs)
            segs += [("ccother", other), ("ccnone", v["none"])]
            x = 0.0
            for c, n in segs:
                if n <= 0:
                    continue
                w = W * n / v["n"]
                tz.append("\\fill[%s] (%.3f,%.3f) rectangle (%.3f,%.3f);" % (c, x, y, x + w, y - H))
                if w > 0.9 and c != "ccnone":
                    tz.append("\\node[font=\\tiny,text=white] at (%.3f,%.3f) {%d\\%%};" % (x + w / 2, y - H / 2,
                                                                                         round(100.0 * n / v["n"])))
                x += w
            tz.append("\\node[anchor=east,font=\\tiny] at (-0.1,%.3f) {%s%s\\ \\ %s};" % (
                y - H / 2, lab(s), "*" if partial(s) else "", format(v["n"], ",")))
        defs = "".join("\\definecolor{cc%d}{HTML}{%s}" % (i, h) for i, h in enumerate(CHART_COLOURS))
        defs += "\\definecolor{ccother}{HTML}{%s}\\definecolor{ccnone}{HTML}{%s}" % (OTHER_COLOUR, NONE_COLOUR)
        legend = " \\quad ".join("\\mbox{{\\color{%s}\\rule{0.7em}{0.7em}}\\ %s}" % (col[l], E(name_of[l], 48)) for l in keys)
        legend += " \\quad \\mbox{{\\color{ccother}\\rule{0.7em}{0.7em}}\\ other clusters} \\quad " \
                  "\\mbox{{\\color{ccnone}\\rule{0.7em}{0.7em}}\\ no other clustered cites}"
        out.append(defs)                                   # outside the minipage, so chart 2 sees them too
        out.append("\\vspace{5pt}\\begin{minipage}{\\linewidth}\\textbf{Who cites it.} {\\footnotesize Each citing decision is placed in the cluster "
                   "most of its other citations fall in; bars are the share of each slice's citing decisions "
                   "(number at left%s).}\\par\\vspace{3pt}" % ("; * partial slice" if any(partial(s) for s in sl) else ""))
        out.append("{\\centering\\begin{tikzpicture}%s\\end{tikzpicture}\\par}\\vspace{2pt}"
                   "{\\scriptsize\\raggedright %s\\par}\\end{minipage}\\par" % ("".join(tz), legend))
        # ---- chart 2: the newest full slice's citing sub-clusters, ranked, in their cluster's colour
        cur = who.get(now["slice"])
        if cur and cur["sub"]:
            subs = sorted(((int(k), n) for k, n in cur["sub"].items()), key=lambda x: -x[1])[:6]
            mx = subs[0][1]
            tz = []
            for i, (k, n) in enumerate(subs):
                y = -i * 0.42
                c = col.get(lineage_of(now["slice"], k // 1000), "ccother")
                tz.append("\\node[anchor=east,font=\\scriptsize,align=right] at (6.9,%.3f) {%s};" % (
                    y, E(unit_name(db, now["slice"], k, "sub")[0] or sub_label(k), 58)))
                tz.append("\\fill[%s] (7.0,%.3f) rectangle (%.3f,%.3f);" % (c, y - 0.15, 7.0 + 4.6 * n / mx, y + 0.15))
                tz.append("\\node[anchor=west,font=\\tiny] at (%.3f,%.3f) {%d\\%%};" % (7.05 + 4.6 * n / mx, y,
                                                                                     round(100.0 * n / cur["n"])))
            out.append("\\vspace{4pt}\\begin{minipage}{\\linewidth}{\\footnotesize\\textbf{Citing sub-clusters, %s} (share of its %s citing "
                       "decisions)\\par}\\vspace{2pt}{\\centering\\begin{tikzpicture}%s\\end{tikzpicture}\\par}\\end{minipage}\\par" % (
                           lab(now["slice"]), format(cur["n"], ","), "".join(tz)))

    # ---- where it sits: one table, cluster and sub-cluster side by side
    tr = []
    shown = []
    prev_area = None
    for r in rows[-PDF_SLICES:]:
        share = next((c["share"] for c in r["clusters"] if c["home"]), 0)
        cn = unit_name(db, r["slice"], r["home"])[0]
        sc = r["sub"]["code"] if r["sub"] else None
        if sc and sc % 1000:
            w = db.execute("SELECT weight, subw FROM nodes WHERE slice=? AND ref=?", (r["slice"], ref)).fetchone()
            sn = "\\textcolor{gray}{\\texttt{%s}} %s%s" % (sub_label(sc), E(unit_name(db, r["slice"], sc, "sub")[0], 70),
                                        " (%d\\%%)" % round(100.0 * w[1] / w[0]) if w and w[0] else "")
            slin = lineage_of(r["slice"], sc, "sub")
            if slin and slin not in shown:
                shown.append(slin)
        else:
            sn, slin = "\\emph{not split}", lineage_of(r["slice"], r["home"])
        try:
            an = area_name(db, area_of(db, slin)) if slin else ""
        except sqlite3.OperationalError:
            an = ""
        an, prev_area = ("" if an == prev_area else an), an
        tr.append("%s%s & %s & \\raggedright\\textcolor{gray}{\\texttt{%d}} %s (%d\\%%) & \\raggedright %s & \\raggedright %s \\tabularnewline" % (
            lab(r["slice"]), "*" if partial(r["slice"]) else "", format(r["cited_by"], ","), r["home"],
            E(cn, 60), round(100 * share), sn, E(an or "", 50)))
    out.append("\\vspace{5pt}\\begin{minipage}{\\linewidth}\\textbf{Where it sits.} {\\footnotesize The case's cluster in each slice and its share of "
               "the case's co-citation weight, then the sub-cluster within it; the area is shown where it changes.}\\par\\vspace{2pt}"
               "{\\scriptsize\\setlength{\\tabcolsep}{3pt}\\renewcommand{\\arraystretch}{1.15}\\raggedright"
               "\\noindent\\begin{tabular}{@{}l r p{0.27\\linewidth} p{0.32\\linewidth} p{0.2\\linewidth}@{}}"
               "\\textbf{Slice} & \\textbf{Cited by} & \\textbf{Cluster} & \\textbf{Sub-cluster} & \\textbf{Area}\\tabularnewline\\hline "
               "%s\\end{tabular}\\par}\\end{minipage}\\par" % "\n".join(tr))

    # ---- partners
    pts = sorted(now["partners"], key=lambda p: -p["support"])
    if pts:
        cells = ["%s & %s & %d\\%% \\\\" % (E(p["name"], 75), format(p["support"], ","),
                                         round(100.0 * p["support"] / max(now["cited_by"], 1))) for p in pts]
        out.append("\\vspace{5pt}\\textbf{Usually cited with, %s.} {\\footnotesize Decisions citing both, and their share "
                   "of the %s decisions citing this case.}\\par\\vspace{2pt}{\\scriptsize\\setlength{\\tabcolsep}{3pt}"
                   "\\noindent\\begin{tabular}{@{}p{0.6\\linewidth} r r@{}}%s\\end{tabular}\\par}" % (
                       lab(now["slice"]), format(now["cited_by"], ","), "\n".join(cells)))
    # ---- what the sub-clusters are
    notes = [label_of(db, l) for l in shown]
    notes = [n for n in notes if n]
    if notes:
        out.append("\\vspace{5pt}{\\footnotesize\\textbf{The sub-clusters above} (descriptions written by Claude from "
                   "the cluster evidence)\\par}" + "".join("{\\scriptsize\\textbf{%s.} %s\\par}\\vspace{1pt}" % (
                       E(n[0]), E(n[1])) for n in notes))
    return ("\\pdfbookmark[1]{Co-citation}{cocitation}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=%s!50!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n%s\n\\end{tcolorbox}\n\\vspace{6pt}\n") % (
        COCITE_COLOR, COCITE_COLOR.split("!")[0], "\n".join(out))


# ================================================================ vault: generated map of content
# `vault` writes md-cocite/: one note per area, one per lineage that reaches --min-size cases
# (an explainer: who joined and left each slice, merges and splits), and an index MOC.
# The folder is generator-owned (sync_vault.sh mirrors it with --delete); headings are fixed so
# a personal MOC elsewhere in the vault can transclude sections: ![[<note>#Timeline]].
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')


def vault_names(db, min_size=30):
    """lineage -> note name, for every lineage that gets a note; stable across rebuilds when labelled."""
    peak = {}
    for table, level in (("clusters", "cluster"), ("subs", "sub")):
        for lin, z in db.execute("SELECT lineage, max(size) FROM %s WHERE lineage IS NOT NULL GROUP BY lineage" % table):
            if z >= min_size:
                peak[lin] = level
    out, used = {}, Counter()
    for lin in sorted(peak):
        lab = label_of(db, lin)
        if lab:
            nm = lab[0]
        else:
            a = db.execute("SELECT anchor FROM lineages WHERE lineage=?", (lin,)).fetchone()
            nm = "Lineage %d (around %s)" % (lin, short(db, a[0]) if a else "?")
        nm = BAD_CHARS.sub("-", nm).strip()
        if peak[lin] == "cluster":
            nm += " (cluster)"
        used[nm] += 1
        out[lin] = nm
    for lin, nm in list(out.items()):                  # two lineages with one label: number them
        if used[nm] > 1:
            out[lin] = "%s (lineage %d)" % (nm, lin)
    return out


def area_note_name(db, ar):
    return BAD_CHARS.sub("-", area_name(db, ar) or "Area %s" % ar).strip()


def cmd_vault(args):
    db = need_db()
    t0 = time.time()
    out_dir = Path(args.out)
    index = {}
    try:
        index = json.loads((HERE / "cases" / "_index.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pass
    names = vault_names(db, args.min_size)
    nm_of = {r: (n, c, y) for r, n, c, y in db.execute("SELECT ref, name, cite, year FROM names")}

    def case(ref, pct=None):
        e = index.get(str(ref))
        n, c, y = nm_of.get(ref, ("case %d" % ref, "", None))
        text = "%s, %s (%s)" % (e.get("short") or e["name"], e["cite"], e["year"]) if e else \
               "%s%s%s" % (n, ", " + c if c else "", " (%s)" % y if y else "")
        text = text.replace("[", "(").replace("]", ")")
        return "[[%s|%s]]" % (e["slug"], text) if e else text

    def lin_link(lin, cell=False):
        nm = names.get(lin)
        if not nm:
            return "lineage %s" % lin
        return "[[%s]]" % nm
    # members of every (slice, unit), heaviest first
    mem = {"cluster": {}, "sub": {}}
    for s, ref, cl, w, sb, sw in db.execute("SELECT slice, ref, cl, weight, sub, subw FROM nodes"):
        if cl and cl > 0:
            mem["cluster"].setdefault((s, cl), []).append((w or 0.0, ref))
        if sb and sb > 0 and sb % 1000:
            mem["sub"].setdefault((s, sb), []).append((sw or 0.0, ref))
    for d in mem.values():
        for v in d.values():
            v.sort(reverse=True)
    unit_lin = {"cluster": {}, "sub": {}}
    for table, level, col in (("clusters", "cluster", "cl"), ("subs", "sub", "sub")):
        for s, k, lin, z in db.execute("SELECT slice, %s, lineage, size FROM %s" % (col, table)):
            unit_lin[level][(s, k)] = lin
    home = {}                                          # (slice, ref) -> (cluster lineage, sub lineage)
    for s, ref, cl, sb in db.execute("SELECT slice, ref, cl, sub FROM nodes"):
        home[(s, ref)] = (unit_lin["cluster"].get((s, cl)), unit_lin["sub"].get((s, sb)) if sb and sb % 1000 else None)
    links = {}
    for level, sf, cf, st, ct, sh, j in db.execute("SELECT level, slice_from, cl_from, slice_to, cl_to, shared, jaccard FROM links"):
        links.setdefault(level, []).append((sf, cf, st, ct, sh, j))
    try:
        area_of_lin = dict(db.execute("SELECT lineage, area FROM lineage_area"))
    except sqlite3.OperationalError:
        area_of_lin = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "Lineages").mkdir(exist_ok=True)
    (out_dir / "Areas").mkdir(exist_ok=True)
    stamp = "*Generated by `cocite.py vault` from %s on %s. Names and descriptions were written by Claude from " \
            "each cluster's evidence; not part of any record. Do not edit: this folder is rebuilt.*" % (
                COCITE_DB.name, time.strftime("%Y-%m-%d"))
    written = set()

    def write(rel, lines):
        p = out_dir / rel
        txt = "\n".join(lines) + "\n"
        if not p.exists() or p.read_text(encoding="utf-8") != txt:
            p.write_text(txt, encoding="utf-8")
        written.add(p.resolve())

    # ---- lineage notes
    for lin, nm in names.items():
        level, first, last, anchor = db.execute("SELECT level, first_slice, last_slice, anchor FROM lineages "
                                                "WHERE lineage=?", (lin,)).fetchone()
        table, col = ("subs", "sub") if level == "sub" else ("clusters", "cl")
        steps = db.execute("SELECT slice, %s, size FROM %s WHERE lineage=? ORDER BY slice" % (col, table), (lin,)).fetchall()
        lab = label_of(db, lin)
        ar = area_of_lin.get(lin)
        L = ["---", "aliases: [\"lineage %d\"]" % lin, "cocite: {lineage: %d, level: %s, first: \"%s\", last: \"%s\"%s}" % (
             lin, level, label(first), label(last), ", area: %d" % ar if ar else ""), "tags: [cocite/lineage]", "---",
             "# %s" % nm, "", stamp, ""]
        L += ["## Summary", ""]
        if lab:
            L += [lab[1], ""]
        L += ["- **Level:** %s" % ("sub-cluster" if level == "sub" else "cluster"),
              "- **Span:** %s to %s, %d slices" % (label(first), label(last), len(steps)),
              "- **Founded on:** %s" % case(anchor)]
        if ar:
            L.append("- **Area:** [[%s]]" % area_note_name(db, ar))
        if level == "sub":
            parents = Counter(unit_lin["cluster"].get((s, k // 1000)) for s, k, _ in steps)
            L.append("- **Inside clusters:** " + ", ".join(lin_link(p) for p, _ in parents.most_common() if p))
        L += [""]
        # core cases: weight summed over the lineage's slices
        tot = Counter()
        for s, k, _ in steps:
            for w, r in mem[level].get((s, k), []):
                tot[r] += w
        L += ["## Core cases", "", "*Heaviest members over the whole lineage (co-citation weight summed across slices).*", ""]
        L += ["%d. %s" % (i + 1, case(r)) for i, (r, _) in enumerate(tot.most_common(args.core))] + [""]
        # timeline
        L += ["## Timeline", "", "| Slice | Cases | Anchor | Joined | Left |", "|---|---:|---|---|---|"]
        prev = None
        story = []
        for s, k, z in steps:
            cur = mem[level].get((s, k), [])
            cur_set = {r for _, r in cur}
            a = db.execute("SELECT anchor FROM %s WHERE slice=? AND %s=?" % (table, col), (s, k)).fetchone()
            if prev is None:
                joined, left = [], []
                jtxt, ltxt = "*founded*", ""
            else:
                ps, pmem = prev
                pset = {r for _, r in pmem}
                joined = [r for _, r in cur if r not in pset]
                left = [r for _, r in pmem if r not in cur_set]
                jtxt = "%d" % len(joined) + ("" if not joined else ": " + "; ".join(case(r) for r in joined[:3]))
                ltxt = "%d" % len(left) + ("" if not left else ": " + "; ".join(case(r) for r in left[:3]))
                # where the leavers went
                dest = Counter()
                for r in left:
                    h = home.get((s, r))
                    dest[(h[1] if level == "sub" else h[0]) if h else None] += 1
                gone = dest.pop(None, 0)
                moves = ["%d to %s" % (n, lin_link(d)) for d, n in dest.most_common(3) if d and d != lin and d in names]
                if moves or gone:
                    story.append("- **%s:** of %d that left, %s%s." % (
                        label(s), len(left), "; ".join(moves) if moves else "",
                        ("%s%d no longer cited enough to be clustered" % ("; " if moves else "", gone)) if gone else ""))
            L.append("| %s | %d | %s | %s | %s |" % (label(s), len(cur), case(a[0]) if a else "",
                                                     jtxt.replace("|", "\\|"), ltxt.replace("|", "\\|")))
            prev = (s, cur)
        L.append("")
        # merges and splits from the slice-to-slice links
        ev = []
        keys = {(s, k) for s, k, _ in steps}
        for sf, cf, st, ct, sh, j in links.get(level, []):
            a_in, b_in = (sf, cf) in keys, (st, ct) in keys
            if a_in == b_in or j < args.link_min:
                continue
            other = unit_lin[level].get((st, ct) if a_in else (sf, cf))
            if other == lin or other is None:
                continue
            ev.append((st, ("ended" if st > last else "split") if a_in else "merge", other, sh, j))
        L += ["## Merges and splits", ""]
        if ev:
            L += ["- **%s:** %s %s (%d shared cases, overlap %.2f)" % (
                label(st), {"split": "part went to", "merge": "took in part of", "ended": "after it ended, its cases went to"}[kind],
                lin_link(o), sh, j)
                for st, kind, o, sh, j in sorted(ev)]
        else:
            L += ["*None above an overlap of %.2f.*" % args.link_min]
        L += [""]
        L += ["## Where leavers went", ""] + (story or ["*Nothing to report.*"]) + [""]
        write(Path("Lineages") / (nm + ".md"), L)

    # ---- area notes
    areas = db.execute("SELECT area, lineages, weight, anchor_lineage, first_slice, last_slice FROM areas "
                       "ORDER BY weight DESC").fetchall() if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='areas'").fetchone() else []
    alab = {}
    try:
        keyed = {x["key"]: x for x in load_labels()}
        for ar, k in db.execute("SELECT area, key FROM area_label"):
            if k in keyed:
                alab[ar] = keyed[k]
    except sqlite3.OperationalError:
        pass
    moc = []
    for ar, n, w, anc, f, t in areas:
        members = [l for l, in db.execute("SELECT lineage FROM lineage_area WHERE area=? ORDER BY weight DESC", (ar,))]
        noted = [l for l in members if l in names]
        if not noted:
            continue
        an = area_note_name(db, ar)
        L = ["---", "cocite: {area: %d, lineages: %d, first: \"%s\", last: \"%s\"}" % (ar, n, label(f) if f else "",
                                                                                     label(t) if t else ""),
             "tags: [cocite/area]", "---", "# %s" % an, "", stamp, "", "## Summary", ""]
        if ar in alab:
            L += [alab[ar]["description"], ""]
        L += ["- **Span:** %s to %s" % (label(f) if f else "?", label(t) if t else "?"),
              "- **Lineages:** %d (%d with notes; the rest are small and listed by number in `./cocite.py areas`)" % (
                  n, len(noted)), ""]
        L += ["## Core cases", ""] + ["%d. %s" % (i + 1, case(r)) for i, (r,) in enumerate(db.execute(
            "SELECT ref FROM area_fp WHERE area=? ORDER BY rank LIMIT ?", (ar, args.core)))] + [""]
        L += ["## Lineages", "", "| Lineage | Level | Span |", "|---|---|---|"]
        for l in noted:
            lv, a1, a2 = db.execute("SELECT level, first_slice, last_slice FROM lineages WHERE lineage=?", (l,)).fetchone()
            L.append("| [[%s]] | %s | %s to %s |" % (names[l], lv, label(a1), label(a2)))
        L.append("")
        write(Path("Areas") / (an + ".md"), L)
        moc.append((an, n, f, t, alab.get(ar, {}).get("description", "")))

    # ---- the index MOC
    L = ["---", "tags: [cocite/moc]", "---", "# Co-citation map", "", stamp, "",
         "Areas group sub-cluster lineages that later decisions cite together, pooled over all slices "
         "(`cocite.py areas`). The area level is a chosen cut of a nested structure, not a natural one.", "",
         "## Areas", ""]
    L += ["- [[%s]] · %d lineages · %s to %s%s" % (an, n, label(f) if f else "?", label(t) if t else "?",
                                                   (" — " + trim(d, 110)) if d else "") for an, n, f, t, d in moc]
    lone = [l for l in names if l not in area_of_lin]
    if lone:
        L += ["", "## Lineages in no area", "", "*Cluster-level lineages, and sub-cluster lineages the areas left out.*", ""]
        L += ["- %s" % lin_link(l) for l in sorted(lone, key=lambda l: names[l])]
    write(Path("Co-citation map.md"), L + [""])
    names_file = out_dir / ".names.json"               # for case notes: lineage -> note, area -> note
    names_file.write_text(json.dumps({"db": COCITE_DB.name, "lineage": {str(k): v for k, v in names.items()},
                                      "area": {str(ar): area_note_name(db, ar) for ar, *_ in areas}}), encoding="utf-8")
    stale = [p for p in out_dir.rglob("*.md") if p.resolve() not in written]
    print("vault: %d lineage notes, %d area notes -> %s%s  (%.1f min)" % (
        len(names), len(moc), out_dir, ("; %d old notes no longer generated (sync_vault.sh drops them from the vault; "
                                        "remove them here with: rm -r %s)" % (len(stale), out_dir)) if stale else "",
        (time.time() - t0) / 60))


# ================================================================ crawl: which cases to fetch around one
def crawl_set(ref, view=NOTE_VIEW, top=60, scope="current", year=None):
    """[(ref, why)] for casemap.py map --by cluster: the members of the case's sub-cluster lineage
    (its cluster when the cluster isn't split), heaviest first. scope="current" uses the newest full
    slice, plus cases that joined the same lineage in the partial slice after it; scope="all" pools
    every lineage the case has belonged to. None when the view isn't built or the case is in no slice."""
    db = open_view(view)
    if db is None:
        return None
    rows = db.execute("SELECT slice, cl, sub FROM nodes WHERE ref=? AND cl > 0 ORDER BY slice", (ref,)).fetchall()
    if not rows:
        return None
    year = year or time.localtime().tm_year
    full = [r for r in rows if r[0] + WIDTH - 1 < year] or rows

    def unit(s, cl, sb):
        if sb and sb > 0 and sb % 1000:
            r = db.execute("SELECT lineage FROM subs WHERE slice=? AND sub=?", (s, sb)).fetchone()
            return ("sub", r[0]) if r else None
        r = db.execute("SELECT lineage FROM clusters WHERE slice=? AND cl=?", (s, cl)).fetchone()
        return ("cluster", r[0]) if r else None

    def members(level, lin, slices=None):
        table, col, wcol = ("subs", "sub", "subw") if level == "sub" else ("clusters", "cl", "weight")
        w = Counter()
        for s, k in db.execute("SELECT slice, %s FROM %s WHERE lineage=?" % (col, table), (lin,)):
            if slices is None or s in slices:
                for r, x in db.execute("SELECT ref, %s FROM nodes WHERE slice=? AND %s=?" % (wcol, col), (s, k)):
                    w[r] += x or 0.0
        return w

    out, seen = [], {ref}
    if scope == "all":
        units = {u for u in (unit(*r) for r in rows) if u}
        tot = Counter()
        for lv, lin in units:
            tot.update(members(lv, lin))
        why = "in one of its lineages"
        ranked = [(r, why) for r, _ in tot.most_common()]
    else:
        s0, cl, sb = full[-1]
        u = unit(s0, cl, sb)
        if not u:
            return None
        nm = (label_of(db, u[1]) or ("lineage %d" % u[1],))[0]
        ranked = [(r, "%s, %s" % (nm, label(s0))) for r, _ in members(u[0], u[1], {s0}).most_common()]
        later = {s for (s,) in db.execute("SELECT DISTINCT slice FROM nodes WHERE slice > ?", (s0,))}
        ranked += [(r, "joined %s after %s" % (nm, label(s0))) for r, _ in members(u[0], u[1], later).most_common()]
    for r, why in ranked:
        if r not in seen:
            seen.add(r)
            out.append((r, why))
        if len(out) >= top:
            break
    return out


# ================================================================ commands

def params_of(args):
    return {k: getattr(args, k) for k in DEFAULTS}


def cmd_build(args):
    con = main_db()
    stamp = source_stamp(con)
    ci_all, cd_all, y_all, cstamp, lvl = load_cache(stamp)
    codes, tag = parse_courts(args.courts)
    if codes:
        ci_np = np.asarray(ci_all)
        keep = np.isin(np.asarray(lvl)[ci_np], codes)
        ci_all, cd_all, y_all = ci_np[keep], np.asarray(cd_all)[keep], np.asarray(y_all)[keep]
        print("courts %s: %s of %s citing edges kept" % (tag, format(int(keep.sum()), ","), format(len(keep), ",")))
    db = out_db()
    p = params_of(args)
    p["courts"] = tag
    if getattr(args, "weight", None):                # binary builds keep their old params (no rebuild)
        import cocite_positions
        cocite_positions.index()                     # fail now, not after the first slice
        p["weight"] = args.weight
        if args.mix is not None:
            p["mix"] = args.mix
    yv = np.asarray(y_all)
    first, last = int(yv[yv >= 1700].min()), int(yv.max())     # below 1700 = junk dates (see prep)
    every = list(range(first - first % WIDTH, last + 1, WIDTH))
    picks = [int(x) - int(x) % WIDTH for x in args.slices.split(",")] if args.slices else every
    print("slices %s to %s; parameters %s" % (label(picks[0]), label(picks[-1]), json.dumps(p, sort_keys=True)))
    order = np.argsort(y_all, kind="stable")
    ys = np.asarray(y_all)[order]
    for s in picks:
        old = db.execute("SELECT stamp, params FROM slices WHERE slice=?", (s,)).fetchone()
        if old and old[0] == stamp and json.loads(old[1]) == p and not args.force:
            print("%s: current" % label(s))
            continue
        lo, hi = np.searchsorted(ys, s), np.searchsorted(ys, s + WIDTH)
        sel = order[lo:hi]
        if len(sel) == 0:
            print("%s: no citing decisions" % label(s))
            continue
        build_slice(db, s, np.asarray(ci_all)[sel], np.asarray(cd_all)[sel], p, stamp)
    build_lineage(db, args.carry)
    fill_names(db, con)
    if LABELS.exists():
        match_labels(db)
    meta(db, "stamp", stamp)
    db.commit()


def need_db():
    if not COCITE_DB.exists():
        sys.exit("No %s yet: ./cocite.py prep, then ./cocite.py build" % COCITE_DB)
    return out_db()


def name(db, ref):
    r = db.execute("SELECT name, cite, year FROM names WHERE ref=?", (ref,)).fetchone()
    return ("%s, %s (%s)" % r) if r else "cluster %d" % ref


def short(db, ref):
    r = db.execute("SELECT name FROM names WHERE ref=?", (ref,)).fetchone()
    return r[0] if r else "cluster %d" % ref


def cl_title(db, s, k, level="cluster"):
    table, col, tag = ("subs", "sub", sub_label(k)) if level == "sub" else ("clusters", "cl", "%d" % k)
    r = db.execute("SELECT size, anchor, lineage FROM %s WHERE slice=? AND %s=?" % (table, col), (s, k)).fetchone()
    if not r:
        return "%s:%s" % (label(s), tag)
    lin = db.execute("SELECT anchor, first_slice FROM lineages WHERE lineage=?", (r[2],)).fetchone()
    tail = ", lineage %d" % r[2] if r[2] else ""
    if lin and (lin[1] != s):
        tail += " since %s (%s)" % (label(lin[1]), short(db, lin[0]))
    lab = label_of(db, r[2])
    head = "%s (%s)" % (lab[0], short(db, r[1])) if lab else short(db, r[1])
    return "%s:%s · %s  [%d cases%s]" % (label(s), tag, head, r[0], tail)


def cmd_status(args):
    db = need_db()
    print("%s  (%.2f GB)" % (COCITE_DB, COCITE_DB.stat().st_size / 1e9))
    print("%-8s %10s %6s %9s %11s %8s %10s %6s %6s %6s %6s" % (
        "slice", "citing", "cut", "cases", "pairs", "clusters", "clustered", "subs", "Q", "chance", "above"))
    rows = db.execute("SELECT label, citing, nodes, pairs, clusters, clustered, modularity, params, stamp, subs, "
                      "min_cited, q_null FROM slices ORDER BY slice").fetchall()
    common = max({r[7] for r in rows}, key=lambda x: sum(r[7] == x for r in rows)) if rows else None
    for r in rows:
        odd = "  (other parameters: %s)" % r[7] if r[7] != common else ""
        old = "  (older bulk load)" if r[8] != meta(db, "stamp") else ""
        print("%-8s %10s %6d %9s %11s %8d %10s %6d %6.3f %6.3f %6.3f%s%s" % (
            r[0], format(r[1], ","), r[10] or 0, format(r[2], ","), format(r[3], ","), r[4], format(r[5], ","),
            r[9] or 0, r[6], r[11] or 0, r[6] - (r[11] or 0), odd, old))
    print("cut = citing decisions a case needed in that slice; chance = modularity of the same graph rewired at random")
    if common:
        print("parameters: %s" % common)


def resolve(cite):
    import casemap
    con = main_db()
    return casemap.db_lookup(con, cite)


def summary(ref, db=None, top=8):
    """Everything a case note would show, slice by slice; None when the case is in no slice."""
    db = db or need_db()
    out = []
    for s, n, k, W, sb in db.execute("SELECT slice, n, cl, weight, sub FROM nodes WHERE ref=? ORDER BY slice", (ref,)).fetchall():
        mem = db.execute("SELECT cl, home, share FROM members WHERE slice=? AND ref=? ORDER BY share DESC", (s, ref)).fetchall()
        parts = db.execute("SELECT partner, support, lift, cosine FROM partners WHERE slice=? AND ref=? "
                           "ORDER BY cosine DESC LIMIT ?", (s, ref, top)).fetchall()
        out.append({"slice": s, "label": label(s), "cited_by": n, "home": k if k > 0 else None,
                    "sub": {"code": sb, "title": cl_title(db, s, sb, "sub")} if sb and sb > 0 else None,
                    "clusters": [{"cl": c, "title": cl_title(db, s, c), "home": bool(h), "share": round(sh, 3)} for c, h, sh in mem],
                    "partners": [{"ref": pr, "name": name(db, pr), "support": su, "lift": round(li, 1), "cosine": round(co, 3)}
                                 for pr, su, li, co in parts]})
    return out or None


def cmd_case(args):
    db = need_db()
    ref = args.cluster or resolve(args.cite)
    print(name(db, ref))
    rows = summary(ref, db, args.top) or []
    if args.slice:
        rows = [r for r in rows if r["slice"] == args.slice - args.slice % WIDTH]
    if not rows:
        print("  not in any slice (cited fewer than --min-cited times per slice, or not built yet)")
    for r in rows:
        print("\n%s  cited by %s decisions" % (r["label"], format(r["cited_by"], ",")))
        if not r["clusters"]:
            print("  no cluster")
        for c in r["clusters"]:
            print("  %s %4.0f%%  %s" % ("home" if c["home"] else "    ", 100 * c["share"], c["title"]))
            if c["home"] and r["sub"] and r["sub"]["code"] % 1000:
                print("    sub         %s" % r["sub"]["title"])
        for pt in r["partners"]:
            print("      with %-60s support %5d  lift %6.1f  cosine %.3f" % (pt["name"][:60], pt["support"], pt["lift"], pt["cosine"]))


def cmd_cluster(args):
    db = need_db()
    s, k, j = parse_label(args.which)
    s -= s % WIDTH
    if k is None or not db.execute("SELECT 1 FROM clusters WHERE slice=? AND cl=?", (s, k)).fetchone():
        sys.exit("no cluster %s (./cocite.py status lists the slices and their cluster counts)" % args.which)
    if j is not None:
        if not db.execute("SELECT 1 FROM subs WHERE slice=? AND sub=?", (s, j)).fetchone():
            sys.exit("no sub-cluster %s (./cocite.py cluster %s:%d lists them)" % (args.which, label(s), k))
        print(cl_title(db, s, j, "sub"))
        print("members, most weight inside the sub-cluster first:")
        for ref, n in db.execute("SELECT ref, n FROM nodes WHERE slice=? AND sub=? ORDER BY subw DESC LIMIT ?", (s, j, args.top)):
            print("  %6s cites  %s" % (format(n, ","), name(db, ref)))
        links_of(db, "sub", s, j)
        return
    print(cl_title(db, s, k))
    subs = db.execute("SELECT sub FROM subs WHERE slice=? AND sub/1000=? AND sub%1000>0 ORDER BY sub", (s, k)).fetchall()
    if subs:
        print("sub-clusters:")
        for (j,) in subs[:args.top]:
            print("  " + cl_title(db, s, j, "sub"))
        if len(subs) > args.top:
            print("  ... %d more" % (len(subs) - args.top))
    print("members, most weight inside the cluster first (home members, then others at >= --share):")
    for ref, home, share, w in db.execute("SELECT ref, home, share, weight FROM members WHERE slice=? AND cl=? "
                                          "ORDER BY home DESC, weight DESC LIMIT ?", (s, k, args.top)):
        print("  %s %4.0f%%  %s" % ("home" if home else "also", 100 * share, name(db, ref)))
    links_of(db, "cluster", s, k)


def links_of(db, level, s, k):
    for sql, word in (("SELECT slice_from, cl_from, shared, jaccard FROM links WHERE level=? AND slice_to=? AND cl_to=?", "from"),
                      ("SELECT slice_to, cl_to, shared, jaccard FROM links WHERE level=? AND slice_from=? AND cl_from=?", "into")):
        for s2, k2, m, j in db.execute(sql + " ORDER BY shared DESC LIMIT 8", (level, s, k)).fetchall():
            print("  %s %s  shared %d, Jaccard %.2f" % (word, cl_title(db, s2, k2, level), m, j))


def cmd_diag(args):
    ci, cd, y, _, lvl = load_cache()
    y = np.asarray(y)
    ok = y >= 1700
    sl = (y[ok] // WIDTH) * WIDTH
    key = np.unique((sl.astype(np.int64) << 32) | np.asarray(ci)[ok].astype(np.int64))   # one row per decision per slice
    s_of, c_of = (key >> 32), (key & 0xFFFFFFFF)
    code = np.asarray(lvl)[c_of]
    slices = np.unique(s_of)
    print("citing decisions per slice, by the citing court's level (share of the slice)")
    print("%-8s %9s " % ("slice", "decisions") + " ".join("%15s" % n for n in COURT_LEVELS))
    for s in slices:
        m = s_of == s
        cnt = np.bincount(code[m], minlength=7)[1:]
        tot = int(m.sum())
        print("%-8s %9s " % (label(int(s)), format(tot, ",")) +
              " ".join("%8s %5.1f%%" % (format(int(c), ","), 100 * c / tot) for c in cnt))


def cmd_note(args):
    ref = args.cluster or resolve(args.cite)
    try:
        import case2md
        index = case2md.load_index()
    except Exception:
        index = {}
    view = args.courts or NOTE_VIEW
    lines = case_md(ref, index, view)
    print("\n".join(lines) if lines else "nothing to show: case not clustered in %s, or that view isn't built" % view)


def cmd_tree(args):
    db = need_db()
    s = args.slice - args.slice % WIDTH
    if not db.execute("SELECT 1 FROM slices WHERE slice=?", (s,)).fetchone():
        sys.exit("slice %s is not built" % label(s))
    for (k,) in db.execute("SELECT cl FROM clusters WHERE slice=? ORDER BY cl LIMIT ?", (s, args.top)).fetchall():
        print(cl_title(db, s, k))
        subs = db.execute("SELECT sub FROM subs WHERE slice=? AND sub/1000=? AND sub%1000>0 ORDER BY sub", (s, k)).fetchall()
        for (j,) in subs[:args.subs]:
            print("    " + cl_title(db, s, j, "sub"))
        if len(subs) > args.subs:
            print("    ... %d more sub-clusters (./cocite.py cluster %s:%d)" % (len(subs) - args.subs, label(s), k))


def cmd_lineage(args):
    db = need_db()
    r = db.execute("SELECT first_slice, last_slice, anchor, level FROM lineages WHERE lineage=?", (args.id,)).fetchone()
    if not r:
        sys.exit("no lineage %d" % args.id)
    print("lineage %d (%s level): %s to %s, founded on %s" % (args.id, r[3], label(r[0]), label(r[1]), name(db, r[2])))
    lab = label_of(db, args.id)
    if lab:
        print("  %s: %s" % lab)
    table, col = ("subs", "sub") if r[3] == "sub" else ("clusters", "cl")
    for s, k in db.execute("SELECT slice, %s FROM %s WHERE lineage=? ORDER BY slice" % (col, table), (args.id,)):
        print("  " + cl_title(db, s, k, r[3]))


def cmd_together(args):
    db = need_db()
    refs = [resolve(c) for c in args.cite] + list(args.cluster or [])
    if len(refs) < 2:
        sys.exit("give two or more --cite / --cluster")
    for r in refs:
        print("  %s" % name(db, r))
    ci, cd, y, _, lvl = load_cache()
    codes, _tag = parse_courts(args.courts)
    if codes:
        keep = np.isin(np.asarray(lvl)[np.asarray(ci)], codes)
        ci, cd, y = np.asarray(ci)[keep], np.asarray(cd)[keep], np.asarray(y)[keep]
    cd_arr = np.asarray(cd)
    hits = {r: np.flatnonzero(cd_arr == r) for r in refs}
    ci_arr, y_arr = np.asarray(ci), np.asarray(y)
    slices = sorted({int(v) - int(v) % WIDTH for r in refs for v in y_arr[hits[r]]})
    print("\n%-8s %s" % ("slice", "home sub-cluster of each  |  decisions citing all of them"))
    for s in slices:
        homes, codes = [], []
        for r in refs:
            row = db.execute("SELECT cl, sub FROM nodes WHERE slice=? AND ref=?", (s, r)).fetchone()
            codes.append(row)
            homes.append("-" if not row else ("none" if row[0] <= 0 else sub_label(row[1]) if row[1] > 0 else "%d.-" % row[0]))
        sets = [set(ci_arr[hits[r]][(y_arr[hits[r]] >= s) & (y_arr[hits[r]] < s + WIDTH)].tolist()) for r in refs]
        both = set.intersection(*sets)
        verdict = ""
        if all(c and c[0] > 0 for c in codes):
            if len({c[1] for c in codes}) == 1 and codes[0][1] > 0:
                verdict = "SAME SUB-CLUSTER: " + cl_title(db, s, codes[0][1], "sub")
            elif len({c[0] for c in codes}) == 1:
                verdict = "same cluster only: " + cl_title(db, s, codes[0][0])
        print("%-8s %-24s %s  %s" % (label(s), " / ".join(homes), format(len(both), ",").rjust(7), verdict))



# ================================================================ areas: a level that holds still over time
# Clusters are rebuilt in every slice, so a doctrine's parent cluster can change name from one
# slice to the next. An area is built once, over all slices: its units are sub-cluster lineages,
# tied by (1) co-citation between their members, summed over every slice, and (2) continuity:
# when a case moves from lineage A to lineage B between adjacent slices, A and B are tied by that
# case's weight, so a doctrine's successive stages (which never share a slice) stay together.

def cmd_areas(args):
    import igraph as ig
    db = need_db()
    t0 = time.time()
    ci_all, cd_all, y_all, _, lvl = load_cache()
    built = db.execute("SELECT slice, params FROM slices ORDER BY slice").fetchall()
    if not built:
        sys.exit("no slices built")
    codes, _ = parse_courts(json.loads(built[0][1]).get("courts"))
    ci_np, cd_np, y_np = np.asarray(ci_all), np.asarray(cd_all), np.asarray(y_all)
    if codes:
        keep = np.isin(np.asarray(lvl)[ci_np], codes)
        ci_np, cd_np, y_np = ci_np[keep], cd_np[keep], y_np[keep]
    order = np.argsort(y_np, kind="stable")
    ys = y_np[order]
    lin_of = {}
    for s, ref, lin, sw in db.execute("SELECT n.slice, n.ref, s.lineage, n.subw FROM nodes n JOIN subs s "
                                      "ON s.slice = n.slice AND s.sub = n.sub WHERE n.sub > 0"):
        lin_of[(s, ref)] = (lin, sw or 0.0)
    keys, wts, self_w = [], [], Counter()
    for s, params in built:
        p = json.loads(params)
        lo, hi = np.searchsorted(ys, s), np.searchsorted(ys, s + WIDTH)
        sel = order[lo:hi]
        if not len(sel):
            continue
        refs, n, N, skipped, ea, eb, es, mc = slice_pairs(ci_np[sel], cd_np[sel], p)
        if not len(ea):
            continue
        lin = np.array([lin_of.get((s, int(r)), (0, 0))[0] for r in refs], np.int64)
        la, lb = lin[ea], lin[eb]
        ok = (la > 0) & (lb > 0)
        same = ok & (la == lb)
        if same.any():
            u, inv = np.unique(la[same], return_inverse=True)
            for l, w in zip(u.tolist(), np.bincount(inv, weights=es[same]).tolist()):
                self_w[l] += w
        diff = ok & (la != lb)
        a, b = np.minimum(la[diff], lb[diff]), np.maximum(la[diff], lb[diff])
        keys.append((a << 32) | b)
        wts.append(es[diff].astype(float))
        print("  %s: co-citation ties gathered" % label(s), flush=True)
    # continuity: a case's weight carries over when its lineage changes between adjacent slices
    seq = {}
    for (s, ref), (lin, sw) in lin_of.items():
        seq.setdefault(ref, []).append((s, lin, sw))
    ca, cw = [], []
    for ref, rows in seq.items():
        rows.sort()
        for (s1, l1, w1), (s2, l2, w2) in zip(rows, rows[1:]):
            if s2 == s1 + WIDTH and l1 != l2:
                ca.append((min(l1, l2) << 32) | max(l1, l2))
                cw.append(min(w1, w2) * args.continuity)
    if ca:
        keys.append(np.array(ca, np.int64))
        wts.append(np.array(cw, float))
    key = np.concatenate(keys) if keys else np.zeros(0, np.int64)
    wt = np.concatenate(wts) if wts else np.zeros(0)
    uk, inv = np.unique(key, return_inverse=True)
    w = np.bincount(inv, weights=wt) if len(uk) else np.zeros(0)
    a, b = (uk >> 32), (uk & 0xFFFFFFFF)
    allw = Counter()                                   # every lineage's tie weight, for the small ones
    peak = dict(db.execute("SELECT lineage, max(size) FROM subs GROUP BY lineage"))
    big = {l for l, z in peak.items() if z >= args.min_size}
    nodes = sorted(big & (set(a.tolist()) | set(b.tolist()) | set(self_w)))
    idx = {l: i for i, l in enumerate(nodes)}
    ka, kb, kw = [], [], []
    small_ties = {}
    for x, y, wv in zip(a.tolist(), b.tolist(), w.tolist()):
        if x in idx and y in idx:
            ka.append(idx[x]); kb.append(idx[y]); kw.append(wv)
        elif x in idx or y in idx:                       # a small lineage's tie to a real one
            sm, bg = (y, x) if x in idx else (x, y)
            d = small_ties.setdefault(sm, {})
            d[bg] = d.get(bg, 0.0) + wv
    ka, kb, kw = np.array(ka, np.int64), np.array(kb, np.int64), np.array(kw, float)
    raw_strength = np.bincount(ka, weights=kw, minlength=len(nodes)) + np.bincount(kb, weights=kw, minlength=len(nodes))
    kw_raw = kw.copy()
    med = float(np.median(raw_strength)) if len(raw_strength) else 1.0

    def weights_for(method, shrink):
        """cpm: association strength, the tie relative to what the two lineages' total ties predict
        (ties made only of shared hub cases fall to about 1). shrink adds k = shrink x the median
        strength to each side, so a small lineage's one incidental tie can't blow up."""
        if method != "cpm":
            return kw_raw
        k = shrink * med
        return kw_raw * raw_strength.sum() / np.maximum((raw_strength[ka] + k) * (raw_strength[kb] + k), 1e-9)

    def graph_for(method, shrink):
        gg = ig.Graph(n=len(nodes), edges=np.column_stack([ka, kb]).tolist(), directed=False)
        gg.es["w"] = weights_for(method, shrink).tolist()
        return gg

    g = graph_for(args.method, args.shrink)
    kw = np.asarray(g.es["w"])

    def run(gamma, seed, gg=None, method=None):
        gg, method = gg or g, method or args.method
        random.seed(seed)
        if method == "cpm":
            return gg.community_leiden(objective_function="CPM", weights="w", resolution=gamma, n_iterations=-1)
        return gg.community_leiden(objective_function="modularity", weights="w", resolution=gamma, n_iterations=-1)

    if args.grid:
        gold = load_gold(db, args.gold, idx)
        if not gold:
            sys.exit("no benchmark groups matched lineages here (%s)" % args.gold)
        print("benchmark: %d groups, %d lineages" % (len(set(gold.values())), len(gold)))
        print("%-10s %7s %7s %7s %7s %7s %8s %7s" % ("method", "gamma", "shrink", "areas5+", "largest", "F1", "precis.", "recall"))
        for spec in args.grid.split(";"):
            method, _, rest = spec.partition(":")
            gammas, _, shr = rest.partition("@")
            for shrink in ([float(x) for x in shr.split(",")] if shr else [0.0]):
                gg = graph_for(method, shrink)
                for gamma in [float(x) for x in gammas.split(",")]:
                    m0 = np.array(run(gamma, args.seed, gg, method).membership)
                    sz = sorted(np.bincount(m0).tolist(), reverse=True)
                    f1, pr, rc = gold_score(m0, gold)
                    print("%-10s %7.3g %7.3g %7d %7d %7.3f %8.3f %7.3f" % (method, gamma, shrink, sum(1 for z in sz if z >= 5),
                                                                       sz[0], f1, pr, rc), flush=True)
        return

    if args.sweep:
        print("%8s %7s %10s %10s %8s  (areas of 5+ lineages; ARI = agreement between 3 seeds, 1.0 = identical)" % (
            "gamma", "areas", "largest", "2nd", "ARI"))
        for gamma in [float(x) for x in args.sweep.split(",")]:
            parts = [run(gamma, sd) for sd in (0, 1, 2)]
            m0 = np.array(parts[0].membership)
            sz = sorted(np.bincount(m0).tolist(), reverse=True)
            ari = np.mean([ig.compare_communities(parts[i], parts[j], method="ari") for i, j in ((0, 1), (0, 2), (1, 2))])
            print("%8.3g %7d %10d %10d %8.3f" % (gamma, sum(1 for z in sz if z >= 5), sz[0], sz[1] if len(sz) > 1 else 0, ari),
                  flush=True)
        print("pick a gamma where ARI is high and the area count is steady across neighbours, then:\n"
              "  ./cocite.py areas --courts ... --method %s --resolution <gamma>" % args.method)
        return
    if args.consensus > 1:
        # consensus clustering (Lancichinetti & Fortunato 2012): run many seeds, weight each tie by the
        # share of runs that put its two ends together, re-cluster that graph until the runs agree
        cg_w = None
        ea_c, eb_c = ka.copy(), kb.copy()
        for it in range(args.consensus_rounds):
            if cg_w is None:
                parts = [np.array(run(args.resolution, args.seed + r).membership) for r in range(args.consensus)]
            else:
                gc = ig.Graph(n=len(nodes), edges=np.column_stack([ea_c, eb_c]).tolist(), directed=False)
                gc.es["w"] = cg_w.tolist()
                parts = []
                for r in range(args.consensus):
                    random.seed(args.seed + 100 * (it + 1) + r)
                    parts.append(np.array(gc.community_leiden(objective_function="modularity", weights="w",
                                                              n_iterations=-1).membership))
            same = np.mean([pm[ea_c] == pm[eb_c] for pm in parts], axis=0)
            undecided = float(np.mean((same > 0) & (same < 1)))
            print("  consensus round %d: %d runs, %.1f%% of ties undecided" % (it + 1, len(parts), 100 * undecided), flush=True)
            keep = same >= args.consensus_tau
            ea_c, eb_c, cg_w = ea_c[keep], eb_c[keep], same[keep]
            if undecided < 0.01:
                break
        memb = parts[0]
        # confidence: the share of a lineage's ties (by weight) that runs keep inside its area
        conf_num = np.zeros(len(nodes)); conf_den = np.zeros(len(nodes))
        allsame = np.mean([pm[ka] == pm[kb] for pm in parts], axis=0) if cg_w is not None else None
        inside = memb[ka] == memb[kb]
        for arr in (ka, kb):
            np.add.at(conf_num, arr, kw * inside)
            np.add.at(conf_den, arr, kw)
        confidence = conf_num / np.maximum(conf_den, 1e-9)
    else:
        part = run(args.resolution, args.seed)
        memb = np.array(part.membership)
        confidence = None
    strength = raw_strength
    tot = np.bincount(memb, weights=strength)
    order_a = np.argsort(-tot)
    top = np.full(len(tot), 0, np.int64)
    top[order_a] = np.arange(1, len(tot) + 1)
    code = top[memb] * 1000                            # area 3 -> 3000; its split parts -> 3001, 3002 ...
    # split the big areas, keeping a split only if it beats the same graph rewired at random
    p_split = dict(DEFAULTS, resolution=args.resolution, seed=args.seed, sub_z=args.sub_z, nulls=args.nulls)
    split = 0
    for ar in range(1, len(tot) + 1):
        mem = np.flatnonzero(top[memb] == ar)
        if args.method == "cpm" or len(mem) <= args.split_over:
            continue
        loc = np.full(len(nodes), -1, np.int64)
        loc[mem] = np.arange(len(mem))
        inside = (loc[ka] >= 0) & (loc[kb] >= 0) & (ka != kb)
        la, lb, lw = loc[ka[inside]], loc[kb[inside]], kw[inside]
        sm, sq = leiden(len(mem), la, lb, lw, p_split)
        m0, sd0 = null_q(len(mem), la, lb, lw, p_split, args.nulls)
        verdict = sq > m0 and (sq - m0) >= args.sub_z * max(sd0, 1e-4)
        print("  split test, area %d (%d lineages): modularity %.3f vs %.3f +/- %.3f rewired -> %s" % (
            ar, len(mem), sq, m0, sd0, "split" if verdict else "kept whole"), flush=True)
        if not verdict:
            continue
        sz = np.bincount(sm, weights=strength[mem])
        rk = np.zeros(len(sz), np.int64)
        rk[np.argsort(-sz)] = np.arange(1, len(sz) + 1)
        code[mem] = ar * 1000 + np.minimum(rk[sm], 999)
        split += 1
    area_of_node = {nodes[i]: int(code[i]) for i in range(len(nodes))}
    for sm_l, ties in small_ties.items():               # small lineages join the area they are most tied to
        bgl = max(ties.items(), key=lambda kv: kv[1])[0]
        area_of_node.setdefault(sm_l, area_of_node[bgl])
    weight_of = {nodes[i]: float(strength[i]) for i in range(len(nodes))}
    conf_of = {nodes[i]: float(confidence[i]) for i in range(len(nodes))} if confidence is not None else {}
    for sm_l, ties in small_ties.items():
        weight_of.setdefault(sm_l, sum(ties.values()))
    spans = {l: (f, t) for l, f, t in db.execute("SELECT lineage, first_slice, last_slice FROM lineages")}
    db.execute("BEGIN")
    for t in ("areas", "lineage_area", "area_fp"):
        db.execute("DELETE FROM %s" % t)
    rows = [(l, ar, weight_of.get(l, 0.0)) for l, ar in area_of_node.items()]
    db.executemany("INSERT INTO lineage_area VALUES (?,?,?)", rows)
    db.execute("CREATE TABLE IF NOT EXISTS lineage_area_conf(lineage INTEGER PRIMARY KEY, confidence REAL)")
    db.execute("DELETE FROM lineage_area_conf")
    db.executemany("INSERT INTO lineage_area_conf VALUES (?,?)", list(conf_of.items()))
    meta(db, "areas_params", json.dumps({"method": args.method, "resolution": args.resolution,
                                         "consensus": args.consensus, "min_size": args.min_size,
                                         "shrink": args.shrink}))
    by_area = {}
    for l, ar, st in rows:
        by_area.setdefault(ar, []).append((st, l))
    case_w = {}
    for (sl, ref), (lin, sw) in lin_of.items():
        ar = area_of_node.get(lin)
        if ar:
            case_w.setdefault(ar, Counter())[ref] += sw
    for ar, members in by_area.items():
        members.sort(reverse=True)
        fs = [spans[l][0] for _, l in members if l in spans]
        ls = [spans[l][1] for _, l in members if l in spans]
        db.execute("INSERT INTO areas VALUES (?,?,?,?,?,?,datetime('now'))",
                   (ar, len(members), sum(x for x, _ in members), members[0][1], min(fs) if fs else None, max(ls) if ls else None))
        fp = case_w.get(ar, Counter()).most_common(FINGER)
        db.executemany("INSERT INTO area_fp VALUES (?,?,?,?)", [(ar, k, r, wv) for k, (r, wv) in enumerate(fp, 1)])
    db.commit()
    q = g.modularity(memb.tolist(), weights="w")
    sizes = sorted((len(v) for v in by_area.values()), reverse=True)
    print("areas: %d (%d large areas split) from %s lineages of %d+ cases (+%s small ones attached), modularity %.3f; "
          "largest hold %s lineages  (%.1f min)" % (len(by_area), split, format(len(nodes), ","), args.min_size,
          format(len(small_ties), ","), q, ", ".join(map(str, sizes[:10])), (time.time() - t0) / 60))
    big_n = np.bincount(memb)
    print("coverage: %.1f%% of node lineages sit in areas of 5+" % (100 * float(np.mean(big_n[memb] >= 5))))
    if Path(args.gold).exists() and LABELS.exists():
        gold = load_gold(db, args.gold, idx)
        if gold:
            f1, pr, rc = gold_score(memb, gold)
            print("benchmark (this partition): F1 %.3f, precision %.3f, recall %.3f" % (f1, pr, rc))
    if LABELS.exists():
        match_labels(db)
    show_areas(db, args.top)


def load_gold(db, path, idx):
    """Benchmark groupings, written by lineage label name (names survive rebuilds; numbers don't):
    {"group": ["label name", ...]} -> {node index: group}."""
    try:
        groups = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit("no benchmark file %s" % path)
    # areas are built over sub-level lineages, so a name used at both levels resolves to the sub label
    key_of = {lab["name"]: lab["key"] for lab in sorted(load_labels(), key=lambda l: l.get("level") == "sub")}
    lin_of = {k: l for l, k in db.execute("SELECT lineage, key FROM label_map")}
    out, missing = {}, []
    for grp, names in groups.items():
        for nm in names:
            lin = lin_of.get(key_of.get(nm))
            if lin in idx:
                out[idx[lin]] = grp
            else:
                missing.append(nm)
    if missing:
        print("benchmark: %d names not found or not a node here: %s" % (len(missing), "; ".join(missing[:6])))
    return out


def gold_score(memb, gold):
    """Pairwise F1 of a partition against the benchmark: precision = share of same-area benchmark pairs
    that the benchmark puts together; recall = share of benchmark-together pairs placed in one area."""
    items = list(gold.items())
    tp = fp = fn = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            same_a = memb[items[i][0]] == memb[items[j][0]]
            same_g = items[i][1] == items[j][1]
            tp += same_a and same_g
            fp += same_a and not same_g
            fn += same_g and not same_a
    pr = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    return (2 * pr * rc / (pr + rc) if pr + rc else 0.0), pr, rc


def area_of(db, lineage):
    r = db.execute("SELECT area FROM lineage_area WHERE lineage=?", (lineage,)).fetchone() if lineage else None
    return r[0] if r else None


def area_name(db, area):
    if not area:
        return None
    try:
        r = db.execute("SELECT key FROM area_label WHERE area=?", (area,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r:
        lab = {x["key"]: x for x in load_labels()}.get(r[0])
        if lab:
            return lab["name"]
    a = db.execute("SELECT anchor_lineage FROM areas WHERE area=?", (area,)).fetchone()
    lab = label_of(db, a[0]) if a else None
    return "area %s (around %s)" % (sub_label(area), lab[0] if lab else "lineage %s" % (a[0] if a else "?"))


def show_areas(db, top=40):
    try:
        low = db.execute("SELECT count(*) FROM lineage_area_conf WHERE confidence < 0.5").fetchone()[0]
        tot = db.execute("SELECT count(*) FROM lineage_area_conf").fetchone()[0]
        if tot:
            print("boundary lineages (under half their ties inside their area): %d of %d" % (low, tot))
    except sqlite3.OperationalError:
        pass
    for ar, n, wt, anchor, f, t in db.execute("SELECT area, lineages, weight, anchor_lineage, first_slice, last_slice "
                                             "FROM areas ORDER BY weight DESC LIMIT ?", (top,)).fetchall():
        heads = []
        for (l,) in db.execute("SELECT lineage FROM lineage_area WHERE area=? ORDER BY weight DESC LIMIT 4", (ar,)):
            lab = label_of(db, l)
            heads.append(lab[0] if lab else "lineage %d" % l)
        print("%5s  %-55s %4d lineages  %s to %s\n       %s" % (sub_label(ar), (area_name(db, ar) or "")[:55], n,
                                                           label(f) if f else "?", label(t) if t else "?", " | ".join(heads)))


def cmd_area_evidence(args):
    db = need_db()
    out = []
    for ar, n, wt, anchor, f, t in db.execute("SELECT area, lineages, weight, anchor_lineage, first_slice, last_slice "
                                             "FROM areas ORDER BY weight DESC LIMIT ?", (args.top,)).fetchall():
        mem = []
        for l, w in db.execute("SELECT lineage, weight FROM lineage_area WHERE area=? ORDER BY weight DESC LIMIT 30", (ar,)):
            lab = label_of(db, l)
            sp = db.execute("SELECT first_slice, last_slice FROM lineages WHERE lineage=?", (l,)).fetchone()
            mem.append({"lineage": l, "label": lab[0] if lab else None,
                        "span": "%s to %s" % (label(sp[0]), label(sp[1])) if sp else None, "weight": round(w)})
        fp = [r for (r,) in db.execute("SELECT ref FROM area_fp WHERE area=? ORDER BY rank", (ar,))]
        out.append({"level": "area", "area": ar, "lineages": n, "span": "%s to %s" % (label(f), label(t)) if f else None,
                    "members": mem, "top_cases": [name(db, r) for r in fp[:20]], "fingerprint": fp,
                    "view": COCITE_DB.stem})
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / ("areas-%s.jsonl" % COCITE_DB.stem)
    with path.open("w", encoding="utf-8") as fh:
        for rec in out:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("wrote %s (%d areas)" % (path, len(out)))


# ================================================================ labels: what a lineage is about
# Evidence is computed here, by script, from the clustering and from the parentheticals other
# courts wrote about each member ("holding that ..."). The one-line name and the description are
# written from that evidence by a language model, outside this script, and kept in
# cocite_labels.json next to it (tracked in git: unlike everything else, it can't be rebuilt).
# Lineage numbers change with every build, so a label is tied to a fingerprint (the lineage's 30
# heaviest members) and re-attached after a build to whichever lineage overlaps it best.

HERE = Path(__file__).resolve().parent
LABELS = HERE / "cocite_labels.json"
EVIDENCE_DIR = HERE / "build" / "cocite"
FINGER = 30
STABLE = 0.7          # a label still matching its lineage at this Jaccard or better counts as unchanged
STOP = set("""a an the and or of to in on at by for with from into onto over under upon about as is are was were be been
being it its this that these those there their they them he she his her we our you your not no nor but if then than
so such any all each every other another same which who whom whose what when where whether while because although
though since until unless also only even just more most less least very much many few can could may might must shall
should will would do does did done has have had having held holding holds hold noting noted notes stating states
stated explaining explained explains finding found finds concluding concluded concludes observing observed
recognizing recognized describing described discussing discussed reasoning reaffirming reiterating rejecting rejected
citing quoting quoted see cf also court courts case cases rule rules law laws legal federal state states plaintiff
plaintiffs defendant defendants party parties claim claims action actions issue issues question questions require
requires required requiring under within without between against after before during one two three first second
new same id supra infra et al inc co corp v vs u s us app supp ed cir dist""".split())


def tokens(text):
    words = [w for w in re.findall(r"[a-z][a-z'\-]+|\d+\([a-z0-9]+\)(?:\([a-z0-9]+\))*|§+\s*\d+[a-z0-9()]*", text.lower())
             if w not in STOP and len(w) > 2]
    return words + [a + " " + b for a, b in zip(words, words[1:])]


def lineage_members(db, lin, level, k=FINGER):
    if level == "sub":
        sql = ("SELECT n.ref, sum(n.subw) w FROM subs s JOIN nodes n ON n.slice = s.slice AND n.sub = s.sub "
               "WHERE s.lineage = ? GROUP BY n.ref ORDER BY w DESC LIMIT ?")
    else:
        sql = ("SELECT m.ref, sum(m.weight) w FROM clusters c JOIN members m ON m.slice = c.slice AND m.cl = c.cl "
               "AND m.home = 1 WHERE c.lineage = ? GROUP BY m.ref ORDER BY w DESC LIMIT ?")
    return [r for r, _ in db.execute(sql, (lin, k))]


def load_labels():
    try:
        return json.loads(LABELS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def lineage_fingerprints(db, only=None):
    """lineage -> its FINGER heaviest members, for every lineage (or those in `only`), in one pass
    over nodes and members rather than one query per lineage."""
    unit = {}
    for s, k, lin in db.execute("SELECT slice, sub, lineage FROM subs"):
        unit[("s", s, k)] = lin
    for s, k, lin in db.execute("SELECT slice, cl, lineage FROM clusters"):
        unit[("c", s, k)] = lin
    w = {}
    for s, k, ref, sw in db.execute("SELECT slice, sub, ref, subw FROM nodes WHERE sub > 0"):
        lin = unit.get(("s", s, k))
        if lin and (only is None or lin in only):
            d = w.setdefault(lin, {})
            d[ref] = d.get(ref, 0.0) + (sw or 0.0)
    for s, k, ref, mw in db.execute("SELECT slice, cl, ref, weight FROM members WHERE home = 1"):
        lin = unit.get(("c", s, k))
        if lin and (only is None or lin in only):
            d = w.setdefault(lin, {})
            d[ref] = d.get(ref, 0.0) + (mw or 0.0)
    return {lin: [r for r, _ in sorted(d.items(), key=lambda x: -x[1])[:FINGER]] for lin, d in w.items()}


def match_labels(db, quiet=False):
    """label_map: lineage -> label key, by member overlap with each label's fingerprint."""
    t0 = time.time()
    labels = load_labels()
    db.execute("CREATE TABLE IF NOT EXISTS label_map(lineage INTEGER PRIMARY KEY, key TEXT, jaccard REAL)")
    db.execute("DELETE FROM label_map")
    if not labels:
        db.commit()
        return 0
    refs = sorted({r for lab in labels for r in lab["fingerprint"]})
    cand = set()                                   # lineages holding any fingerprint case at all
    for i in range(0, len(refs), 500):
        q = ",".join("?" * len(refs[i:i + 500]))
        for (lin,) in db.execute("SELECT DISTINCT s.lineage FROM nodes n JOIN subs s ON s.slice = n.slice AND s.sub = n.sub "
                                 "WHERE n.ref IN (%s)" % q, refs[i:i + 500]):
            cand.add(lin)
        for (lin,) in db.execute("SELECT DISTINCT c.lineage FROM nodes n JOIN clusters c ON c.slice = n.slice AND c.cl = n.cl "
                                 "WHERE n.ref IN (%s)" % q, refs[i:i + 500]):
            cand.add(lin)
    level_of = dict(db.execute("SELECT lineage, level FROM lineages"))
    fps = lineage_fingerprints(db, cand)
    by_ref = {}
    for i, lab in enumerate(labels):
        for r in lab["fingerprint"]:
            by_ref.setdefault(r, []).append(i)
    best, best_any = {}, {}
    for lin, mem in fps.items():
        hits = {}
        for r in mem:
            for i in by_ref.get(r, ()):
                if labels[i]["level"] == level_of.get(lin):   # area labels never match a lineage
                    hits[i] = hits.get(i, 0) + 1
        for i, m in hits.items():
            j = m / (len(mem) + len(labels[i]["fingerprint"]) - m)
            if j > best_any.get(i, (None, 0))[1]:
                best_any[i] = (lin, j)
            if j >= 0.3 and j > best.get(i, (None, 0))[1]:
                best[i] = (lin, j)
    rows = {}
    for i, (lin, j) in best.items():                 # one label per lineage: the closer one wins
        if lin not in rows or j > rows[lin][1]:
            rows[lin] = (labels[i]["key"], j)
    db.executemany("INSERT INTO label_map VALUES (?,?,?)", [(l, k, j) for l, (k, j) in rows.items()])
    # status of every label after this build: stable, drifted (attached, but its cluster changed),
    # displaced (a closer label took its lineage) or orphaned (nothing overlaps it enough)
    db.execute("CREATE TABLE IF NOT EXISTS label_status(key TEXT PRIMARY KEY, level TEXT, lineage INTEGER, "
               "jaccard REAL, status TEXT)")
    db.execute("DELETE FROM label_status")
    attached = {k: (l, j) for l, (k, j) in rows.items()}
    st = []
    for i, lab in enumerate(labels):
        if lab["level"] == "area":
            continue
        if lab["key"] in attached:
            l, j = attached[lab["key"]]
            st.append((lab["key"], lab["level"], l, j, "stable" if j >= STABLE else "drifted"))
        elif i in best:
            st.append((lab["key"], lab["level"], best[i][0], best[i][1], "displaced"))
        else:
            l, j = best_any.get(i, (None, 0.0))
            st.append((lab["key"], lab["level"], l, j, "orphaned"))
    db.executemany("INSERT INTO label_status VALUES (?,?,?,?,?)", st)
    db.commit()
    alabs = [lab for lab in labels if lab["level"] == "area"]
    n_area = 0
    if alabs and db.execute("SELECT 1 FROM sqlite_master WHERE name='area_fp'").fetchone():
        afp = {}
        for ar, ref in db.execute("SELECT area, ref FROM area_fp"):
            afp.setdefault(ar, set()).add(ref)
        db.execute("DELETE FROM area_label")
        best_a = {}
        for lab in alabs:
            f = set(lab["fingerprint"])
            for ar, mem in afp.items():
                j = len(f & mem) / max(len(f | mem), 1)
                if j >= 0.3 and j > best_a.get(ar, (None, 0))[1]:
                    best_a[ar] = (lab["key"], j)
        db.executemany("INSERT INTO area_label VALUES (?,?,?)", [(ar, k, j) for ar, (k, j) in best_a.items()])
        db.commit()
        n_area = len(best_a)
    if not quiet:
        print("labels: %d of %d attached to lineages%s in %s  (%.1f min)" % (
            len(rows), len(labels) - len(alabs), ", %d of %d to areas" % (n_area, len(alabs)) if alabs else "",
            COCITE_DB.name, (time.time() - t0) / 60))
    return len(rows)


def label_of(db, lin):
    """(name, description) for a lineage, or None."""
    if not lin:
        return None
    try:
        r = db.execute("SELECT key FROM label_map WHERE lineage=?", (lin,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not r:
        return None
    global _LABELS
    try:
        _LABELS
    except NameError:
        _LABELS = {lab["key"]: lab for lab in load_labels()}
    lab = _LABELS.get(r[0])
    return (lab["name"], lab["description"]) if lab else None


def family_parents(db):
    """sub-cluster lineage -> the cluster lineage it sits under in most of its slices
    (a cluster too small to split is its own single sub-cluster and is left out)."""
    votes = {}
    for sl, pl, n in db.execute("SELECT s.lineage, c.lineage, count(*) FROM subs s JOIN clusters c "
                                "ON c.slice = s.slice AND c.cl = s.sub / 1000 WHERE s.sub % 1000 > 0 GROUP BY 1, 2"):
        if n > votes.get(sl, (None, 0))[1]:
            votes[sl] = (pl, n)
    return {sl: pl for sl, (pl, n) in votes.items()}


def family_of(db, ref, year=None):
    """The cluster lineage a case sits in, in its newest complete slice."""
    year = year or time.localtime().tm_year
    rows = db.execute("SELECT n.slice, c.lineage FROM nodes n JOIN clusters c ON c.slice = n.slice AND c.cl = n.cl "
                      "WHERE n.ref = ? AND n.cl > 0 ORDER BY n.slice", (ref,)).fetchall()
    full = [r for r in rows if r[0] + WIDTH - 1 < year] or rows
    if not full:
        sys.exit("that case is in no cluster in %s" % COCITE_DB.name)
    return full[-1][1]


def cmd_evidence(args):
    """One JSON line per lineage worth describing: timeline, top members, parentheticals, terms."""
    db = need_db()
    con = main_db()
    if casedb.OPS_DB.exists():
        con.execute("ATTACH DATABASE ? AS ops", ("file:%s?mode=ro" % casedb.OPS_DB,))
    have = {lab["key"] for lab in load_labels()}
    labelled = {r[0] for r in db.execute("SELECT lineage FROM label_map")} if \
        db.execute("SELECT 1 FROM sqlite_master WHERE name='label_map'").fetchone() else set()
    picks = []
    family = None
    if args.stale:
        stale = [r for r in db.execute("SELECT lineage, level FROM label_status WHERE status IN ('drifted', 'displaced') "
                                       "AND lineage IS NOT NULL")]
        picks = [(lv, l) for l, lv in stale]
        print("evidence for %d lineages whose labels drifted or were displaced" % len(picks))
    if args.family_of or args.family:
        family = args.family or family_of(db, resolve(args.family_of))
        parent = family_parents(db)
        sizes = dict(db.execute("SELECT lineage, max(size) FROM subs GROUP BY lineage"))
        sibs = sorted((l for l, p in parent.items() if p == family and sizes.get(l, 0) >= args.min_family_size),
                      key=lambda l: -sizes[l])
        picks = [("sub", l) for l in sibs[:args.top]]
        fam_name = label_of(db, family)
        print("family: cluster lineage %d (%s), %d sub-cluster lineages of %d+ cases" % (
            family, fam_name[0] if fam_name else "unlabelled", len(picks), args.min_family_size))
    for level, table, n in (() if (family or args.stale) else (("cluster", "clusters", args.top_clusters), ("sub", "subs", args.top))):
        rows = db.execute("SELECT l.lineage, sum(t.size) cs, count(*) ns FROM lineages l JOIN %s t ON t.lineage = l.lineage "
                          "WHERE l.level = ? GROUP BY l.lineage HAVING ns >= ? OR max(t.size) >= ? ORDER BY cs DESC"
                          % table, (level, args.min_slices, args.min_size)).fetchall()
        rows = [r for r in rows if args.redo or r[0] not in labelled]
        picks += [(level, r[0]) for r in rows[:n]]
    if not family and not args.stale:
        print("evidence for %d lineages (%d already labelled are skipped; --redo to include them)" % (len(picks), len(labelled)))
    out, docs = [], []
    for k, (level, lin) in enumerate(picks, 1):
        table, col = ("subs", "sub") if level == "sub" else ("clusters", "cl")
        tl = db.execute("SELECT slice, %s, size, anchor FROM %s WHERE lineage=? ORDER BY slice" % (col, table), (lin,)).fetchall()
        mem = lineage_members(db, lin, level)
        ops = {}
        for i in range(0, len(mem), 500):
            q = ",".join("?" * len(mem[i:i + 500]))
            for oid, cid in con.execute("SELECT id, cluster_id FROM ops.opinions WHERE cluster_id IN (%s)" % q, mem[i:i + 500]):
                ops.setdefault(cid, []).append(oid)
        parens, words = [], []
        for ref in mem[:20]:
            ids = ops.get(ref, [])
            if not ids:
                continue
            q = ",".join("?" * len(ids))
            got = con.execute("SELECT text, score FROM parens WHERE described IN (%s) AND text IS NOT NULL "
                              "ORDER BY score DESC LIMIT 150" % q, ids).fetchall()
            for t, sc in got:
                words += tokens(t)
            parens += [(sc, short(db, ref), t) for t, sc in got[:3]]
        parens.sort(key=lambda x: -(x[0] or 0))
        docs.append(Counter(words))
        out.append({"level": level, "lineage": lin, "view": COCITE_DB.stem,
                    "timeline": [{"slice": label(s), "unit": "%s:%s" % (label(s), sub_label(u) if level == "sub" else u),
                                  "size": z, "anchor": short(db, a)} for s, u, z, a in tl],
                    "members": [name(db, r) for r in mem[:15]], "fingerprint": mem,
                    "parentheticals": ["%s: %s" % (nm, t[:220]) for _, nm, t in parens[:8]]})
        if k % 25 == 0:
            print("  %d of %d" % (k, len(picks)), flush=True)
    # distinctive terms: in family mode the comparison set is the siblings, so these are what
    # sets each sub-cluster apart from the others under the same parent
    df = Counter(w for d in docs for w in d)
    Nd = max(len(docs), 1)
    for rec, d in zip(out, docs):
        score = {w: c * math.log((1 + Nd) / (1 + df[w])) for w, c in d.items() if c >= 3}
        rec["terms"] = [w for w, _ in sorted(score.items(), key=lambda x: -x[1])[:15]]
        lab = label_of(db, rec["lineage"])
        rec["current_label"] = lab[0] if lab else None
    if family:
        fps = {rec["lineage"]: set(rec["fingerprint"]) for rec in out}
        for rec in out:
            mine = fps[rec["lineage"]]
            others = set().union(*(f for l, f in fps.items() if l != rec["lineage"])) if len(fps) > 1 else set()
            rec["distinct_members"] = [name(db, r) for r in rec["fingerprint"] if r not in others][:8]
            near = sorted(((len(mine & f), l) for l, f in fps.items() if l != rec["lineage"]), reverse=True)[:3]
            rec["nearest_siblings"] = [{"lineage": l, "shared_members": m,
                                        "label": (label_of(db, l) or (None,))[0]} for m, l in near if m]
            rec["family"] = family
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / ("evidence-%s%s.jsonl" % (COCITE_DB.stem, "-family-%d" % family if family else ""))
    with path.open("w", encoding="utf-8") as f:
        for rec in out:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("wrote %s (%d lineages)" % (path, len(out)))


def cmd_labels(args):
    if args.action == "import":
        labels = load_labels()
        by_key = {lab["key"]: lab for lab in labels}
        n_new = 0
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            for f in ("level", "fingerprint", "name", "description", "written_by"):
                if f not in rec:
                    sys.exit("label without %r: %s" % (f, line[:120]))
            rec["key"] = rec.get("key") or "%s-%s" % (rec["level"], hashlib.sha1(
                ",".join(map(str, sorted(rec["fingerprint"]))).encode()).hexdigest()[:10])
            n_new += rec["key"] not in by_key
            by_key[rec["key"]] = rec
        tmp = LABELS.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(by_key.values(), key=lambda r: r["key"]), indent=1, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(LABELS)
        print("labels: %d new, %d in %s" % (n_new, len(by_key), LABELS.name))
        match_labels(need_db())
    elif args.action == "match":
        match_labels(need_db())
    elif args.action == "check":
        db = need_db()
        match_labels(db, quiet=True)
        labs = {lab["key"]: lab for lab in load_labels()}
        rows = db.execute("SELECT key, level, lineage, jaccard, status FROM label_status ORDER BY status, jaccard").fetchall()
        c = Counter(r[4] for r in rows)
        print("labels: %d stable (overlap >= %.1f), %d drifted, %d displaced, %d orphaned" % (
            c["stable"], STABLE, c["drifted"], c["displaced"], c["orphaned"]))
        for key, level, lin, j, status in rows:
            if status != "stable":
                print("  %-9s %-7s overlap %.2f  lineage %-6s %s" % (status, level, j or 0, lin or "-", labs[key]["name"][:60]))
        big = db.execute("SELECT count(*) FROM lineages l JOIN subs s ON s.lineage = l.lineage WHERE l.level='sub' "
                         "AND l.lineage NOT IN (SELECT lineage FROM label_map) AND s.size >= 100").fetchone()[0]
        print("unlabelled sub-cluster slices of 100+ cases: %d (./cocite.py evidence picks the largest)" % big)
        print("drifted and displaced labels are re-described with: ./cocite.py evidence --stale")
    else:
        db = need_db()
        labs = {lab["key"]: lab for lab in load_labels()}
        try:
            rows = db.execute("SELECT m.lineage, m.key, m.jaccard, l.level, l.first_slice, l.last_slice FROM label_map m "
                              "JOIN lineages l USING(lineage) ORDER BY l.level, l.first_slice").fetchall()
        except sqlite3.OperationalError:
            rows = []
        for lin, key, j, level, a, b in rows:
            print("%-7s %6d  %s to %s  %-45s (match %.2f)" % (level, lin, label(a), label(b), labs[key]["name"][:45], j))
        print("%d labels, %d attached here" % (len(labs), len(rows)))


def cmd_shift(args):
    """Lives in cocite_shift.py, which imports this module as 'cocite'; run as a script, this one is
    __main__, so hand over the results file chosen from --courts."""
    import cocite_shift
    cocite_shift.cc.COCITE_DB = COCITE_DB
    cocite_shift.cmd_shift(args)


def cmd_prox_evidence(args):
    """Lives in cocite_positions.py; hand it the results file chosen from --courts (see cmd_shift)."""
    import cocite_positions, cocite as cc
    cc.COCITE_DB = COCITE_DB
    cocite_positions.cmd_prox_evidence(args)


def main():
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)     # `| head` ends quietly instead of with a traceback
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    view = argparse.ArgumentParser(add_help=False)
    view.add_argument("--courts", help="only citing decisions from these court levels, e.g. appellate "
                                       "(default: all; each view has its own results file)")
    view.add_argument("--mix", type=float, help="with --weight: keep the binary pairs (same nodes and edges) and weight "
                                                "them mix x support + (1 - mix) x proximity weight, e.g. 0.2 "
                                                "(its own results file)")
    view.add_argument("--weight", help="pair weights: binary (default: decisions citing both), or from citation "
                                       "positions: decision, paragraph, sentence, string, kernel:<chars> "
                                       "(each has its own results file; needs ./cocite.py positions --index)")
    p = sub.add_parser("prep", help="read the database once into cocite-cache/")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_prep)
    p = sub.add_parser("build", parents=[view], help="cluster the slices, then link them into lineages")
    p.add_argument("--slices", help="comma list of start years, e.g. 2005,2010 (default: all)")
    p.add_argument("--force", action="store_true", help="rebuild slices that are current")
    p.add_argument("--min-cited", dest="min_cited", type=int, default=DEFAULTS["min_cited"],
                   help="floor: a node needs at least this many citing decisions (default %(default)s)")
    p.add_argument("--min-cited-frac", dest="min_cited_frac", type=float, default=DEFAULTS["min_cited_frac"],
                   help="share of the slice's citing decisions a node needs, if above the floor (default %(default)s)")
    p.add_argument("--min-support", dest="min_support", type=int, default=DEFAULTS["min_support"],
                   help="a pair needs this many decisions citing both (default %(default)s)")
    p.add_argument("--max-cites", dest="max_cites", type=int, default=DEFAULTS["max_cites"],
                   help="leave out decisions citing more nodes than this (default %(default)s)")
    p.add_argument("--resolution", type=float, default=DEFAULTS["resolution"],
                   help="Leiden resolution: higher = more, smaller clusters (default %(default)s)")
    p.add_argument("--share", type=float, default=DEFAULTS["share"],
                   help="list a case in a non-home cluster holding this share of its weight (default %(default)s)")
    p.add_argument("--top", type=int, default=DEFAULTS["top"], help="partners kept per case per slice (default %(default)s)")
    p.add_argument("--min-size", dest="min_size", type=int, default=DEFAULTS["min_size"],
                   help="smaller clusters are dropped (default %(default)s)")
    p.add_argument("--seed", type=int, default=DEFAULTS["seed"])
    p.add_argument("--split-over", dest="split_over", type=int, default=DEFAULTS["split_over"],
                   help="try to split every cluster at least this big (default %(default)s)")
    p.add_argument("--sub-z", dest="sub_z", type=float, default=DEFAULTS["sub_z"],
                   help="a split must beat rewired copies by this many standard deviations (default %(default)s)")
    p.add_argument("--nulls", type=int, default=DEFAULTS["nulls"],
                   help="rewired copies per split test (default %(default)s)")
    p.add_argument("--carry", type=float, default=0.2,
                   help="Jaccard a mutual best match needs to continue a lineage (default %(default)s)")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("status", parents=[view])
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("diag", help="citing decisions per slice by court level")
    p.set_defaults(func=cmd_diag)
    p = sub.add_parser("case", parents=[view], help="one case's clusters and partners, slice by slice")
    p.add_argument("--cite")
    p.add_argument("--cluster", type=int, help="CourtListener cluster id instead of --cite")
    p.add_argument("--slice", type=int, help="only the slice holding this year")
    p.add_argument("--top", type=int, default=8)
    p.set_defaults(func=cmd_case)
    p = sub.add_parser("cluster", parents=[view], help="one co-citation cluster: members, before and after")
    p.add_argument("which", help="slice:number or slice:number.sub, e.g. 2005-09:3 or 2010-14:1.3")
    p.add_argument("--top", type=int, default=40)
    p.set_defaults(func=cmd_cluster)
    p = sub.add_parser("evidence", parents=[view], help="evidence file for describing the biggest unlabelled lineages")
    p.add_argument("--top", type=int, default=260, help="sub-cluster lineages (default %(default)s)")
    p.add_argument("--top-clusters", dest="top_clusters", type=int, default=40, help="cluster lineages (default %(default)s)")
    p.add_argument("--min-slices", dest="min_slices", type=int, default=2, help="lineage must span this many slices ...")
    p.add_argument("--min-size", dest="min_size", type=int, default=100, help="... or reach this size once (default %(default)s)")
    p.add_argument("--redo", action="store_true", help="include lineages that already have a label")
    p.add_argument("--family-of", dest="family_of", help="every sub-cluster lineage under this case's cluster, "
                   "with terms that set each apart from its siblings (labelled ones included)")
    p.add_argument("--family", type=int, help="the same, for a cluster lineage number")
    p.add_argument("--stale", action="store_true", help="lineages whose labels drifted or were displaced (see labels check)")
    p.add_argument("--min-family-size", dest="min_family_size", type=int, default=30,
                   help="family mode: sub-cluster lineages reaching this size at least once (default %(default)s)")
    p.set_defaults(func=cmd_evidence)
    p = sub.add_parser("labels", parents=[view], help="import / match / list lineage descriptions")
    p.add_argument("action", choices=["import", "match", "list", "check"])
    p.add_argument("file", nargs="?", help="JSON lines to import")
    p.set_defaults(func=cmd_labels)
    p = sub.add_parser("note", parents=[view], help="preview the case-note section (default view: appellate)")
    p.add_argument("--cite")
    p.add_argument("--cluster", type=int)
    p.set_defaults(func=cmd_note)
    p = sub.add_parser("areas", parents=[view], help="group sub-cluster lineages into areas, pooled over all slices")
    p.add_argument("--resolution", type=float, default=1.0, help="higher = more, smaller areas (default %(default)s)")
    p.add_argument("--continuity", type=float, default=1.0,
                   help="weight of the tie between a lineage and its successor (default %(default)s)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--top", type=int, default=60, help="areas listed (default %(default)s)")
    p.add_argument("--min-size", dest="min_size", type=int, default=30,
                   help="lineages reaching this size in some slice are nodes; smaller ones join their closest area")
    p.add_argument("--method", choices=["modularity", "cpm"], default="modularity",
                   help="cpm = Constant Potts Model on association-strength weights (no resolution limit)")
    p.add_argument("--sweep", help="comma list of resolutions to compare (nothing is written), e.g. 0.5,1,2,4,8")
    p.add_argument("--shrink", type=float, default=0.0,
                   help="cpm: add shrink x median strength to each side of the association-strength division")
    p.add_argument("--grid", help='score many settings against the benchmark, nothing written: '
                   '"cpm:1,2,4@0,1,10;modularity:1,2,4,8"')
    p.add_argument("--gold", default=str(HERE / "cocite_gold.json"), help="benchmark groupings (default %(default)s)")
    p.add_argument("--consensus", type=int, default=1,
                   help="runs per consensus round; 20 gives one reproducible partition (default 1 = a single run)")
    p.add_argument("--consensus-rounds", dest="consensus_rounds", type=int, default=6)
    p.add_argument("--consensus-tau", dest="consensus_tau", type=float, default=0.5,
                   help="drop ties that fewer than this share of runs keep together (default %(default)s)")
    p.add_argument("--split-over", dest="split_over", type=int, default=150,
                   help="split areas holding more lineages than this (default %(default)s)")
    p.add_argument("--sub-z", dest="sub_z", type=float, default=4.0)
    p.add_argument("--nulls", type=int, default=5)
    p.set_defaults(func=cmd_areas)
    p = sub.add_parser("vault", parents=[view], help="write md-cocite/: area and lineage notes plus an index MOC")
    p.add_argument("--out", default=str(HERE / "md-cocite"), help="folder (default %(default)s)")
    p.add_argument("--min-size", dest="min_size", type=int, default=30,
                   help="lineages that reach this many cases in some slice get a note (default %(default)s)")
    p.add_argument("--core", type=int, default=12, help="core cases listed per note (default %(default)s)")
    p.add_argument("--link-min", dest="link_min", type=float, default=0.1,
                   help="overlap that counts as a merge or split (default %(default)s)")
    p.set_defaults(func=cmd_vault)
    p = sub.add_parser("area-evidence", parents=[view], help="evidence file for naming the areas")
    p.add_argument("--top", type=int, default=80)
    p.set_defaults(func=cmd_area_evidence)
    p = sub.add_parser("tree", parents=[view], help="one slice: clusters, largest first, each with its sub-clusters")
    p.add_argument("slice", type=int, help="any year in the slice, e.g. 2010")
    p.add_argument("--top", type=int, default=20, help="clusters shown (default %(default)s)")
    p.add_argument("--subs", type=int, default=10, help="sub-clusters shown per cluster (default %(default)s)")
    p.set_defaults(func=cmd_tree)
    p = sub.add_parser("lineage", parents=[view], help="one lineage across slices")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_lineage)
    p = sub.add_parser("shift", parents=[view], help="good-law signals from cluster change (cocite_shift.py)")
    p.add_argument("--cite")
    p.add_argument("--cluster", type=int)
    p.add_argument("--eval", action="store_true", help="score the signals on goodlaw_eval.py's cases")
    p.add_argument("--all", action="store_true", help="also list transitions where A is cited under 5 times")
    p.add_argument("--share", type=float, default=0.25, help="successor: share of A's citers that also cite B")
    p.add_argument("--growth", type=float, default=3.0, help="successor: B's citing growth over A's")
    p.add_argument("--cohort-pct", dest="cohort_pct", type=float, default=0.10,
                   help="collapse: fires when at most this share of the cohort fell further")
    p.set_defaults(func=cmd_shift)
    import cocite_positions
    cocite_positions.add_parser(sub)
    cocite_positions.add_evidence_parser(sub, view)
    p = sub.add_parser("together", parents=[view], help="do these cases share a cluster, slice by slice?")
    p.add_argument("--cite", action="append", default=[])
    p.add_argument("--cluster", type=int, action="append")
    p.set_defaults(func=cmd_together)
    args = ap.parse_args()
    global COCITE_DB
    tags = []
    if getattr(args, "courts", None):
        _codes, tag = parse_courts(args.courts)
        if tag != "all":
            tags.append(tag)
    if getattr(args, "weight", None):
        import cocite_positions
        args.weight = cocite_positions.parse_weight(args.weight)
        if args.weight:
            tags.append(args.weight)
            if getattr(args, "mix", None) is not None:
                if not 0 <= args.mix <= 1:
                    sys.exit("--mix must be between 0 and 1")
                tags.append("m%g" % args.mix)
    if tags:
        COCITE_DB = BASE_DB.with_name("cocite-%s.sqlite" % "-".join(tags))
    if args.cmd == "shift" and not (args.cite or args.cluster or args.eval):
        ap.error("shift needs --cite, --cluster or --eval")
    if args.cmd in ("case", "note") and not (args.cite or args.cluster):
        ap.error("case needs --cite or --cluster")
    args.func(args)


if __name__ == "__main__":
    main()
