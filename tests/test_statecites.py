#!/usr/bin/env python3
"""State law cites (statecites.py, the formulas' [cite] tables) and the shared appendix pieces.

    python -m pytest tests/test_statecites.py

No network. The text checks use what is built under law/ when it is there and are skipped otherwise.
"""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tribonian import lawcites, lawtext, statecites, statelaw    # noqa: E402


def cites(text, states=()):
    statecites.CONTEXT["states"] = list(states)
    try:
        return lawcites.find(text)
    finally:
        statecites.CONTEXT["states"] = []


class Tables(unittest.TestCase):
    def test_every_jurisdiction_has_a_table(self):
        sts = {t.st for t in statecites.tables()}
        self.assertEqual(len(sts), 51, sorted(set(statecites.STATES) - sts))

    def test_examples_read_back(self):
        for tb in statecites.tables():
            for ex in tb.c.get("examples") or []:
                with self.subTest(table=tb.prefix, cite=ex[0]):
                    self.assertIn(ex[1], cites(ex[0], [tb.st])["cites"])

    def test_constitution_does_not_inherit_statute_cites(self):
        f = statelaw.formulas()[("TX", "const")]
        self.assertNotIn("Code", " ".join(f.get("cite", {}).get("full", [])))


class Reading(unittest.TestCase):
    def test_colorado_keys_unchanged(self):
        r = cites("Colo. Rev. Stat. § 26.5-4-205(2) and Colo. Const. art. IX, § 7; 8 CCR 1404-1 § 4.109; § 26.5-4-206.")
        self.assertEqual(set(r["cites"]), {"co-crs:26.5-4-205", "co-const:art-9:7", "co-ccr:8-1404-1:4.109", "co-crs:26.5-4-206"})

    def test_edition_is_not_a_subsection(self):
        r = cites("Colo. Rev. Stat. § 26.5-4-205(2) (2023)")
        self.assertEqual(r["editions"], {"co-crs:26.5-4-205": ("", "2023")})

    def test_bare_form_needs_the_state(self):
        self.assertNotIn("ca-code:PEN/187", cites("Penal Code § 187 applies.", ["TX"])["cites"])
        self.assertIn("ca-code:PEN/187", cites("Pen. Code, § 187 applies.", ["CA"])["cites"])

    def test_bare_form_not_taken_after_another_states_name(self):
        r = cites("Cal. Penal Code § 187; Tex. Penal Code Ann. § 19.02.", ["TX"])
        self.assertIn("ca-code:PEN/187", r["cites"])
        self.assertIn("tx-codes:PE/19.02", r["cites"])

    def test_usc_short_form_wins_near_its_title(self):
        r = cites("42 U.S.C. § 2000bb; Mass. Gen. Laws ch. 151B, § 4; see § 2000bb-1.")
        self.assertIn("usc:42:2000bb-1", r["cites"])

    def test_source_error(self):
        r = cites("43 P.S. § 951")
        self.assertIn("pa-ps:43/951", r["cites"])
        self.assertTrue(lawtext.fetch_hint("pa-ps:43/951").startswith("source error"))
        self.assertIsNone(lawtext.fetch_spec("pa-ps:43/951"))

    def test_section_of_and_note_names(self):
        self.assertEqual(lawcites.section_of("co-const:art-9:7"), "co-const:art-9")
        self.assertEqual(lawcites.section_of("co-ccr:8-1404-1:4.109"), "co-ccr:8-1404-1")
        self.assertEqual(lawcites.section_of("la-rs:14:30"), "la-rs:14:30")       # a title:section statute is no pin
        self.assertNotIn(":", lawcites.note_name("nj-statutes:2A:14-1"))

    def test_fetch_spec(self):
        self.assertEqual(lawtext.fetch_spec("co-crs:26.5-4-205"), ("state", "CO", "crs", "26.5"))
        self.assertEqual(lawtext.fetch_spec("tx-codes:GV/311.001"), ("state", "TX", "codes", "GV.311"))


@unittest.skipUnless((statelaw.state_dir("CO") / "uslm" / "crs" / "title-26.5.xml").exists(), "C.R.S. title 26.5 not built")
class Text(unittest.TestCase):
    def test_statute(self):
        head, rows = lawtext.text_of("co-crs:26.5-4-205")
        self.assertIn("Quality standards", head)
        self.assertTrue(rows[0][1].startswith("(1)"))

    def test_rule_section(self):
        got = lawtext.text_of("co-ccr:8-1404-1:4.109")
        if got is None:
            self.skipTest("8 CCR 1404-1 not built")
        self.assertIn("PROVIDER MATCHING", got[0])


if __name__ == "__main__":
    unittest.main()
