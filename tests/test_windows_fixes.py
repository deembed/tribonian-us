#!/usr/bin/env python3
"""Fixes from running the 0.2.2 build on Windows: output that outlives its reader, the work folders of a fresh
install, the data mode after an interrupted swap, bitmap fonts in the LaTeX check, co-citation with no cap.

    python -m pytest tests/test_windows_fixes.py
"""
import errno, json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tribonian import cli, setup_guide                          # noqa: E402


class Gone:
    def __init__(self, err):
        self.err, self.written = err, []

    def write(self, t):
        raise self.err

    def flush(self):
        raise self.err


class QuietOutput(unittest.TestCase):
    def test_a_closed_pipe_stops_the_writing_not_the_work(self):
        for err in (BrokenPipeError(errno.EPIPE, "pipe"), OSError(errno.EINVAL, "Invalid argument")):   # Linux, Windows
            q = cli.QuietStream(Gone(err))
            self.assertEqual(q.write("law not local: § 1983\n"), 22)
            q.flush()
            self.assertEqual(q.write("more\n"), 5)

    def test_other_errors_still_raise(self):
        with self.assertRaises(OSError):
            cli.QuietStream(Gone(OSError(errno.ENOSPC, "No space left"))).write("x")


def env_for(d, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CL_", "TRIBONIAN_")) and k != "COURTLISTENER_TOKEN"}
    env.update(PYTHONPATH=str(ROOT / "src"), TRIBONIAN_CONFIG=str(Path(d) / "config.toml"), TRIBONIAN_WORK=str(Path(d) / "work"),
               PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", **extra)
    return env


def tribonian(d, *args, **extra):
    return subprocess.run([sys.executable, "-m", "tribonian", *args], env=env_for(d, **extra), capture_output=True, text=True)


class WorkFolders(unittest.TestCase):
    def test_made_on_start_except_bulk_and_db(self):
        with tempfile.TemporaryDirectory() as d:
            r = tribonian(d, "check", "--paths")
            self.assertEqual(r.returncode, 0, r.stderr)
            work = Path(d) / "work"
            for sub in cli.WORK_DIRS:
                self.assertTrue((work / sub).is_dir(), sub)
            # db/ decides where opinions.sqlite goes by default, so start-up must not make it
            self.assertFalse((work / "db").exists())
            self.assertFalse((work / "bulk").exists())


class InterruptedSwap(unittest.TestCase):
    def test_finishing_it_chooses_the_mode(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            bulk, nxt = work / "bulk", work / "bulk" / "next"
            nxt.mkdir(parents=True)
            (work / "db").mkdir()
            new = sqlite3.connect(nxt / "courtlistener.sqlite")
            new.execute("CREATE TABLE loaded(name TEXT PRIMARY KEY, file TEXT, rows INTEGER, seconds REAL, finished TEXT)")
            for n in setup_guide.BULK_FILES:
                (bulk / ("%s-2026-09-30.csv.bz2" % n)).write_bytes(b"")
                new.execute("INSERT INTO loaded VALUES (?, ?, 1, 0, '')", (n, "%s-2026-09-30.csv.bz2" % n))
            new.commit()
            new.close()
            (work / "db" / "next").mkdir()
            ops = sqlite3.connect(work / "db" / "next" / "opinions.sqlite")
            ops.execute("CREATE TABLE opinions(id INTEGER)")
            ops.commit()
            ops.close()                                  # Windows: an open file can't be swapped (the point of the fix)
            (nxt / "SWAPPING").write_text("swapping\n")
            Path(d, "config.toml").write_text('data_mode = "api"\ncourtlistener_token = "x"\n')
            r = tribonian(d, "setup", "update")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("finishing an interrupted swap", r.stdout)
            self.assertIn("already loaded", r.stdout)
            self.assertTrue((bulk / "courtlistener.sqlite").exists())
            self.assertFalse(nxt.exists())
            self.assertIn('data_mode = "full"', Path(d, "config.toml").read_text())      # none -> full: chosen


class Type3(unittest.TestCase):
    def test_counts_bitmap_fonts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "t.pdf")
            p.write_bytes(b"%PDF-1.5\n1 0 obj << /Type /Font /Subtype /Type1 >>\n2 0 obj << /Type/Font/Subtype/Type3 >>\n"
                          b"3 0 obj << /Type /Font /Subtype /Type3 /FontMatrix [] >>\n")
            self.assertEqual(setup_guide.type3_fonts(p), 2)

    def test_the_smoke_document_covers_typewriter_and_is_uncompressed(self):
        t = setup_guide.smoke_tex()
        self.assertIn("\\pdfobjcompresslevel=0", t)
        self.assertIn("\\textit{\\texttt{", t)


def have_scipy():
    try:
        import scipy  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_scipy(), "scipy not installed")
class NoCap(unittest.TestCase):
    def test_a_decision_citing_many_nodes_stays_in(self):
        import numpy as np
        from tribonian import cocite
        # 6 decisions citing nodes 0-2; decision 99 cites 200 nodes; every node cited 5+ times
        ci, cd = [], []
        for dec in range(6):
            for node in range(3):
                ci.append(dec), cd.append(node)
        for node in range(3, 203):                   # each node cited by 5 decisions citing only it
            for j in range(5):
                ci.append(1000 + node * 10 + j), cd.append(node)
        for node in range(200):
            ci.append(99), cd.append(node)
        ci, cd = np.array(ci), np.array(cd)
        p = dict(cocite.DEFAULTS)
        self.assertIsNone(p["max_cites"])
        _, _, N, skipped, *_ = cocite.slice_pairs(ci, cd, p)
        self.assertEqual(skipped, 0)
        p["max_cites"] = 150
        _, _, N150, skipped150, *_ = cocite.slice_pairs(ci, cd, p)
        self.assertEqual((N - N150, skipped150), (1, 1))         # the cap leaves decision 99 out


if __name__ == "__main__":
    unittest.main()
