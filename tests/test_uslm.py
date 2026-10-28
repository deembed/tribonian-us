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
        self.assertEqual(L.place("I", ["1", "h", None, "H"], "x;"), 3)     # (I) after (H): a letter
        self.assertEqual(L.place("I", ["1", "a"], "text;"), 2)                 # (I) under (a): roman one
        self.assertEqual(L.place("i", ["1", "h"], "text;"), 1)                 # (i) after (h): a letter
        self.assertEqual(L.place("i", ["1", "h"], "the following:"), 2)        # announced: roman child
        self.assertEqual(L.place("a", [], ""), 1)                                    # a section may start at (a)
        self.assertIsNone(L.place("the", ["1"], ""))


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

        def lossy(self, text, *rest):
            if text != "(d) four;":
                feed(self, text, *rest)
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


CT = {
    "name": "Test CT", "doc": "/us-xx/ct", "family": "html-paragraphs",
    "html": {"blocks": ["p"], "headings": ["h2", "h3"], "opening": ["span.catchln"], "verbatim": ["table"],
             "drop": ["table.nav_tbl", "h4:noclass"]},
    "ladder": {"types": ["lower", "num", "upper"], "tags": ["subsection", "paragraph", "subparagraph"]},
    "containers": {"chapter": {"match": r"^CHAPTER\s+(?P<num>\d+)\*?$", "tag": "chapter", "rank": 2, "id": "ch{num}"}},
    "sections": {"match": r"^Secs?\.\s+(?P<num>\d+-\d+[a-z]*(?:\s+to\s+\d+-\d+[a-z]*)?)\.\s*(?P<heading>.*)$",
                 "bold": True, "scope": "title"},
    "notes": {"sourceCredit": {"class": "source-first"}, "note": [{"class": "toc_catchln", "topic": "toc"}]},
}
NAV = '<table class="nav_tbl"><tr><td>(Return to List of Titles)</td></tr></table>'


def ct_page(*body):
    return page('<h2 class="chap-no">CHAPTER 9*</h2>', '<h2 class="chap-name">TESTS</h2>', "<h4>Note: see supplement</h4>",
                '<p class="toc_catchln">Sec. 9-1. One.</p>', *body, NAV)


class Layers(unittest.TestCase):
    def test_overlay_and_drops(self):
        base = ct_page('<p><span class="catchln">Sec. 9-1. One.</span> (a) Old text.</p>', '<p>(b) More.</p>',
                       '<p class="source-first">(P.A. 1.)</p>',
                       '<p><span class="catchln">Secs. 9-2 to 9-4.</span> Reserved.</p>',
                       '<p><span class="catchln">Sec. 9-5. Five.</span> A form follows.</p>',
                       '<table><tr><td>(1)</td><td>Name: ____</td></tr></table>')
        sup = ct_page('<p><span class="catchln">Sec. 9-1. One.</span> (a) New text.</p>',
                      '<p><span class="catchln">Sec. 9-4a. Added.</span> Brand new.</p>')
        tree, r = build.convert_layers([("base", [("c9", base)]), ("sup", [("c9", sup)])], CT, "/us-xx/ct/t9", "TITLE 9", "TESTS", {})
        self.assertTrue(r["ok"], (r["conservation"], r["schema"]["errors"]))
        self.assertEqual(r["layers"]["sup"]["replaced"], ["/us-xx/ct/t9/s9-1"])
        self.assertEqual(r["layers"]["sup"]["added"], ["/us-xx/ct/t9/s9-4a"])
        nums = [el.findtext("{*}num") for el in tree.getroot().iter("{*}section")]
        self.assertEqual(nums, ["Sec. 9-1.", "Secs. 9-2 to 9-4.", "Sec. 9-4a.", "Sec. 9-5."])
        text = " ".join(tree.getroot().itertext())
        self.assertIn("New text", text)
        self.assertNotIn("Old text", text)
        self.assertNotIn("Return to", text)
        self.assertGreater(r["dropped"]["table.nav_tbl"], 0)
        self.assertEqual(r["anomalies"], [])            # the (1) in the form table is not read as a label

    def test_unlisted_markup_fails_conservation(self):
        bad = ct_page('<p><span class="catchln">Sec. 9-1. One.</span> Text.</p>', "<div>Loose text in a div.</div>")
        _, r = build.convert_layers([("base", [("c9", bad)])], CT, "/us-xx/ct/t9", "", "", {})
        self.assertFalse(r["conservation"]["ok"])


if __name__ == "__main__":
    unittest.main()
