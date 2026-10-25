#!/usr/bin/env python3
"""check.py: probes against small synthetic databases, and the mode logic, offline.

    python3 -m unittest tests.test_check -v
"""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, unittest, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import check                                    # noqa: E402

SF = 110991


def main_db(path, with_probe=True):
    """The casedb.py tables, a few rows each; with_probe=False is a sample load without State Farm."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE loaded(name TEXT PRIMARY KEY, file TEXT, rows INTEGER, seconds REAL, finished TEXT);
        CREATE TABLE courts(id TEXT PRIMARY KEY, citation_string TEXT, short_name TEXT, full_name TEXT, jurisdiction TEXT);
        CREATE TABLE cites(cluster_id INTEGER, volume TEXT, reporter TEXT, page TEXT, type INTEGER);
        CREATE TABLE edges(citing INTEGER, cited INTEGER, depth INTEGER);
        CREATE TABLE parens(id INTEGER, described INTEGER, describing INTEGER, score REAL, group_id INTEGER, text TEXT);
        CREATE TABLE clusters(id INTEGER, case_name TEXT, docket_id INTEGER);
        CREATE TABLE dockets(id INTEGER, docket_number TEXT);
        CREATE TABLE opmap(id INTEGER PRIMARY KEY, cluster_id INTEGER);
    """)
    for name in ("courts", "citations", "parentheticals", "citation-map", "opinion-clusters", "dockets"):
        c.execute("INSERT INTO loaded VALUES (?, ?, 1, 0, '')", (name, name + "-2026-06-30.csv.bz2"))
    c.execute("INSERT INTO loaded VALUES ('opinions', 'opinions-2026-06-30.csv.bz2', 1, 0, '')")
    c.execute("INSERT INTO loaded VALUES ('opmap', 'opinions-2026-06-30.csv.bz2', 1, 0, '')")
    c.execute("INSERT INTO courts VALUES ('scotus', 'SCOTUS', 'Supreme Court', 'Supreme Court of the United States', 'F')")
    cid = SF if with_probe else 1
    c.execute("INSERT INTO cites VALUES (?, '463', 'U.S.', '29', 1)", (cid,))
    c.execute("INSERT INTO clusters VALUES (?, 'Motor Vehicle v. State Farm', 7)", (cid,))
    c.execute("INSERT INTO dockets VALUES (7, '82-354')")
    c.execute("INSERT INTO opmap VALUES (500, ?)", (cid,))
    c.executemany("INSERT INTO edges VALUES (?, 500, 1)", [(600,), (601,)])
    c.execute("INSERT INTO parens VALUES (1, 500, 600, 1, 1, 'holding that ...')")
    c.commit()


def run(env):
    out = subprocess.run([sys.executable, str(ROOT / "check.py"), "--json"], capture_output=True, text=True,
                         cwd=ROOT, env=dict(os.environ, **env))
    rep = json.loads(out.stdout)
    return out.returncode, rep, {r["key"]: r for r in rep["checks"]}


class Probes(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.env = {"CL_DB": str(self.d / "courtlistener.sqlite"), "CL_OPS_DB": str(self.d / "opinions.sqlite"),
                    "CL_SEARCH_DB": str(self.d / "search.sqlite"), "CL_COCITE_DB": str(self.d / "cocite.sqlite"),
                    "COURTLISTENER_TOKEN": "test",
                    "TRIBONIAN_CONFIG": str(self.d / "no-config.toml")}     # not the user's own config file

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_nothing_there(self):
        code, rep, by = run(self.env)
        self.assertEqual(by["db:file"]["status"], "fail")
        self.assertEqual(by["db:courts"]["status"], "skip")
        self.assertEqual(by["ops:text"]["status"], "fail")
        self.assertFalse(rep["modes"]["lite"]["ok"])
        self.assertIn("courtlistener.sqlite", rep["modes"]["lite"]["missing"])
        self.assertEqual(rep["paths"]["main_db"], self.env["CL_DB"])

    def test_lite_tables_answer(self):
        main_db(self.env["CL_DB"])
        code, rep, by = run(self.env)
        for k in check.LITE_TABLES + ["db:file", "db:dockets"]:
            self.assertEqual(by[k]["status"], "ok", (k, by[k]))
        self.assertIn("cited by 2 opinions", by["db:citation-map"]["detail"])
        self.assertIn("1 parentheticals", by["db:parentheticals"]["detail"])
        self.assertIn("docket 82-354", by["db:dockets"]["detail"])

    def test_sample_load_warns(self):
        main_db(self.env["CL_DB"], with_probe=False)
        code, rep, by = run(self.env)
        self.assertEqual(by["db:citations"]["status"], "warn")
        self.assertEqual(by["db:courts"]["status"], "ok")
        self.assertTrue(rep["modes"]["api"]["ok"] or by["tool:python"]["status"] == "fail")

    def test_opinion_text(self):
        o = sqlite3.connect(self.env["CL_OPS_DB"])
        o.execute("CREATE TABLE opinions(id INTEGER, cluster_id INTEGER, type TEXT, author_str TEXT, field TEXT, text BLOB)")
        o.execute("CREATE INDEX opinions_by_cluster ON opinions(cluster_id)")
        o.execute("INSERT INTO opinions VALUES (500, ?, 'lead', 'White', 'html', ?)", (SF, zlib.compress(b"arbitrary and capricious")))
        o.commit()
        code, rep, by = run(self.env)
        self.assertEqual(by["ops:text"]["status"], "ok", by["ops:text"])

    def test_json_shape_the_gui_reads(self):
        # gui/src/check_page.rs: every item field is a string; paths values are strings, null or a table
        code, rep, by = run(self.env)
        for c in rep["checks"]:
            for k in ("group", "label", "status", "detail", "fix"):
                self.assertIsInstance(c[k], str, (c["key"], k))
        self.assertIsNone(rep["paths"]["config_error"])
        self.assertIsInstance(rep["paths"]["gui"], dict)

    def test_settings_name_their_source(self):
        code, rep, by = run(self.env)
        self.assertIn("from CL_DB", by["cfg:main_db"]["detail"])
        self.assertEqual(rep["paths"]["main_db"], self.env["CL_DB"])

    def test_missing_table_fails_with_fix(self):
        main_db(self.env["CL_DB"])
        c = sqlite3.connect(self.env["CL_DB"])
        c.execute("DELETE FROM loaded WHERE name='parentheticals'")
        c.commit()
        code, rep, by = run(self.env)
        self.assertEqual(by["db:parentheticals"]["status"], "fail")
        self.assertEqual(by["db:parentheticals"]["fix"], "./casedb.py load parentheticals")
        self.assertFalse(rep["modes"]["lite"]["ok"])


class Modes(unittest.TestCase):
    def test_each_mode_contains_the_one_below(self):
        self.assertTrue(set(check.NEEDS["api"]) <= set(check.NEEDS["lite"]) <= set(check.NEEDS["full"]))

    def test_best_is_highest_ready(self):
        saved = check.RESULTS[:]
        try:
            check.RESULTS[:] = []
            for k in check.NEEDS["full"]:
                c = check.Check(k, "g", k)
                c.status = "fail" if k == "ops:text" else "ok"
                check.RESULTS.append(c)
            best, ms = check.modes()
            self.assertEqual(best, "lite")
            self.assertEqual(ms["full"]["missing"], ["ops:text"])
        finally:
            check.RESULTS[:] = saved


if __name__ == "__main__":
    unittest.main()
