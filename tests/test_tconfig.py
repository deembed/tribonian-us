#!/usr/bin/env python3
"""tconfig: the TOML subset reader, and variable > config file > default, offline.

    python -m pytest tests/test_tconfig.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tribonian import tconfig                                  # noqa: E402

SAMPLE = '''# comment
bulk_dir = "~/bulk"     # trailing comment
main_db = 'db/x.sqlite'
api_limits = "10,75,300"
[gui]
default_view = "v # not a comment"
n = 3
on = true
'''


def paths(env):
    """tribonian check --paths in a clean environment plus env."""
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("CL_") or k in ("TRIBONIAN_CONFIG", "XDG_CONFIG_HOME", "TRIBONIAN_WORK"))}
    out = subprocess.run([sys.executable, "-m", "tribonian", "check", "--paths"], capture_output=True, text=True,
                         cwd=ROOT, env=dict(base, PYTHONPATH=str(ROOT / "src"), **env))
    return json.loads(out.stdout), out.stderr


class Parse(unittest.TestCase):
    def test_subset(self):
        d = tconfig.parse(SAMPLE)
        self.assertEqual(d["bulk_dir"], "~/bulk")
        self.assertEqual(d["main_db"], "db/x.sqlite")
        self.assertEqual(d["gui"], {"default_view": "v # not a comment", "n": 3, "on": True})

    def test_same_as_tomllib(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("Python < 3.11")
        self.assertEqual(tconfig.parse(SAMPLE), tomllib.loads(SAMPLE))

    def test_bad_line(self):
        with self.assertRaises(ValueError):
            tconfig.parse("main_db =\n")


class Precedence(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cfg = self.d / "config.toml"
        self.cfg.write_text('main_db = "%s"\nsearch_db = "db/s.sqlite"\n[gui]\ndefault_view = "v"\n' % (self.d / "file.sqlite"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_file_beats_default_and_relative_is_work(self):
        p, _ = paths({"TRIBONIAN_CONFIG": str(self.cfg)})
        self.assertEqual(p["main_db"], str(self.d / "file.sqlite"))
        self.assertEqual(p["work_dir"], str(ROOT))                      # a checkout works in itself
        self.assertEqual(p["search_db"], str(ROOT / "db" / "s.sqlite"))
        self.assertEqual(p["config_file"], str(self.cfg))
        self.assertEqual(p["gui"], {"default_view": "v"})

    def test_work_dir_from_file_and_variable(self):
        work = self.d / "work"
        self.cfg.write_text('work_dir = "%s"\nsearch_db = "db/s.sqlite"\n' % work)
        p, _ = paths({"TRIBONIAN_CONFIG": str(self.cfg)})
        self.assertEqual(p["work_dir"], str(work))
        self.assertEqual(p["search_db"], str(work / "db" / "s.sqlite"))
        self.assertEqual(p["main_db"], str(work / "bulk" / "courtlistener.sqlite"))
        p, _ = paths({"TRIBONIAN_CONFIG": str(self.cfg), "TRIBONIAN_WORK": str(self.d / "other")})
        self.assertEqual(p["work_dir"], str(self.d / "other"))

    def test_variable_beats_file(self):
        p, _ = paths({"TRIBONIAN_CONFIG": str(self.cfg), "CL_DB": str(self.d / "env.sqlite")})
        self.assertEqual(p["main_db"], str(self.d / "env.sqlite"))

    def test_xdg_location(self):
        (self.d / "tribonian-us").mkdir()
        shutil.copy(self.cfg, self.d / "tribonian-us" / "config.toml")
        p, _ = paths({"XDG_CONFIG_HOME": str(self.d)})
        self.assertEqual(p["config_file"], str(self.d / "tribonian-us" / "config.toml"))

    def test_no_file_is_default(self):
        work = self.d / "work"
        p, _ = paths({"XDG_CONFIG_HOME": str(self.d / "none"), "TRIBONIAN_WORK": str(work)})
        self.assertIsNone(p["config_file"])
        self.assertEqual(p["opinions_db"], str(work / "bulk" / "opinions.sqlite"))   # no db/: beside main_db
        (work / "db").mkdir(parents=True)
        p, _ = paths({"XDG_CONFIG_HOME": str(self.d / "none"), "TRIBONIAN_WORK": str(work)})
        self.assertEqual(p["opinions_db"], str(work / "db" / "opinions.sqlite"))

    def test_broken_file_is_ignored_and_reported(self):
        self.cfg.write_text("main_db =\n")
        p, err = paths({"TRIBONIAN_CONFIG": str(self.cfg)})
        self.assertIn("line 1", p["config_error"])
        self.assertIn("ignoring the config file", err)


if __name__ == "__main__":
    unittest.main()
