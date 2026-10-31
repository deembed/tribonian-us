#!/usr/bin/env python3
"""citefind.py: the patterns engine (no dependency), Id./supra antecedents, and the eyecite engine when installed.

    python -m pytest tests/test_citefind.py
"""
import sqlite3, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tribonian import citefind as F                            # noqa: E402


def found(text):
    return [(c.kind, c.vol, c.rep, c.page) for c in F.find(text, "patterns")]


class Patterns(unittest.TestCase):
    def test_full_and_pin(self):
        for text, want in [
            ("Employment Div. v. Smith, 494 U. S. 872 (1990)", [("full", "494", "U.S.", "872")]),
            ("Hobbie, 480 U. S., at 145", [("pin", "480", "U.S.", "145")]),            # comma before "at"
            ("Bruen, 142 S. Ct. at6 2132", [("pin", "142", "S. Ct.", "2132")]),         # stray mark on "at"
            ("10 Cal.Rptr.3d at p. 296", [("pin", "10", "Cal. Rptr. 3d", "296")]),      # California "at p."
            ("2022 WL 18145414", [("full", "2022", "WL", "18145414")]),                 # 8-digit Westlaw number
            ("455 F. Appx. 795", [("full", "455", "F. App'x", "795")]),
            ("109 Cal. App. 5th 204", [("full", "109", "Cal. App. 5th", "204")]),
            ("89 OhioSt.3d 62", [("full", "89", "Ohio St. 3d", "62")]),
            ("251 App Div 132", [("full", "251", "A.D.", "132")]),                      # New York, no periods
            ("5 U.S. (1 Cranch) 137", [("full", "5", "U.S.", "137")]),
            ("1 Cranch 137", [("full", "1", "Cranch", "137")]),
            ("10 Peters, 402", [("full", "10", "Pet.", "402")]),                        # old comma style
            ("7 Howard, 624", [("full", "7", "How.", "624")]),
        ]:
            self.assertEqual(found(text), want, text)

    def test_not_cites(self):
        for text in ["in 2004 Idaho 12 ways", "In 1990 U.S., 45 states", "Justice Howard, 5 times",
                     "4 App. 52a, 57a", "Rule 12 of", "Id. § 3", "Robinson, supra note 32, at 34", "see pp. 9-10, supra"]:
            self.assertEqual(found(text), [], text)

    def test_id_and_supra(self):
        self.assertEqual(found("Id., at 843"), [("id", "", "", "843")])
        self.assertEqual(found("Ibid.; see"), [("id", "", "", "")])
        c = F.find("Smith, supra, at 890", "patterns")[0]
        self.assertEqual((c.kind, c.name, c.page), ("supra", "Smith", "890"))


class Antecedents(unittest.TestCase):
    def test_chain(self):
        d = F.Antecedents()
        self.assertIsNone(d.id())                                  # nothing before
        d.cite(7, before="Employment Div. v. Smith, ")
        self.assertEqual(d.id(), 7)
        self.assertEqual(d.supra("Smith"), 7)
        d.law()                                                    # a statute between: its Id., not the case's
        self.assertIsNone(d.id())
        d.cite(9, before="Smith v. Jones, ")
        self.assertIsNone(d.supra("Smith"))                        # two Smiths: no guess


class Checks(unittest.TestCase):
    """A cite found by its citation is checked against the name written before it and its pin page."""

    def setUp(self):
        from tribonian import scotus
        self.S = scotus
        scotus._NAMES.clear(), scotus._STARTS.clear()
        self.con = sqlite3.connect(":memory:")
        self.con.executescript("CREATE TABLE clusters(id, case_name, case_name_short, case_name_full);"
                               "CREATE TABLE cites(cluster_id, volume, reporter, page);")
        self.con.executemany("INSERT INTO clusters VALUES(?,?,?,?)", [
            (1, "Employment Div., Dept. of Human Resources of Ore. v. Smith", "Smith",
             "Employment Division, Department of Human Resources of Oregon v. Smith"),
            (2, "Brown v. Board of Education", "Brown", ""), (3, "Next Case v. Somebody", "", "")])
        self.con.executemany("INSERT INTO cites VALUES(?,?,?,?)",
                             [(1, "494", "U.S.", "872"), (3, "494", "U.S.", "891"), (2, "347", "U.S.", "483")])

    def test_name(self):
        nm = lambda cid, vol, page, before: self.S.name_mismatch(self.con, cid, vol, "U.S.", page, before)
        self.assertEqual(nm(1, "494", "872", "Employment Div. v. Smith, "), "")
        self.assertEqual(nm(2, "347", "483", "See Brown v. Bd. of Educ., "), "")           # abbreviations
        self.assertIn("likely miscite", nm(1, "494", "872", "Smith v. Jones, "))           # one party right is not enough
        self.assertEqual(nm(1, "494", "872", "(1990); see also "), "")                     # no name of its own: not checked

    def test_pin(self):
        po = lambda pin, start=None: self.S.pin_outside(self.con, 1, "494", "U.S.", pin, start=start)
        self.assertEqual(po("890"), "")
        self.assertIn("past the case", po("893"))                                         # the next case starts at 891
        self.assertIn("before the case", po("850", start="872"))


@unittest.skipUnless(__import__("importlib").util.find_spec("eyecite"), "eyecite not installed")
class Eyecite(unittest.TestCase):
    def test_same_cites(self):
        text = "Employment Div. v. Smith, 494 U. S. 872 (1990). Id., at 890. Smith, 494 U. S., at 879."
        self.assertEqual([c.kind for c in F.find(text, "eyecite")], ["full", "id", "pin"])
        self.assertEqual([c.kind for c in F.find(text, "both")], ["full", "id", "pin"])


if __name__ == "__main__":
    unittest.main()
