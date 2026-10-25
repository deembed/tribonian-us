#!/usr/bin/env python3
"""Smoke tests: pure functions, plus one fixture build per generator when the inputs are present.

    python -m pytest                          # from the repo root
    python tests/test_smoke.py -v

No database, no network. The fixture builds need xml/usc01.xml and one cases/*.json (skipped
otherwise) and pdflatex (the PDF step is skipped without it). TRIBONIAN_WORK points the build at a
scratch work folder so nothing under the repo is written (the databases stay where they are).
"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tribonian import usc2md                                   # noqa: E402
from tribonian import case2md                                  # noqa: E402
from tribonian import casemap                                  # noqa: E402


class TexEscaping(unittest.TestCase):
    def test_specials(self):
        self.assertEqual(usc2md.tex_chars("a & b % c # d"), r"a \& b \% c \# d")

    def test_nothing_passes_silently(self):
        out = usc2md.tex_chars("x⁢y")           # invisible times: not in any table
        self.assertNotIn("⁢", out)


class Markdown(unittest.TestCase):
    def test_pipe_is_escaped_for_wikilink_labels(self):
        self.assertEqual(case2md.esc("a|b", "md"), r"a\|b")

    def test_bracket_is_not_escaped(self):
        self.assertEqual(case2md.esc("[N. C.]", "md"), "[N. C.]")

    def test_yaml_head_quotes_and_escapes(self):
        lines = case2md.yaml_head([("title", 'He said "hi" \\ back'), ("aliases", ["x"]), ("n", 3),
                                   ("d", case2md.Raw("2026-10-04")), ("skip", None), ("flag", True)])
        self.assertEqual(lines, ["---", 'title: "He said \\"hi\\" \\\\ back"', "aliases:", '  - "x"',
                                 "n: 3", "d: 2026-10-04", "flag: true", "---"])


class Anchors(unittest.TestCase):
    def test_anchor_is_tex_safe_and_unique(self):
        case2md._anchors.clear()
        self.assertEqual(case2md.anchor("I-A & B %x"), "I-A-B-x")
        self.assertEqual(case2md.anchor("I-A & B %x"), "I-A-B-x--2")
        self.assertEqual(case2md.anchor("§ 2"), "2")
        self.assertEqual(case2md.anchor("¶"), "x")


class Sampling(unittest.TestCase):
    def test_cochran_with_fpc(self):
        n = casemap.sample_size(8450, 0.05)
        self.assertTrue(350 <= n <= 385, n)             # the State Farm run reported 368
        self.assertEqual(casemap.sample_size(100, 0.05), 80)   # 384 / (1 + 383/100), rounded up

    def test_moe_zero_when_everything_read(self):
        self.assertEqual(casemap.moe(10, 50, 50), 0)
        self.assertGreater(casemap.moe(25, 50, 5000), 0.1)


class IndexMerge(unittest.TestCase):
    def test_other_process_entries_survive(self):
        with tempfile.TemporaryDirectory() as d:
            old = case2md.CASES
            case2md.CASES = Path(d)
            try:
                idx = case2md.load_index()
                disk = {"1": {"slug": "other"}}
                (Path(d) / "_index.json").write_text(json.dumps(disk))
                idx["2"] = {"slug": "mine"}
                case2md.save_index(idx)
                final = json.loads((Path(d) / "_index.json").read_text())
                self.assertEqual(set(final), {"1", "2"})
                with case2md.index_lock():
                    with case2md.index_lock():          # re-entrant
                        case2md.save_index(idx)
            finally:
                case2md.CASES = old


@unittest.skipUnless((ROOT / "xml" / "usc01.xml").exists(), "needs xml/usc01.xml")
class UscBuild(unittest.TestCase):
    def test_title1_chapter1(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, "-m", "tribonian", "usc", str(ROOT / "xml" / "usc01.xml"),
                                "--chapter", "/us/usc/t1/ch1", "--out", d], capture_output=True, text=True,
                               env=dict(os.environ, PYTHONPATH=str(ROOT / "src")))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            tex = Path(d) / "1-usc-ch1.tex"
            self.assertTrue(tex.exists())
            self.assertTrue(list(Path(d).rglob("1-usc-1.md")))
            if shutil.which("pdflatex"):
                for _ in range(2):
                    p = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                                        "-output-directory", d, str(tex)], capture_output=True, text=True,
                                       env=dict(os.environ, SOURCE_DATE_EPOCH="0"), timeout=600)
                self.assertEqual(p.returncode, 0, p.stdout[-2000:])
                self.assertIn(r"\pathmark{", (Path(d) / "1-usc-ch1.aux").read_text())


def _a_case():
    hits = sorted(p for p in (ROOT / "cases").glob("*.json") if not p.name.startswith("_"))
    return hits[0] if hits else None


@unittest.skipUnless(_a_case(), "needs one cases/*.json")
class CaseBuild(unittest.TestCase):
    def test_build_reproducible(self):
        src = _a_case()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "cases").mkdir()
            shutil.copy(src, Path(d) / "cases" / src.name)
            from tribonian import casedb          # the checkout's databases, passed on explicitly
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), TRIBONIAN_WORK=d, CL_BULK=str(casedb.BULK),
                       CL_DB=str(casedb.DB), CL_OPS_DB=str(casedb.OPS_DB), CL_SEARCH_DB=str(casedb.SEARCH_DB))
            cmd = [sys.executable, "-m", "tribonian", "case", "build", str(Path(d) / "cases" / src.name)]
            if not shutil.which("pdflatex"):
                cmd.append("--no-pdf")
            r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1200)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(list((Path(d) / "md-cases").rglob("*.md")))
            if shutil.which("pdflatex"):
                pdfs = list((Path(d) / "pdf").rglob("*.pdf"))
                self.assertEqual(len(pdfs), 1)
                first = pdfs[0].read_bytes()
                subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1200)
                self.assertEqual(pdfs[0].read_bytes(), first, "rebuild is not byte-identical")


if __name__ == "__main__":
    unittest.main()
