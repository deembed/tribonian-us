#!/usr/bin/env python3
"""usc-citedby: 'Cited by' and 'Recent appellate cites' for US Code notes, on a tiny fake database, offline.

    python3 -m unittest tests.test_usc_citedby -v
"""
import shutil, sqlite3, sys, tempfile, types, unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cocite                                  # noqa: E402
import cocite_usc as cu                        # noqa: E402


class FakeMap:
    ends = {"18:80": [1948]}                   # 18 U.S.C. § 80 reused in 1948

    def node(self, key, year=0):
        for y in self.ends.get(key, ()):
            if year and year < y:
                return "%s@-%d" % (key, y)
        return key


# decision: (year, level, filed, name); levels 1 scotus, 2 circuit, 3 district, 5 state-appellate
CASES = {1: (2024, 2, "2024-03-01", "Alpha"), 2: (2025, 2, "2025-06-01", "Beta"), 3: (2010, 1, "2010-01-01", "Gamma"),
         4: (2025, 3, "2025-07-01", "Delta"), 5: (2023, 5, "2023-02-02", "Eps"), 6: (1940, 1, "1940-05-05", "Old"),
         7: (2025, 2, "2025-01-15", "Zeta")}
REFS = [(1, "42:1983"), (2, "42:1983"), (3, "42:1983"), (4, "42:1983"), (5, "42:1983"), (7, "42:1983"),
        (6, "18:80"), (2, "18:80")]
CITED_BY = {1: 3, 2: 1, 3: 50, 4: 9, 5: 0, 6: 7, 7: 2}


class CitedBy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        con = sqlite3.connect(":memory:")
        con.executescript("""CREATE TABLE loaded(name TEXT, file TEXT);
            CREATE TABLE clusters(id INTEGER, case_name_short TEXT, case_name TEXT, date_filed TEXT, slug TEXT);
            CREATE TABLE cites(cluster_id INTEGER, volume TEXT, reporter TEXT, page TEXT, type INTEGER);""")
        con.execute("INSERT INTO loaded VALUES ('citation-map', 'citation-map-2025-09-30.csv.bz2')")
        for i, (y, l, f, n) in CASES.items():
            con.execute("INSERT INTO clusters VALUES (?,?,?,?,?)", (i, n, n + " v. X", f, None if i == 4 else n.lower() + "-v-x"))
            con.execute("INSERT INTO cites VALUES (?,?,?,?,?)", (i, str(i), "F.4th", "1", 1))
        cd = np.array(sum(([i] * n for i, n in CITED_BY.items()), []), np.int32)
        np.save(cls.tmp / "cited.npy", cd)
        lvl = np.zeros(10, np.int8)
        for i, (y, l, f, n) in CASES.items():
            lvl[i] = l
        np.save(cls.tmp / "level.npy", lvl)
        keys = sorted({k for _, k in REFS})
        refs = sorted((d, keys.index(k)) for d, k in REFS)
        dec = np.array([d for d, _ in refs], np.int32)
        key = np.array([k for _, k in refs], np.int32)
        yr = np.array([CASES[d][0] for d in dec], np.int32)
        cls.saved = (cu.load_refs, cu.load_map, cu.usc_dir, cocite.main_db, cocite.CACHE)
        cu.load_refs = lambda courts=None: (dec, key, yr, keys)
        cu.load_map = FakeMap
        cu.usc_dir = lambda out=None: cls.tmp
        cocite.main_db = lambda: con
        cocite.CACHE = cls.tmp
        cu.cmd_usc_citedby(types.SimpleNamespace(top=3, years=2))
        cu._CB.clear()
        cu._cb_state()
        cu._CB["index"] = {"3": {"slug": "gamma-note"}}

    @classmethod
    def tearDownClass(cls):
        cu.load_refs, cu.load_map, cu.usc_dir, cocite.main_db, cocite.CACHE = cls.saved
        cu._CB.clear()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_cited_by_most_cited_first(self):
        md = "\n".join(cu.citedby_md("/us/usc/t42/s1983", top=3))
        self.assertIn("## Cited by", md)
        self.assertIn("6 decisions cite it; the 3 most cited:", md)
        rows = [l for l in md.splitlines() if l.startswith("| [")]
        self.assertEqual(len(rows), 3)
        self.assertIn("[[gamma-note\\|Gamma]]", rows[0])            # the case note when there is one
        self.assertIn("Delta", rows[1])                              # district courts count here
        self.assertIn("(https://www.courtlistener.com/opinion/4/delta-v-x/)", rows[1])   # no slug in the database: from the name
        self.assertIn("(https://www.courtlistener.com/opinion/1/alpha-v-x/)", rows[2])   # the database's slug

    def test_recent_is_appellate_newest_first(self):
        md = cu.recent_md("/us/usc/t42/s1983", top=3)
        rows = [l for l in md if l.startswith("| 20")]
        self.assertEqual([r.split(" | ")[0] for r in rows], ["| 2025-06-01", "| 2025-01-15", "| 2024-03-01"])
        self.assertFalse(any("Delta" in r for r in rows))            # district court left out
        per = [l for l in md if l.startswith("Per year:")][0]
        self.assertIn("2023 1 · 2024 1 · 2025 (to 09-30) 2", per)
        self.assertIn("(all years: 5)", per)

    def test_reused_number_split_by_era(self):
        md = "\n".join(cu.citedby_md("/us/usc/t18/s80"))
        self.assertIn("### As cited before 1948", md)
        self.assertIn("Old", md.split("### As cited before 1948")[1])
        self.assertNotIn("Old", md.split("### As cited before 1948")[0])

    def test_nothing_for_uncited_section(self):
        self.assertEqual(cu.citedby_md("/us/usc/t5/s552"), [])
        self.assertEqual(cu.recent_md("/us/usc/t5/s552"), [])


if __name__ == "__main__":
    unittest.main()
