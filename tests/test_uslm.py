#!/usr/bin/env python3
"""uslm: the numbering ladder, the html-paragraphs family, and that verify catches what it claims to, offline.

    python -m pytest tests/test_uslm.py
"""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tribonian.uslm import build, tree                              # noqa: E402
from tribonian.uslm.families import html_paragraphs                 # noqa: E402

FORMULA = {
    "name": "Test Code", "doc": "/us-xx/tc", "family": "html-paragraphs",
    "ladder": {"types": ["num", "lower", ["roman_upper", "roman_lower"], "upper"],
               "tags": ["subsection", "paragraph", "subparagraph", "clause"]},
    "containers": {"title": {"match": r"^TITLE\s+(?P<num>\d+)$", "tag": "title", "rank": 1, "id": "t{num}"},
                   "article": {"match": r"^ARTICLE\s+(?P<num>\d+)$", "tag": "article", "rank": 2, "id": "a{num}"}},
    "sections": {"match": r"^(?P<num>\d+-\d+-\d+)\.\s*(?P<heading>.*)$", "bold": True, "scope": "title",
                 "versions": r"^\[Editor's note: This version", "toc": [r"^\d+-\d+-\d+\.\s"]},
    "notes": {"annotation": "^ANNOTATION$", "sourceCredit": "^Source:", "editorialNote": ["^Editor's note"]},
}

C = '<p align=center style="text-align:center">%s</p>'
B = '<p><b>%s</b> %s</p>'
P = '<p>%s</p>'


def page(*ps):
    return ("<html><body>" + "\n".join(ps) + "</body></html>").encode()


SAMPLE = page(
    C % "<b>TITLE 7</b>", C % "<b>TESTS</b>", C % "<b>ARTICLE 1</b>", C % "General",
    P % "7-1-101. Short title.",                                     # table of contents line
    B % ("7-1-101. Definitions.", "As used in this article:"),
    P % "(1) &quot;Alpha&quot; means:", P % "(a) one;", P % "(b) two:", P % "(I) roman one;", P % "(II) roman two;",
    P % "(c) three;", P % "(d) four;", P % "(e) five;", P % "(f) six;", P % "(g) seven;", P % "(h) eight;",
    P % "(i) the letter i, not roman one.",
    P % "(2) [Editor's note: This version is effective until July 1, 2027.] Old text.",
    P % "(2) [Editor's note: This version is effective July 1, 2027.] New text.",
    P % "(3) (a) Both labels in one paragraph.",
    P % "Closing words of subsection (3).",
    P % "<b>Source:</b> L. 2026: Entire article added.",
    C % "ANNOTATION", P % "<b>Annotated.</b> Some case. 1 P.3d 1.", C % "II. Sub heading inside the annotation.",
    B % ("7-1-102. Starts at a letter.", ""), P % "(a) First.", P % "(b) Second.",
)


class Ladder(unittest.TestCase):
    def test_ordinals(self):
        self.assertEqual(tree.ordinal("num", "1.5"), (1, 5))
        self.assertEqual(tree.ordinal("lower", "aa"), (27, 0))
        self.assertEqual(tree.ordinal("roman_upper", "IV"), (4, 0))
        self.assertIsNone(tree.ordinal("roman_upper", "IIII"))
        self.assertIsNone(tree.ordinal("lower", "the"))

    def test_choices(self):
        L = tree.Ladder(FORMULA["ladder"]["types"])
        self.assertEqual(L.place("I", [(1, 0), (8, 0), None, (8, 0)], "x;"), 3)     # (I) after (H): a letter
        self.assertEqual(L.place("I", [(1, 0), (1, 0)], "text;"), 2)                 # (I) under (a): roman one
        self.assertEqual(L.place("i", [(1, 0), (8, 0)], "text;"), 1)                 # (i) after (h): a letter
        self.assertEqual(L.place("i", [(1, 0), (8, 0)], "the following:"), 2)        # announced: roman child
        self.assertEqual(L.place("a", [], ""), 1)                                    # a section may start at (a)
        self.assertIsNone(L.place("the", [(1, 0)], ""))


class Family(unittest.TestCase):
    def setUp(self):
        self.tree, self.r = build.convert(SAMPLE, FORMULA, "t7", {"edition": "test"})
        self.ids = {el.get("identifier") for el in self.tree.getroot().iter() if isinstance(el.tag, str)}

    def test_passes(self):
        self.assertTrue(self.r["conservation"]["ok"], self.r["conservation"])
        self.assertTrue(self.r["schema"]["ok"], self.r["schema"]["errors"])
        self.assertEqual(self.r["unmapped"]["count"], 0)
        self.assertEqual(self.r["anomalies"], [])
        self.assertEqual(self.r["numbering"], [])

    def test_structure(self):
        s = "/us-xx/tc/t7/s7-1-101"
        for i in ("/1/a", "/1/b/I", "/1/b/II", "/1/i", "/2", "/2_v2", "/3/a"):
            self.assertIn(s + i, self.ids)
        self.assertNotIn(s + "/1/h/i", self.ids)
        self.assertIn("/us-xx/tc/t7/s7-1-102/a", self.ids)
        self.assertEqual(len(self.r["versions"]), 1)
        self.assertEqual(self.r["counts"]["sourceCredit"], 1)

    def test_conservation_catches_a_drop(self):
        feed = html_paragraphs.Builder.feed

        def lossy(self, text, bold, centered):
            if text != "(d) four;":
                feed(self, text, bold, centered)
        html_paragraphs.Builder.feed = lossy
        try:
            _, r = build.convert(SAMPLE, FORMULA, "t7", {})
        finally:
            html_paragraphs.Builder.feed = feed
        self.assertFalse(r["conservation"]["ok"])
        self.assertFalse(r["ok"])

    def test_numbering_catches_disorder(self):
        bad = SAMPLE.replace(b"(e) five;", b"(c) five again;")
        _, r = build.convert(bad, FORMULA, "t7", {})
        self.assertTrue(r["anomalies"] or r["numbering"])


if __name__ == "__main__":
    unittest.main()
