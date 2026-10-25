"""cocite_shift.py -- good-law signals from co-citation cluster change (scope: docs/scope-cocite-next.md, section A).

    tribonian cocite shift --cite "375 U.S. 384" --courts appellate     # one case, every slice transition
    tribonian cocite shift --eval --courts appellate                     # goodlaw_eval.py's cases, scored

Kept out of cocite.py so it can be developed beside other work on that file; it only reads
cocite.py's results databases and cocite-cache/ (built by tribonian cocite prep and build).
"""
import argparse, json, math, sys, time

import numpy as np

from tribonian import cocite as cc
from tribonian.cocite import CACHE, WIDTH, EVIDENCE_DIR, parse_courts, label, short, name, need_db, resolve

_MAIN = []


def main_db():
    """One read-only connection to the main database, opened on first use."""
    if not _MAIN:
        _MAIN.append(cc.main_db())
    return _MAIN[0]

# ================================================================ shift: good-law signals from cluster change
# Does a case's place in the co-citation structure change in a recognisable way when it is
# overruled? Four signals per slice transition s -> t (t = s + 5), read from the cache and the
# view's results; nothing is rebuilt. A flag only: co-citation sees displacement, not its reason
# (Bowles/Thompson, Twombly/Conley and Mead/Chevron can look alike); casemap.py goodlaw judges.
#   1 successor  a later-decided partner B: share = decisions citing A and B / decisions citing A
#                >= --share at t, and B's citing count growing >= --growth times faster than A's
#   2 break      A's sub-cluster lineage at s does not hold A at t (it ended, split away, or A left);
#                "-> B" when A's new sub-cluster at t is the one B sits in
#   3 collapse   A's citing count s -> t falls further than all but --cohort-pct of its cohort
#                (cases of the same decade cited about as often at s); raw decline is normal aging
#   4 core->edge A in the top 10% of its sub-cluster by inside weight at s, bottom half at t;
#                or its home-cluster share falls from >= 0.5 to under 0.5

SHIFT = dict(share=0.25, growth=3.0, cohort_pct=0.10, min_n=5, cand=40)   # min_n: citing decisions (and shared ones) needed


class CiteIndex:
    """Who cites whom, under a court view, from cocite-cache/ (memory-mapped)."""

    def __init__(self, view, db=None):
        if not (CACHE / "by_cited.npy").exists():
            sys.exit("The cache has no cited-case index yet: run tribonian cocite prep")
        if db is not None:                # the view's results must come from this cache (cocite.check_cache)
            cc.check_cache(db)
        mm = lambda f: np.load(CACHE / f, mmap_mode="r")
        self.ci, self.cd, self.y, self.lvl = mm("citing.npy"), mm("cited.npy"), mm("year.npy"), mm("level.npy")
        self.order, self.cds = mm("by_cited.npy"), mm("cited_sorted.npy")
        self.codes, _ = parse_courts(view)
        self._citers, self._slice_counts, self._ref_year, self._counts = {}, {}, None, None
        self.view = view or "all"

    def citers(self, ref):
        """(decisions citing ref, their slice) under the view."""
        if ref not in self._citers:
            key = np.int32(ref)               # an int64 key would cast the whole int32 array on every search
            lo, hi = np.searchsorted(self.cds, key), np.searchsorted(self.cds, key, side="right")
            rows = np.asarray(self.order[lo:hi]).astype(np.int64)
            D = np.asarray(self.ci[rows]) if len(rows) else np.zeros(0, np.int32)
            Y = np.asarray(self.y[rows]) if len(rows) else np.zeros(0, np.int16)
            if self.codes and len(D):
                keep = np.isin(np.asarray(self.lvl[D]), self.codes)
                D, Y = D[keep], Y[keep]
            self._citers[ref] = (D, (Y - Y % WIDTH).astype(np.int32))
        return self._citers[ref]

    def n_in(self, ref, s):
        D, S = self.citers(ref)
        return int((S == s).sum())

    def cited_by(self, D):
        """Every case cited by the decisions D (one entry per decision and case)."""
        D = np.asarray(D)
        if not len(D):
            return np.zeros(0, np.int32)
        D = D.astype(np.int32)
        starts, ends = np.searchsorted(self.ci, D), np.searchsorted(self.ci, D, side="right")
        lens = ends - starts
        idx = np.arange(lens.sum()) + np.repeat(starts - np.r_[0, np.cumsum(lens)[:-1]], lens)
        return np.asarray(self.cd[idx])

    def ref_year(self):
        """Filing year of every case, by cluster id (0 = unknown): prep's ref_year.npy, from the same
        clusters.date_filed that case_years() dates A from, so A and its cohort share one clock. (Until
        2026-10 this was taken from the citing side -- the year of a case's own citations -- so a case
        that cites nothing had decade 0 and silently fell out of every cohort.)"""
        if self._ref_year is None:
            if not (CACHE / "ref_year.npy").exists():
                sys.exit("The cache has no filing years yet: run tribonian cocite prep (it adds only those)")
            self._ref_year = np.load(CACHE / "ref_year.npy", mmap_mode="r")
        return self._ref_year

    def slice_counts(self, s):
        """(cases, citing decisions in slice s) for every case cited in s, under the view.
        All slices are counted in one pass and kept in build/cocite/shift-counts-<view>.npz."""
        if self._counts is None:
            stamp = (CACHE / "stamp.txt").read_text()
            path = EVIDENCE_DIR / ("shift-counts-%s.npz" % self.view)
            try:
                z = np.load(path)
                if str(z["stamp"]) != stamp:
                    raise ValueError
                self._counts = (z["keys"], z["counts"])
            except (FileNotFoundError, ValueError, KeyError):
                t0 = time.time()
                y = np.asarray(self.y)
                key = (y - y % WIDTH).astype(np.int64) << 32
                key |= np.asarray(self.cd).astype(np.int64)
                if self.codes:
                    key = key[np.isin(np.asarray(self.lvl)[np.asarray(self.ci)], self.codes)]
                keys, counts = np.unique(key, return_counts=True)
                del key
                self._counts = (keys, counts.astype(np.int32))
                EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.stem + ".tmp.npz")
                np.savez(tmp, keys=keys, counts=self._counts[1], stamp=np.array(stamp))
                tmp.replace(path)
                print("slice counts: %s (case, slice) pairs -> %s  (%.0f s)" % (format(len(keys), ","), path, time.time() - t0),
                      flush=True)
        if s not in self._slice_counts:
            keys, counts = self._counts
            lo, hi = np.searchsorted(keys, np.int64(s) << 32), np.searchsorted(keys, np.int64(s + 1) << 32)
            self._slice_counts[s] = ((keys[lo:hi] & 0xFFFFFFFF).astype(np.int64), counts[lo:hi])
        return self._slice_counts[s]


def case_years(db, refs, cache={}):
    """Decision year per case: the view's names table, else the main database."""
    want = [r for r in set(refs) if r not in cache]
    for i in range(0, len(want), 500):
        ids = want[i:i + 500]
        q = ",".join("?" * len(ids))
        for r, yr in db.execute("SELECT ref, year FROM names WHERE ref IN (%s)" % q, ids):
            if yr:
                cache[r] = yr
    miss = [r for r in want if r not in cache]
    if miss:
        con = main_db()
        for i in range(0, len(miss), 500):
            ids = miss[i:i + 500]
            q = ",".join("?" * len(ids))
            for r, f in con.execute("SELECT id, date_filed FROM clusters WHERE id IN (%s)" % q, ids):
                cache[r] = int(f[:4]) if f and f[:4].isdigit() else 0
    return {r: cache.get(r, 0) for r in refs}


def node_row(db, s, ref):
    """(cl, sub, subw, sub lineage) of a case in slice s, or None when it is not a node there."""
    r = db.execute("SELECT n.cl, n.sub, n.subw, s.lineage FROM nodes n LEFT JOIN subs s ON s.slice = n.slice AND s.sub = n.sub "
                   "WHERE n.slice=? AND n.ref=?", (s, ref)).fetchone()
    return r


def sub_rank(db, s, sub, subw):
    """Share of the sub-cluster's members with more inside weight (0 = the top)."""
    tot, above = db.execute("SELECT count(*), sum(subw > ?) FROM nodes WHERE slice=? AND sub=?", (subw, s, sub)).fetchone()
    return (above or 0) / max(tot, 1)


def home_share(db, s, ref):
    r = db.execute("SELECT share FROM members WHERE slice=? AND ref=? AND home=1", (s, ref)).fetchone()
    return r[0] if r else None


def cohort_pct(idx, s, t, n_s, n_t, decade):
    """Share of A's cohort whose citing count fell more than A's from s to t (low = unusual collapse)."""
    rs, cs = idx.slice_counts(s)
    rt, ct = idx.slice_counts(t)
    ry = idx.ref_year()
    dec = ry[np.minimum(rs, len(ry) - 1)] // 10 * 10
    for fac, dspan in ((1.5, 0), (2.0, 10), (3.0, 20)):
        sel = (cs >= n_s / fac) & (cs <= n_s * fac) & (np.abs(dec.astype(int) - decade) <= dspan) & (rs < len(ry))
        if sel.sum() >= 30:
            break
    if sel.sum() < 10:
        return None, int(sel.sum())
    refs, cnt_s = rs[sel], cs[sel]
    pos = np.searchsorted(rt, refs)
    pos = np.minimum(pos, len(rt) - 1)
    cnt_t = np.where(rt[pos] == refs, ct[pos], 0)
    g = np.log((cnt_t + 1) / (cnt_s + 1))
    mine = math.log((n_t + 1) / (n_s + 1))
    return float((g < mine).mean()), int(sel.sum())


def shift_rows(db, idx, ref, p, slices, only_successor=False):
    """One row per slice transition s -> t for case ref: which signals fire, and the best successor.
    only_successor: signal 1 alone (what the case note uses; skips the cohort counts)."""
    yA = case_years(db, [ref])[ref]
    D, S = idx.citers(ref)
    built = set(slices)
    out = []
    for s in slices:
        t = s + WIDTH
        if t not in built or t <= yA - yA % WIDTH:
            continue
        nA_s, nA_t = int((S == s).sum()), int((S == t).sum())
        if nA_s + nA_t < p["min_n"]:
            continue
        row = {"s": s, "t": t, "n_s": nA_s, "n_t": nA_t, "fire": set()}
        # 1 successor
        cands = []
        if nA_t >= p["min_n"]:
            co = idx.cited_by(D[S == t])
            co = co[co != ref]
            u, c = np.unique(co, return_counts=True)
            top = np.argsort(-c, kind="stable")[:p["cand"]]
            u, c = u[top], c[top]
            yrs = case_years(db, u.tolist())
            for b, k in zip(u.tolist(), c.tolist()):
                if yrs[b] <= yA:
                    continue
                nb_s, nb_t = idx.n_in(b, s), idx.n_in(b, t)
                growth = ((nb_t + 1) / (nb_s + 1)) / ((nA_t + 1) / (nA_s + 1))
                cands.append({"ref": b, "share": k / nA_t, "shared": k, "growth": growth, "year": yrs[b],
                              "ok": k >= p["min_n"] and k / nA_t >= p["share"] and growth >= p["growth"]})
        firing = [c for c in cands if c["ok"]]
        best = max(firing or cands, key=lambda c: c["share"]) if cands else None
        row["best"] = best
        if firing:
            row["fire"].add("1")
        if only_successor:
            out.append(row)
            continue
        # 2 lineage break, 4 core -> edge (need A to be a node at s)
        a_s, a_t = node_row(db, s, ref), node_row(db, t, ref)
        row["break"] = row["to_b"] = False
        row["rank_s"] = row["rank_t"] = row["hs_s"] = row["hs_t"] = None
        if a_s and a_s[1] and a_s[1] > 0:
            lin_s = a_s[3]
            lin_t = a_t[3] if a_t and a_t[1] and a_t[1] > 0 else None
            if lin_t != lin_s:
                row["break"] = True
                row["fire"].add("2")
                if best and lin_t is not None:
                    b_t = node_row(db, t, best["ref"])
                    anchor = db.execute("SELECT anchor FROM subs WHERE slice=? AND sub=?", (t, a_t[1])).fetchone()
                    if (b_t and b_t[1] == a_t[1]) or (anchor and anchor[0] == best["ref"]):
                        row["to_b"] = True
            row["rank_s"] = sub_rank(db, s, a_s[1], a_s[2])
            if lin_t is not None:
                row["rank_t"] = sub_rank(db, t, a_t[1], a_t[2])
            row["hs_s"], row["hs_t"] = home_share(db, s, ref), home_share(db, t, ref) if a_t else None
            core_edge = row["rank_s"] <= 0.10 and (row["rank_t"] is None or row["rank_t"] >= 0.5)
            hs_drop = (row["hs_s"] or 0) >= 0.5 and (row["hs_t"] is None or row["hs_t"] < 0.5)
            if core_edge or hs_drop:
                row["fire"].add("4")
        # 3 collapse against the cohort
        row["drop"] = bool(a_s) and not a_t
        row["pct"], row["cohort"] = (None, 0)
        if nA_s >= p["min_n"]:
            row["pct"], row["cohort"] = cohort_pct(idx, s, t, nA_s, nA_t, yA // 10 * 10)
            if row["pct"] is not None and row["pct"] <= p["cohort_pct"]:
                row["fire"].add("3")
        out.append(row)
    return out


def sname(db, ref, cache={}):
    """short(), falling back to the main database for cases that were never a node."""
    if ref not in cache:
        r = db.execute("SELECT name FROM names WHERE ref=?", (ref,)).fetchone()
        if not r:
            r = main_db().execute("SELECT coalesce(nullif(case_name_short, ''), case_name) FROM clusters WHERE id=?",
                                  (ref,)).fetchone()
        cache[ref] = (r[0] if r and r[0] else "cluster %d" % ref)
    return cache[ref]


# ---------------------------------------------------------------- the case note's successor line
SUCCESSOR_MIN = 10        # shared decisions the note needs; 5 (the signal's own floor) is noise for old,
                          # thinly cited cases (Dr. Miles 2010-14 "Iqbal 100%" rested on one or two decisions)
SUCCESSOR_RECENT = 10     # ... but a case decided within this many years of the newest slice keeps the floor at
                          # min_n: its counts are small because it is young, and a false flag there is acceptable
_IDX = {}


def successor(ref, view="appellate"):          # the binary view: the shift eval was run there, not on NOTE_VIEW
    """The latest slice where signal 1 names a successor, for the good-law box:
    {"slice", "ref", "name", "share", "shared", "citing"}; None when nothing fires or nothing is built.
    A flag only; it never changes the good-law colour."""
    try:
        db = cc.open_view(view)
        if db is None or not (CACHE / "by_cited.npy").exists():
            return None
        if view not in _IDX:
            _IDX[view] = CiteIndex(view, db)
        slices = [r[0] for r in db.execute("SELECT slice FROM slices ORDER BY slice")]
        yA = case_years(db, [ref])[ref]
        floor = SHIFT["min_n"] if yA >= max(slices) - SUCCESSOR_RECENT else SUCCESSOR_MIN
        for r in reversed(shift_rows(db, _IDX[view], ref, SHIFT, slices, only_successor=True)):
            b = r["best"]
            if "1" in r["fire"] and b and b["shared"] >= floor:
                return {"slice": label(r["t"]), "ref": b["ref"], "name": name_of(db, b["ref"]),
                        "share": round(b["share"], 3), "shared": b["shared"], "citing": r["n_t"]}
    except (Exception, SystemExit) as e:      # no database, an old cache: the note simply goes without
        print("  note: no successor check (%s)" % e)
    return None


def name_of(db, ref):
    """'Bowles, 551 U.S. 205 (2007)', from the names table or the main database."""
    r = db.execute("SELECT name, cite, year FROM names WHERE ref=?", (ref,)).fetchone()
    if r:
        return "%s, %s (%s)" % r
    return sname(db, ref)


def shift_line(db, r):
    b = r["best"]
    succ = ("%-34s %3.0f%% x%-5s" % (sname(db, b["ref"])[:34], 100 * b["share"],
                                    "%.1f" % b["growth"] if b["growth"] < 100 else ">99")) if b else "-".ljust(46)
    brk = ("break->B" if r["to_b"] else "break") if r["break"] else ""
    pct = "%3.0f%%" % (100 * r["pct"]) if r["pct"] is not None else "  - "
    rank = ("%3.0f>%s" % (100 * r["rank_s"], "%3.0f" % (100 * r["rank_t"]) if r["rank_t"] is not None else "out")
            if r["rank_s"] is not None else "")
    hs = ("%.2f>%s" % (r["hs_s"], "%.2f" % r["hs_t"] if r["hs_t"] is not None else "-")) if r["hs_s"] is not None else ""
    return "%s>%s %5d>%-5d %-4s %s%s %-9s %s%s %-8s %s" % (
        label(r["s"]), label(r["t"])[2:], r["n_s"], r["n_t"], "".join(sorted(r["fire"])) or ".",
        "*" if "1" in r["fire"] else " ", succ, brk, pct, " drop" if r["drop"] else "     ", rank, hs)


SHIFT_HEAD = ("transition  citing      sig  successor (share of A's citers citing B, growth vs A)     lineage  cohort      "
              "rank%  home share")


def cmd_shift(args):
    db = need_db()
    p = dict(SHIFT, share=args.share, growth=args.growth, cohort_pct=args.cohort_pct)
    view = args.courts or "all"
    idx = CiteIndex(view, db)
    slices = [r[0] for r in db.execute("SELECT slice FROM slices ORDER BY slice")]
    if args.eval:
        return shift_eval(db, idx, p, slices, args)
    ref = args.cluster or resolve(args.cite)
    print(name(db, ref))
    print(SHIFT_HEAD)
    for r in shift_rows(db, ref=ref, idx=idx, p=p, slices=slices):
        if args.all or r["fire"] or r["n_s"] >= p["min_n"]:
            print(shift_line(db, r))
    print("sig: 1 successor, 2 lineage break, 3 collapse vs cohort, 4 core->edge.  cohort = share of similar cases "
          "that fell further (low = unusual); rank% = members above A by inside weight, s>t")


# ---------------------------------------------------------------- shift --eval
# Positives: goodlaw_eval.py's 40 Supreme Court overrulings (plus Thompson v. INS / Bowles).
# Negatives: its 20 never-overruled cases. Hard negatives: extended-not-overruled pairs, where
# a successor flag naming the partner is a false alarm. Hit = fires at a transition into the
# overruling's slice or the next; lead = fires into one of the three slices before.

SHIFT_SUCCESSOR = {     # overruled case -> the overruling decision (for checking the named successor)
    "163 U.S. 537": "347 U.S. 483", "478 U.S. 186": "539 U.S. 558", "410 U.S. 113": "597 U.S. 215",
    "505 U.S. 833": "597 U.S. 215", "467 U.S. 837": "603 U.S. 369", "310 U.S. 586": "319 U.S. 624",
    "261 U.S. 525": "300 U.S. 379", "316 U.S. 455": "372 U.S. 335", "338 U.S. 25": "367 U.S. 643",
    "41 U.S. 1": "304 U.S. 64", "494 U.S. 652": "558 U.S. 310", "431 U.S. 209": "585 U.S. 878",
    "426 U.S. 833": "469 U.S. 528", "504 U.S. 298": "585 U.S. 162", "492 U.S. 361": "543 U.S. 551",
    "277 U.S. 438": "389 U.S. 347", "448 U.S. 56": "541 U.S. 36", "482 U.S. 496": "501 U.S. 808",
    "473 U.S. 172": "588 U.S. 180", "406 U.S. 404": "590 U.S. 83", "440 U.S. 410": "587 U.S. 230",
    "491 U.S. 1": "517 U.S. 44", "473 U.S. 402": "521 U.S. 203", "220 U.S. 373": "551 U.S. 877",
    "475 U.S. 625": "556 U.S. 778", "536 U.S. 545": "570 U.S. 99", "550 U.S. 192": "576 U.S. 591",
    "495 U.S. 508": "509 U.S. 688", "490 U.S. 805": "501 U.S. 808", "468 U.S. 447": "577 U.S. 92",
    "533 U.S. 194": "555 U.S. 223", "497 U.S. 639": "536 U.S. 584", "380 U.S. 202": "476 U.S. 79",
    "247 U.S. 251": "312 U.S. 100", "302 U.S. 277": "383 U.S. 663", "106 U.S. 583": "379 U.S. 184",
    "316 U.S. 584": "319 U.S. 105", "375 U.S. 384": "551 U.S. 205",
    "390 U.S. 145": "522 U.S. 3", "490 U.S. 638": "577 U.S. 92", "279 U.S. 263": "515 U.S. 506"}
SHIFT_EXTRA_POS = [("375 U.S. 384", 2007)]                       # Thompson v. INS, by Bowles v. Russell
SHIFT_HARD_NEG = [("467 U.S. 837", "533 U.S. 218", 2024),          # Chevron / Mead: narrowed, not overruled (until Loper Bright)
                  ("550 U.S. 544", "556 U.S. 662", None)]          # Twombly / Iqbal: extended

SHIFT_COMBOS = [
    ("1 successor", lambda r: "1" in r["fire"]),
    ("2 break", lambda r: "2" in r["fire"]),
    ("2 break->B", lambda r: r["to_b"]),
    ("3 collapse", lambda r: "3" in r["fire"]),
    ("4 core->edge", lambda r: "4" in r["fire"]),
    ("1+2", lambda r: {"1", "2"} <= r["fire"]),
    ("1+3", lambda r: {"1", "3"} <= r["fire"]),
    ("1+4", lambda r: {"1", "4"} <= r["fire"]),
    ("1+(2|3|4)", lambda r: "1" in r["fire"] and len(r["fire"]) >= 2),
    ("2+3", lambda r: {"2", "3"} <= r["fire"]),
    ("any 2 of 4", lambda r: len(r["fire"]) >= 2),
    ("any 3 of 4", lambda r: len(r["fire"]) >= 3),
]


def shift_eval(db, idx, p, slices, args):
    from tribonian import goodlaw_eval as ge
    con = main_db()

    def look(cite):
        try:
            return ge.lookup(con, cite)
        except SystemExit:
            return None
    newest = max(slices)
    pos = [(c, y, "A") for c, y in ge.SET_A] + [(c, y, "B") for c, y in ge.SET_B] + [(c, y, "x") for c, y in SHIFT_EXTRA_POS]
    neg = [(c, None, "A") for c in ge.CONTROL_A] + [(c, None, "B") for c in ge.CONTROL_B]
    cases = []
    for cite, yr, grp in pos + neg:
        ref = look(cite)
        if ref is None:
            print("  not in the database: %s" % cite)
            continue
        succ = look(SHIFT_SUCCESSOR[cite]) if cite in SHIFT_SUCCESSOR else None
        rows = shift_rows(db, idx, ref, p, slices)
        for r in rows:          # the overruling decision, matched by id or (a second record of it) by name
            r["is_succ"] = bool(succ and r["best"] and (r["best"]["ref"] == succ or sname(db, r["best"]["ref"]) == sname(db, succ)))
        cases.append({"cite": cite, "ref": ref, "year": yr, "grp": grp, "succ": succ, "rows": rows})
        print("  %-13s %-38s %s rows" % (cite, short(db, ref)[:38], len(rows)), flush=True)
    tsv = EVIDENCE_DIR / ("shift-eval-%s.tsv" % (args.courts or "all"))
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    with open(tsv, "w") as f:
        f.write("cite\tcase\toverruled\ttransition\tn_s\tn_t\tsignals\tsuccessor\tshare\tgrowth\tknown_successor\t"
                "break\tto_b\tcohort_pct\tdrop\trank_s\trank_t\thome_s\thome_t\n")
        for c in cases:
            for r in c["rows"]:
                b = r["best"] or {}
                f.write("\t".join(str(x) for x in (
                    c["cite"], short(db, c["ref"]), c["year"] or "", label(r["s"]) + ">" + label(r["t"]), r["n_s"], r["n_t"],
                    "".join(sorted(r["fire"])), sname(db, b["ref"]) if b else "", "%.3f" % b["share"] if b else "",
                    "%.2f" % b["growth"] if b else "", int(r["is_succ"]), int(r["break"]), int(r["to_b"]),
                    "" if r["pct"] is None else "%.3f" % r["pct"], int(r["drop"]),
                    "" if r["rank_s"] is None else "%.2f" % r["rank_s"], "" if r["rank_t"] is None else "%.2f" % r["rank_t"],
                    "" if r["hs_s"] is None else "%.2f" % r["hs_s"], "" if r["hs_t"] is None else "%.2f" % r["hs_t"])) + "\n")

    # per positive: what fired around the overruling
    print("\nPOSITIVES  (window = transitions into the overruling's slice and the next; '.' = nothing fired)")
    print("%-13s %-28s %5s  %-18s %-18s %-36s %s" % ("cite", "case", "year", "lead (3 before)", "window", "successor in window", "known?"))
    for c in cases:
        if not c["year"]:
            continue
        so = c["year"] - c["year"] % WIDTH
        win = [r for r in c["rows"] if r["t"] in (so, so + WIDTH)]
        lead = [r for r in c["rows"] if so - 3 * WIDTH <= r["t"] < so]
        fmt = lambda rs: " ".join("%s:%s" % (label(r["t"])[2:4], "".join(sorted(r["fire"])) or ".") for r in rs) or "-"
        bw = [r for r in win if r["best"]]
        bb = max(bw, key=lambda r: ("1" in r["fire"], r["best"]["share"]))["best"] if bw else None
        known = "-" if not c["succ"] else ("YES" if any(r["is_succ"] for r in win) else "no")
        note = "" if so <= newest else "  (after the newest slice)"
        print("%-13s %-28s %5d  %-18s %-18s %-36s %s%s" % (
            c["cite"], short(db, c["ref"])[:28], c["year"], fmt(lead), fmt(win),
            ("%s %.0f%%" % (sname(db, bb["ref"])[:28], 100 * bb["share"])) if bb else "-", known, note))

    # scores
    def window(c):
        so = c["year"] - c["year"] % WIDTH
        return so, [r for r in c["rows"] if r["t"] in (so, so + WIDTH)]
    P = [c for c in cases if c["year"] and window(c)[1]]
    N = [c for c in cases if not c["year"]]
    print("\nSCORES  (%d positives scoreable of %d; %d negatives; partial newest slice %s included)" % (
        len(P), sum(1 for c in cases if c["year"]), len(N), label(newest)))
    print("%-14s %9s %9s %8s %11s %12s %12s %10s" % ("signal", "detected", "lead", "lead exp", "neg: cases", "neg: trans.",
                                                      "pos: early", "succ.named"))
    for nm, f in SHIFT_COMBOS:
        det = sum(1 for c in P if any(f(r) for r in window(c)[1]))
        lead = sum(1 for c in P if any(f(r) for r in c["rows"] if window(c)[0] - 3 * WIDTH <= r["t"] < window(c)[0]))
        neg_c = sum(1 for c in N if any(f(r) for r in c["rows"]))
        nt = [r for c in N for r in c["rows"]]
        neg_t = sum(1 for r in nt if f(r))
        early = [r for c in P for r in c["rows"] if r["t"] < window(c)[0] - 3 * WIDTH]
        early_f = sum(1 for r in early if f(r))
        e = early_f / max(len(early), 1)
        lead_exp = sum(1 - (1 - e) ** sum(1 for r in c["rows"] if window(c)[0] - 3 * WIDTH <= r["t"] < window(c)[0]) for c in P)
        named = sum(1 for c in P if c["succ"] and any(f(r) and r["is_succ"] for r in window(c)[1]))
        print("%-14s %4d/%-4d %4d/%-4d %8.1f %5d/%-5d %5.1f%% %4d %5.1f%% %4d %4d/%-4d" % (
            nm, det, len(P), lead, len(P), lead_exp, neg_c, len(N), 100 * neg_t / max(len(nt), 1), len(nt),
            100 * early_f / max(len(early), 1), len(early), named, sum(1 for c in P if c["succ"])))
    print("detected = fires in the window; lead = fires in the 3 slices before it; lead exp = lead expected by chance, if those transitions fired at the pos: early rate; neg: cases = never-overruled cases with "
          "any fire; neg: trans. = share of their transitions firing; pos: early = share of positives' transitions more "
          "than 3 slices before the overruling that fire; succ.named = the overruling decision named as successor in the window")

    print("\nHARD NEGATIVES  (does the successor signal name the partner?)")
    for a_c, b_c, until in SHIFT_HARD_NEG:
        a, b = look(a_c), look(b_c)
        if not (a and b):
            continue
        c = next((c for c in cases if c["ref"] == a), None)
        rows = c["rows"] if c else shift_rows(db, idx, a, p, slices)
        hits = [r for r in rows if r["best"] and r["best"]["ref"] == b and (until is None or r["t"] < until - until % WIDTH)]
        print("  %s -> %s" % (short(db, a), short(db, b)))
        for r in hits:
            print("    " + shift_line(db, r))
        if not hits:
            print("    never named")
    print("\nall transitions: %s" % tsv)
