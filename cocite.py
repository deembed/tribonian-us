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
    ./cocite.py lineage 17                           # one lineage, slice by slice
    ./cocite.py together --cite "550 U.S. 544" --cite "556 U.S. 662"
                                                     # falsification check: same cluster? pair support, lift
    ./cocite.py diag                                 # citing decisions per slice by court level
    ./cocite.py evidence --courts appellate          # build/cocite/evidence-*.jsonl: what to describe, and the evidence
    ./cocite.py labels import labels.jsonl           # descriptions -> cocite_labels.json, attached to lineages
    ./cocite.py labels list --courts appellate

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
        if not (CACHE / "level.npy").exists():
            prep_levels(con)                   # cache from before court levels existed: add them only
        else:
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

NOTE_VIEW = "appellate"          # the view case notes are drawn from


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


def case_md(ref, index=None, view=NOTE_VIEW, top=8, year=None):
    """Markdown lines for '## Co-citation' in a case note; [] when there is nothing to show."""
    db = open_view(view)
    if db is None:
        return []
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
    L = ["## Co-citation", "",
         "*Which cases later %s decisions cite together with this one, in five-year slices of citing decisions. "
         "Clusters from `cocite.py`; cluster names and descriptions were written by Claude from each cluster's "
         "evidence and are not part of the record.*" % ("appellate" if view == "appellate" else "court"), "",
         "**%s:** %s%s. Since %s." % (now["label"], head_name,
                                     " — within *%s*" % c_name if s_name else "",
                                     label(head_first) if head_first else now["label"]), ""]
    L += ["| Slice | Cited by | Cluster › sub-cluster | Share |", "|---|---:|---|---:|"]
    for r in rows:
        cn = unit_name(db, r["slice"], r["home"])[0]
        sc = r["sub"]["code"] if r["sub"] else None
        sn = unit_name(db, r["slice"], sc, "sub")[0] if sc and sc % 1000 else None
        home_share = next((c["share"] for c in r["clusters"] if c["home"]), 0)
        also = ["%s %d%%" % (unit_name(db, r["slice"], c["cl"])[0], round(100 * c["share"]))
                for c in r["clusters"] if not c["home"]]
        cell = cn + (" › " + sn if sn else "") + ("<br>*also:* " + "; ".join(also) if also else "")
        part = " (partial)" if r["slice"] + WIDTH - 1 >= year else ""
        L.append("| %s%s | %s | %s | %d%% |" % (r["label"], part, format(r["cited_by"], ","), cell.replace("|", "/"),
                                               round(100 * home_share)))
    L += ["", "**Usually cited with, %s** (decisions citing both, and their share of the %s decisions citing this case):"
          % (now["label"], format(now["cited_by"], ",")), ""]
    for pt in now["partners"]:
        e = index.get(str(pt["ref"]))
        text = "%s, %s (%s)" % (e["short"] or e["name"], e["cite"], e["year"]) if e else pt["name"]
        L.append("- %s · %s · %d%%" % (link(pt["ref"], text), format(pt["support"], ","),
                                      round(100.0 * pt["support"] / max(now["cited_by"], 1))))
    L.append("")
    lab = label_of(db, head_lin)
    if lab:
        L += ["> [!info]- %s (written by Claude from the cluster evidence; not part of the record)" % lab[0],
              "> " + lab[1], ""]
    return L


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
    best = {}
    for lin, mem in fps.items():
        hits = {}
        for r in mem:
            for i in by_ref.get(r, ()):
                if labels[i]["level"] == level_of.get(lin):
                    hits[i] = hits.get(i, 0) + 1
        for i, m in hits.items():
            j = m / (len(mem) + len(labels[i]["fingerprint"]) - m)
            if j >= 0.3 and j > best.get(i, (None, 0))[1]:
                best[i] = (lin, j)
    rows = {}
    for i, (lin, j) in best.items():                 # one label per lineage: the closer one wins
        if lin not in rows or j > rows[lin][1]:
            rows[lin] = (labels[i]["key"], j)
    db.executemany("INSERT INTO label_map VALUES (?,?,?)", [(l, k, j) for l, (k, j) in rows.items()])
    db.commit()
    if not quiet:
        print("labels: %d of %d attached to lineages in %s  (%.1f min)" % (len(rows), len(labels), COCITE_DB.name,
                                                                         (time.time() - t0) / 60))
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
    for level, table, n in (("cluster", "clusters", args.top_clusters), ("sub", "subs", args.top)):
        rows = db.execute("SELECT l.lineage, sum(t.size) cs, count(*) ns FROM lineages l JOIN %s t ON t.lineage = l.lineage "
                          "WHERE l.level = ? GROUP BY l.lineage HAVING ns >= ? OR max(t.size) >= ? ORDER BY cs DESC"
                          % table, (level, args.min_slices, args.min_size)).fetchall()
        rows = [r for r in rows if args.redo or r[0] not in labelled]
        picks += [(level, r[0]) for r in rows[:n]]
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
    df = Counter(w for d in docs for w in d)
    Nd = max(len(docs), 1)
    for rec, d in zip(out, docs):
        score = {w: c * math.log((1 + Nd) / (1 + df[w])) for w, c in d.items() if c >= 3}
        rec["terms"] = [w for w, _ in sorted(score.items(), key=lambda x: -x[1])[:15]]
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / ("evidence-%s.jsonl" % COCITE_DB.stem)
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


def main():
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)     # `| head` ends quietly instead of with a traceback
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    view = argparse.ArgumentParser(add_help=False)
    view.add_argument("--courts", help="only citing decisions from these court levels, e.g. appellate "
                                       "(default: all; each view has its own results file)")
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
    p.set_defaults(func=cmd_evidence)
    p = sub.add_parser("labels", parents=[view], help="import / match / list lineage descriptions")
    p.add_argument("action", choices=["import", "match", "list"])
    p.add_argument("file", nargs="?", help="JSON lines to import")
    p.set_defaults(func=cmd_labels)
    p = sub.add_parser("note", parents=[view], help="preview the case-note section (default view: appellate)")
    p.add_argument("--cite")
    p.add_argument("--cluster", type=int)
    p.set_defaults(func=cmd_note)
    p = sub.add_parser("tree", parents=[view], help="one slice: clusters, largest first, each with its sub-clusters")
    p.add_argument("slice", type=int, help="any year in the slice, e.g. 2010")
    p.add_argument("--top", type=int, default=20, help="clusters shown (default %(default)s)")
    p.add_argument("--subs", type=int, default=10, help="sub-clusters shown per cluster (default %(default)s)")
    p.set_defaults(func=cmd_tree)
    p = sub.add_parser("lineage", parents=[view], help="one lineage across slices")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_lineage)
    p = sub.add_parser("together", parents=[view], help="do these cases share a cluster, slice by slice?")
    p.add_argument("--cite", action="append", default=[])
    p.add_argument("--cluster", type=int, action="append")
    p.set_defaults(func=cmd_together)
    args = ap.parse_args()
    global COCITE_DB
    if getattr(args, "courts", None):
        _codes, tag = parse_courts(args.courts)
        if tag != "all":
            COCITE_DB = BASE_DB.with_name("cocite-%s.sqlite" % tag)
    if args.cmd in ("case", "note") and not (args.cite or args.cluster):
        ap.error("case needs --cite or --cluster")
    args.func(args)


if __name__ == "__main__":
    main()
