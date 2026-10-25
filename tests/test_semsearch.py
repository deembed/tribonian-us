#!/usr/bin/env python3
"""semsearch: re-ranking, duplicate collapse, and the acceptance query, offline.

    python3 -m unittest tests.test_semsearch -v

The acceptance test replays two real result pages (tests/fixtures/semsearch-1983-scotus.json) through
the response cache, so it runs the real fetch/rerank/write path with no network and no token.
"""
import json, shutil, sys, tempfile, types, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import semsearch as ss                         # noqa: E402

FIX = json.loads((ROOT / "tests" / "fixtures" / "semsearch-1983-scotus.json").read_text())


def hit(cid, name, d, cites, sem, bm25=1.0):
    return {"cluster_id": cid, "caseName": name, "dateFiled": d, "citeCount": cites,
            "meta": {"score": {"bm25": bm25, "semantic": sem}}, "_window": "w", "_srank": 1}


class Rerank(unittest.TestCase):
    def test_semantic_not_server_order(self):
        out = ss.rerank([hit(1, "A v. B", "2000-01-01", 5, 1.0, bm25=9), hit(2, "C v. D", "2000-01-01", 5, 2.0, bm25=1)])
        self.assertEqual([h["cluster_id"] for h in out], [2, 1])

    def test_revisions_duplicates_collapse_to_most_cited(self):
        out = ss.rerank([hit(10, "Loper Bright Enterprises v. Raimondo", "2024-06-28", 50, 5.0),
                         hit(11, "Loper Bright Enterprises v. Raimondo Revisions: 6/27/24", "2024-06-28", 900, 4.0)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["cluster_id"], 11)          # most cited record kept
        self.assertEqual(out[0]["semantic"], 5.0)           # the group's best score
        self.assertEqual(out[0]["also"], [10])
        self.assertEqual(out[0]["case_name"], "Loper Bright Enterprises v. Raimondo")

    def test_same_name_other_date_stays_separate(self):
        out = ss.rerank([hit(1, "Brown v. Board of Education", "1954-05-17", 9, 1.0),
                         hit(2, "Brown v. Board of Education", "1955-05-31", 9, 1.0)])
        self.assertEqual(len(out), 2)


class Windows(unittest.TestCase):
    def args(self, **kw):
        return types.SimpleNamespace(**dict(dict(window=None, since=None, courts=None), **kw))

    def test_default_is_newest_slice_only(self):
        saved = ss.slice_windows
        ss.slice_windows = lambda view=None: [("1990-01-01", "1994-12-31", "1990-94"), ("1995-01-01", "1999-12-31", "1995-99"),
                                              ("2000-01-01", "2004-12-31", "2000-04")]
        try:
            self.assertEqual([w[2] for w in ss.windows_for(self.args())], ["2000-04"])
            self.assertEqual([w[2] for w in ss.windows_for(self.args(since=1995))], ["1995-99", "2000-04"])
            self.assertEqual(len(ss.windows_for(self.args(since=1700))), 3)
        finally:
            ss.slice_windows = saved


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = ss.OUT, ss.CACHE
        ss.OUT, ss.CACHE = self.tmp / "semsearch", self.tmp / "semsearch" / "_cache"
        ss.CACHE.mkdir(parents=True)
        win = (FIX["window"][0], FIX["window"][1], "")
        for n, page in enumerate(FIX["pages"], 1):        # seed the cache as fetch_window writes it
            ss.cache_path(FIX["query"], FIX["court"], win, n).write_text(json.dumps(
                {"next": "cached" if n < len(FIX["pages"]) else None, "results": page}))

    def tearDown(self):
        ss.OUT, ss.CACHE = self.saved
        shutil.rmtree(self.tmp)

    def test_state_farm_first(self):
        args = types.SimpleNamespace(query=FIX["query"], file=None, court=FIX["court"], pages=2,
                                     window=["%s:%s" % tuple(FIX["window"])], since=None, courts=None,
                                     top=5, min_chars=ss.MIN_CHARS, out=None)
        # --window labels a window "START..END"; the cache key ignores the label
        out = ss.run(args)
        rec = json.loads(out.read_text())
        self.assertEqual(rec["requests"]["fetched"], 0)
        hits = rec["paragraphs"][0]["hits"]
        self.assertEqual(hits[0]["cluster_id"], 110991)
        self.assertEqual([h["cluster_id"] for h in hits].index(110953) + 1, 4)   # Baltimore Gas & Electric
        self.assertEqual(len(hits), 40)


if __name__ == "__main__":
    unittest.main()
