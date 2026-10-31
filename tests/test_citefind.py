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


def standin():
    """A tiny database: Smith (494 U.S. 872), Smith v. Jones (494 U.S. 900), Doe v. Roe (no U.S. cite), Brown."""
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE clusters(id, case_name, case_name_short, case_name_full, citation_count, docket_id, date_filed);"
                      "CREATE TABLE dockets(id, court_id, docket_number, case_name, date_filed);"
                      "CREATE TABLE cites(cluster_id, volume, reporter, page); CREATE TABLE courts(id, citation_string);"
                      "CREATE INDEX clusters_by_date ON clusters(date_filed);")
    con.executemany("INSERT INTO clusters VALUES(?,?,?,?,?,?,?)", [
        (1, "Employment Div. v. Smith", "Smith", "", 5000, 1, "1990-04-17"), (4, "Smith v. Jones", "", "", 10, 4, "1990-05-01"),
        (5, "Doe v. Roe", "", "", 3, 5, "1990-06-01"), (2, "Brown v. Board of Education", "Brown", "", 9000, 2, "1954-05-17")])
    con.executemany("INSERT INTO dockets VALUES(?,?,?,?,?)", [(i, "scotus", "", "", "") for i in (1, 2, 4, 5)])
    con.executemany("INSERT INTO cites VALUES(?,?,?,?)", [(1, "494", "U.S.", "872"), (4, "494", "U.S.", "900"),
                                                          (1, "110", "S. Ct.", "1595"), (2, "347", "U.S.", "483")])
    return con


class Marking(unittest.TestCase):
    """mark_cases on text and on CourtListener's citation spans, against the stand-in database."""

    def setUp(self):
        from tribonian import case2md, scotus
        self.C = case2md
        for cache in (scotus._NAMES, scotus._STARTS, scotus._NAMED, scotus._RESOLVED, scotus.FOUND):
            cache.clear()
        con = standin()
        case2md.shared_db = lambda: con
        F.DOC.reset(["347 U.S. 483"], 2)                          # the document is Brown itself

    def marks(self, kids):
        p = self.C.El("p")
        p.kids = kids
        self.C.mark_cases(p)
        return [(k.tag, k.text(), {a: v for a, v in k.attrs.items() if a in ("ok", "why", "cm_ok", "cm_why", "cm_plain")})
                for k in p.kids if not isinstance(k, str)]

    def test_suggestion(self):
        got = self.marks(["Smith v. Jones, 494 U.S. 872 (1990). Doe v. Roe, 494 U.S. 872 (1990)."])
        self.assertIn("Smith v. Jones is at 494 U.S. 900", got[0][2]["why"])
        self.assertIn("Doe v. Roe has no U.S. cite", got[1][2]["why"])

    def test_self_cite(self):
        self.assertEqual(self.marks(["Brown, 347 U. S. 483."])[0][2], {"ok": "1"})

    def test_unlinked_spans(self):
        def mk(t):
            e = self.C.El("span", {"class": "citation no-link"})
            e.kids.append(t)
            return e
        got = self.marks(["Employment Div. v. Smith, ", mk("494 U.S. 872"), " (1990). ", mk("Id., at 879"), ". See ",
                          mk("90 Stat. 331"), ". ", mk("Id., at 2"), "."])
        self.assertEqual([g[2] for g in got], [{"cm_ok": "1"}, {"cm_ok": "1"}, {"cm_plain": "1"}, {"cm_plain": "1"}])

    def test_coupling_counts(self):
        from tribonian import scotus
        got, miss = scotus.authorities(standin(), "Employment Div. v. Smith, 494 U.S. 872, 110 S. Ct. 1595 (1990). Id. at 879. Id. "
                                       "Brown v. Board of Education, 347 U.S. 483 (1954). Id. at 495. See 42 U.S.C. § 2000bb. Id. "
                                       "Smith, supra, at 885. Smith, 494 U.S. at 890.")
        self.assertEqual(dict(got), {1: 5, 2: 2})              # full, Id., Id., supra, pin; full, Id. (not the statute's)


class LawLists(unittest.TestCase):
    def test_lists(self):
        from tribonian import lawcites
        keys = lambda t: [k for a, b, k in lawcites.find(t)["spans"]]
        self.assertEqual(keys("42 U.S.C. § 1396a. Title XIX. §§ 1396a(a)(10), 1396d(a)(12). Along"),
                         ["usc:42:1396a", "usc:42:1396a", "usc:42:1396d"])
        self.assertEqual(keys("42 C.F.R. §§ 440.210, .220. Mandatory"), ["cfr:42:440.210", "cfr:42:440.220"])

    def test_short_form_after_an_element(self):
        """The full cite sits in a CourtListener span; the short form after it is still read (paragraph context)."""
        from tribonian import case2md as C
        p = C.parse_html('<p><span class="citation no-link">42 U.S.C. § 1396</span>. The program. § 1396a(10)(A)(i); more.</p>')
        C.mark_law(p)
        laws, stack = [], [p]
        while stack:
            n = stack.pop()
            if isinstance(n, C.El):
                if n.tag == "law":
                    laws.append((n.attrs["key"], n.text()))
                stack += n.kids
        self.assertIn(("usc:42:1396a", "§ 1396a(10)(A)(i)"), laws)


class OpinionHtml(unittest.TestCase):
    def test_footnote_form(self):
        from tribonian import case2md as C
        o = C.read_opinion({"html_with_citations": '<p>Text.<a class="footnote" href="#fn1" id="fn1_ref">1</a> More.</p>'
                            '<div class="footnotes"><div class="footnote" id="fn1" label="1"><a class="footnote" href="#fn1_ref">1</a>'
                            '<p>. The note.</p></div></div>'}, 0)
        self.assertEqual(list(o["footnotes"]), ["1"])
        self.assertEqual(" ".join(o["footnotes"]["1"].text().split()), "The note.")
        self.assertIsNotNone(o["flat"][0][1].find("footnotemark"))

    def test_italics_split(self):
        from tribonian import case2md as C
        em = C.El("em")
        em.kids = ["Olmstead v. Zimring, 521"]
        p = C.El("p")
        p.kids = [em, " U.S. 581, 601."]
        C.unsplit(p)
        self.assertEqual((em.kids[0], p.kids[1]), ("Olmstead v. Zimring, ", "521 U.S. 581, 601."))


class SeparateOpinions(unittest.TestCase):
    """Separate opinions kept inside the majority's text come apart, each with its own footnotes."""

    def test_split(self):
        from tribonian import case2md as C
        html = ('<opinion type="majority"><p>Justice KAGAN delivered the opinion of the Court.</p>'
                '<p>Majority text.<footnotemark>1</footnotemark></p>'
                '<p>Justice GORSUCH, with whom Justice THOMAS joins, with whom Justice ALITO joins as to Parts I and II, '
                'concurring in the judgment.</p><p>Concurring text.<footnotemark>1</footnotemark></p>'
                '<p>Justice Thomas, in his dissent, argues otherwise.</p>'
                '<p>Chief Justice ROBERTS, dissenting.</p><p>Dissent.</p>'
                '<footnote label="1"><p>Majority note.</p></footnote><footnote label="1"><p>Gorsuch note.</p></footnote></opinion>')
        parts = C.split_separate(C.read_opinion({"html_with_citations": html, "author_str": ""}, 0))
        got = [(C.short_author(p["author"], p["api"].get("author_str")), p["kind"],
                " ".join(p["footnotes"]["1"].text().split()) if "1" in p["footnotes"] else "") for p in parts]
        self.assertEqual(got, [("Kagan, J.", "majority", "Majority note."), ("Gorsuch, J.", "concurrence", "Gorsuch note."),
                               ("Roberts, C.J.", "dissent", "")])


class ConstitutionCites(unittest.TestCase):
    def test_forms(self):
        from tribonian import lawcites
        for text, key in [("the Constitution of the United States, art. 1, sect. 10, par. 1, which", "const:art-1:10"),
                          ("U.S. Const. art. I, sec. 10, cl. 1", "const:art-1:10"),
                          ("United States Constitution, Article III, Section 2", "const:art-3:2")]:
            self.assertEqual(list(lawcites.find(text)["cites"]), [key], text)
        self.assertEqual(dict(lawcites.find("the Constitution, art. 1, sec. 10")["cites"]), {})   # whose? not read


@unittest.skipUnless(__import__("importlib").util.find_spec("eyecite"), "eyecite not installed")
class Eyecite(unittest.TestCase):
    def test_same_cites(self):
        text = "Employment Div. v. Smith, 494 U. S. 872 (1990). Id., at 890. Smith, 494 U. S., at 879."
        self.assertEqual([c.kind for c in F.find(text, "eyecite")], ["full", "id", "pin"])
        self.assertEqual([c.kind for c in F.find(text, "both")], ["full", "id", "pin"])


if __name__ == "__main__":
    unittest.main()
