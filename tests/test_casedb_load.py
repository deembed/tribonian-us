#!/usr/bin/env python3
"""tribonian db load opinions: the pyarrow path and the Python path store the same rows; --limit leaves the main
database alone; opinion text reads back whichever codec wrote it.

    python -m pytest tests/test_casedb_load.py
"""
import bz2, json, os, sqlite3, subprocess, sys, tempfile, unittest, zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tribonian import casedb, textcodec                         # noqa: E402

# PostgreSQL CSV as CourtListener dumps it: every value quoted, \" for a quote, a NULL is nothing between commas.
HEADER = ["id", "date_created", "cluster_id", "type", "author_str", "plain_text", "html", "html_lawbox",
          "html_columbia", "html_anon_2020", "xml_harvard", "html_with_citations", "extracted_by_ocr"]


def q(v):
    if v is None:
        return b""
    if not isinstance(v, bytes):
        v = str(v).encode("utf-8")
    return b'"' + v.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def row(*vals):
    return b",".join(q(v) for v in vals) + b"\n"


ROWS = [
    row(1, "2020", 10, "lead", "White", "plain", "", "", "", "", "", '<p>a "quoted" word</p>', "f"),
    row(2, "2020", 10, "concur", "Rehnquist", "line one\nline two\r\nline three", None, None, None, None, None, None, "f"),
    row(3, "2020", 11, "", None, "", "", "", "", "", "<xml>back\\slash</xml>", "", "f"),
    row(4, "2020", 11, "lead", b"Bad \xff byte", b"text with a bad \xc3 byte and \xe2\x80\x94 a dash", "", "", "", "", "", "", "f"),
    row(5, "2020", 12, "lead", "Nobody", "", "", "", "", "", "", "", "f"),               # no text at all
    b'"6","2020","12","lead"\n',                                                       # wrong number of fields
    row("seven", "2020", 12, "lead", "X", "id is not a number", "", "", "", "", "", "", "f"),
    row(8, "2020", None, "lead", "Ünïcødé", "", "", "", "", "<p>état</p>", "", "", "f"),
]


def write_bulk(d):
    bulk = Path(d) / "bulk"
    bulk.mkdir()
    data = b",".join(q(h) for h in HEADER) + b"\n" + b"".join(ROWS)
    (bulk / "opinions-2026-09-30.csv.bz2").write_bytes(bz2.compress(data))
    return bulk


def load(d, bulk, tag, *extra, csv=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CL_", "TRIBONIAN_"))}
    env.update(PYTHONPATH=str(ROOT / "src"), TRIBONIAN_CONFIG=str(Path(d) / "none.toml"), TRIBONIAN_WORK=str(d),
               CL_BULK=str(bulk), CL_DB=str(Path(d) / tag / "courtlistener.sqlite"),
               CL_OPS_DB=str(Path(d) / tag / "opinions.sqlite"))
    if csv:
        env["TRIBONIAN_CSV"] = csv
    r = subprocess.run([sys.executable, "-m", "tribonian", "db", "load", "opinions", *extra], env=env,
                       capture_output=True, text=True)
    return r, Path(d) / tag


def stored(folder, name="opinions.sqlite"):
    con = sqlite3.connect(folder / name)
    return [(i, c, t, a, f, textcodec.unpack(x) if x else None)
            for i, c, t, a, f, x in con.execute("SELECT id, cluster_id, type, author_str, field, text FROM opinions ORDER BY rowid")]


def have_arrow():
    try:
        import pyarrow.csv, zstandard                           # noqa: F401
        return True
    except ImportError:
        return False


class Codec(unittest.TestCase):
    def test_reads_both(self):
        text = "état — text".encode("utf-8")
        self.assertEqual(textcodec.unpack(zlib.compress(text, 6)), "état — text")
        self.assertEqual(textcodec.unpack(textcodec.pack(text)), "état — text")


class PythonPath(unittest.TestCase):
    def test_rows_and_bad_rows(self):
        with tempfile.TemporaryDirectory() as d:
            bulk = write_bulk(d)
            r, out = load(d, bulk, "py", csv="python")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("2 unreadable rows skipped", r.stdout)
            rows = stored(out)
            self.assertEqual([x[0] for x in rows], [1, 2, 3, 4, 5, 8])
            self.assertEqual(rows[0][4:], ("html_with_citations", '<p>a "quoted" word</p>'))
            self.assertEqual(rows[1][5], "line one\nline two\r\nline three")
            self.assertEqual(rows[2][2:], ("", "", "xml_harvard", "<xml>back\\slash</xml>"))
            self.assertEqual(rows[3][3], "Bad � byte")
            self.assertIn("�", rows[3][5])
            self.assertEqual(rows[4][4:], (None, None))
            self.assertEqual(rows[5][1:], (None, "lead", "Ünïcødé", "html_anon_2020", "<p>état</p>"))

    def test_limit_leaves_the_main_database_alone(self):
        with tempfile.TemporaryDirectory() as d:
            bulk = write_bulk(d)
            r, out = load(d, bulk, "py", csv="python")
            before = sqlite3.connect(out / "courtlistener.sqlite").execute("SELECT * FROM loaded ORDER BY name").fetchall()
            r, _ = load(d, bulk, "py", "--limit", "2", csv="python")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            after = sqlite3.connect(out / "courtlistener.sqlite").execute("SELECT * FROM loaded ORDER BY name").fetchall()
            self.assertEqual(before, after)
            self.assertFalse((out / "courtlistener.sqlite.loading").exists())
            self.assertEqual(len(stored(out, "opinions-sample.sqlite")), 2)


@unittest.skipUnless(have_arrow(), "pyarrow and zstandard not installed")
class ArrowPath(unittest.TestCase):
    def test_same_rows_as_the_python_path(self):
        with tempfile.TemporaryDirectory() as d:
            bulk = write_bulk(d)
            rp, py = load(d, bulk, "py", csv="python")
            ra, ar = load(d, bulk, "arrow")
            self.assertEqual(ra.returncode, 0, ra.stdout + ra.stderr)
            self.assertIn("reading with pyarrow", ra.stdout)
            self.assertIn("2 unreadable rows skipped", ra.stdout)
            self.assertEqual(stored(ar), stored(py))
            blob = sqlite3.connect(ar / "opinions.sqlite").execute("SELECT text FROM opinions WHERE id=1").fetchone()[0]
            self.assertEqual(blob[:4], textcodec.ZSTD_MAGIC)
            n = lambda f: sqlite3.connect(f / "courtlistener.sqlite").execute("SELECT rows FROM loaded WHERE name='opinions'").fetchone()
            self.assertEqual(n(ar), n(py))


if __name__ == "__main__":
    unittest.main()
