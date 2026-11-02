#!/usr/bin/env python3
"""Case history on Loper Bright's real docket text (D.D.C. -> D.C. Cir. -> U.S. -> back). No network."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tribonian import history as h                             # noqa: E402

DCD = {"court": "dcd", "number": "1:20-cv-00466", "found": True, "case_name": "Loper Bright v. Ross", "date_filed": "2020-02-19",
       "date_terminated": "2021-06-24", "lower_numbers": [], "appeal_from": None, "origin": "", "entries": [
           {"date": "2021-06-23", "number": 38, "text": "FINAL JUDGMENT. Signed by Judge Emmet G. Sullivan on 6/23/2021."},
           {"date": "2021-07-15", "number": 40, "text": "NOTICE OF APPEAL TO DC CIRCUIT COURT as to 39 Judgment"},
           {"date": "2021-07-15", "number": 41, "text": "Transmission of the Notice of Appeal to US Court of Appeals."},
           {"date": "2021-07-19", "text": "USCA Case Number 21-5166 for 40 Notice of Appeal to DC Circuit Court"},
           {"date": "2022-10-05", "number": 42, "text": "RECALLED PURSUANT TO USCA ORDER FILED 7/30/2024.....MANDATE of USCA as to 40 Notice of Appeal; USCA Case Number 21-5166."},
       ]}
CADC = {"court": "cadc", "number": "21-5166", "found": True, "case_name": "", "date_filed": "2021-07-19", "date_terminated": "2022-08-12",
        "noa_date": "2021-07-15", "lower_numbers": ["1:20-cv-00466"], "appeal_from": "dcd", "origin": "D.D.C.", "entries": [
            {"date": "2022-08-12", "text": "[VACATED PURSUANT TO SUPREME COURT JUDGMENT FILED ON 07/30/2024]--PER CURIAM JUDGMENT [1959084] filed"},
            {"date": "2022-08-12", "text": "CLERK'S ORDER [1959088] filed withholding issuance of the mandate."},
            {"date": "2022-10-05", "text": "MANDATE ISSUED to Clerk, U.S. District Court. [21-5166]"},
            {"date": "2022-11-15", "text": "LETTER [1975291] received from the Clerk of the Supreme Court of the United States notifying this court: A petition for writ of certiorari was filed and placed on the docket on 11/15/2022 as No. 22-451."},
            {"date": "2024-06-28", "text": "OPINION [2062278] received from the Clerk of the Supreme Court of the United States notifying this court of the following disposition of case No. 22-451 argued on 01/17/2024: the lower court decision was vacated and the case remanded on 06/28/2024."},
            {"date": "2024-07-30", "text": "CLERK'S ORDER [2067373] filed recalling mandate to district court [1967718-2]."},
            {"date": "2024-08-07", "text": "UNOPPOSED MOTION [2068861] to postpone oral argument filed"},
        ]}
US = {"court": "scotus", "number": "22-451", "found": True, "case_name": "", "date_filed": "2022-11-15", "date_terminated": "",
      "lower_numbers": ["21-5166"], "appeal_from": "cadc", "origin": "", "entries": []}
ALL = {("dcd", "1:20-cv-00466"): DCD, ("cadc", "21-5166"): CADC, ("scotus", "22-451"): US}


def chain_from(court, number):
    return h.walk((court, number), lambda c, n: ALL[(c, n)])


def trips(chain):
    return {(l["from"][0], l["to"][0], l["kind"]) for l in chain["links"]}


class Classify(unittest.TestCase):
    def test_kinds(self):
        for text, kind in [("MANDATE ISSUED to Clerk", "mandate"), ("NOTICE OF APPEAL TO DC CIRCUIT", "appeal"),
                           ("CLERK'S ORDER filed withholding issuance of the mandate.", None), ("MOTION to remand", None),
                           ("NOTICE of intention to participate as amicus", None),
                           ("Judgment VACATED and case REMANDED", "remand"),
                           ("NOTICE of Appeal Due letter sent to counsel", None),
                           ("EO: The Court asks whether counsel will represent Plaintiff on remand", None),
                           ("ORDER: The court is prepared to rule on the parties' cross-motions, but the appeal may be dismissed", None),
                           ("MINUTE ORDER granting 14 Joint Motion; holding in abeyance 15 Motion to Expedite", None),
                           ("PER CURIAM ORDER filed granting the joint motion to hold case in abeyance", "abeyance"),
                           ("MANDATE of Ninth Circuit filed: case REMANDED to the district court", "mandate"),
                           ("Petition DENIED.", "cert_denied"), ("JUDGMENT ISSUED.", "mandate"), ("Argued. For petitioners: X", "argued")]:
            self.assertEqual(h.classify(text)[0], kind, text)

    def test_vacated_prefix(self):
        self.assertEqual(h.classify("[VACATED PURSUANT TO X]--PER CURIAM JUDGMENT filed"), ("judgment", True))

    def test_usca_spellings(self):
        for t in ("USCA Case Number 23-15605 for 24", "MANDATE of USCA; USCA No. 23-15605", "USCA No.23-15605"):
            self.assertEqual(h.USCA_RE.findall(t), ["23-15605"], t)

    def test_judge_suffix(self):
        self.assertEqual(h.clean_number("1:20-cv-00466-EGS"), "1:20-cv-00466")
        self.assertEqual(h.clean_number("1:22-CV-40"), "1:22-cv-00040")
        self.assertEqual(h.clean_number("21-5166"), "21-5166")

    def test_supreme_court_wording(self):
        for text, kind in [("SUPREME COURT ORDER received granting petition for writ of certiorari filed by Appellant in 23-40699 on 06/01/2026.", "cert_granted"),
                           ("SUPREME COURT NOTICE that petition for writ of certiorari [82] was filed by Appellant on 08/20/2025. Supreme Court Number: 25-5930.", "cert_petition"),
                           ("Petition for a writ of certiorari filed. (Response due December 17, 2025)", "cert_petition"),
                           ("Petition GRANTED.", "cert_granted"),
                           ("Application (24A1) to extend the time to file a petition for a writ of certiorari granted by Justice Alito.", None),
                           ("ORDER denying extension of time to file petition for certiorari", None)]:
            self.assertEqual(h.classify(text)[0], kind, text)
        self.assertEqual(h.SCOTUS_NO_RE.findall("was filed. Supreme Court Number: 25-5930."), ["25-5930"])

    def test_fifth_circuit_copies_stay_in_the_supreme_courts_lane(self):
        ca5 = {"court": "ca5", "number": "23-40699", "found": True, "date_filed": "2023-12-12", "lower_numbers": ["1:22-CV-40"], "appeal_from": "txed", "origin": "", "entries": [
            {"date": "2025-10-24", "text": "SUPREME COURT NOTICE that petition for writ of certiorari [82] was filed on 08/20/2025. Supreme Court Number: 25-5930."},
            {"date": "2026-06-02", "text": "SUPREME COURT ORDER received granting petition for writ of certiorari filed by Appellant in 23-40699 on 06/01/2026."}]}
        us = {"court": "scotus", "number": "25-5930", "found": True, "date_filed": "2025-10-22", "lower_numbers": ["23-40699"], "appeal_from": "ca5", "origin": "", "entries": [
            {"date": "2025-08-20", "text": "Petition for a writ of certiorari filed."},
            {"date": "2026-06-01", "text": "Motion to proceed in forma pauperis GRANTED.  Petition for a writ of certiorari GRANTED limited to the following question"},
            {"date": "2026-11-02", "text": "Argued. For petitioner: X"}]}
        tx = {"court": "txed", "number": "1:22-cv-00040", "found": True, "date_filed": "2022-02-07", "lower_numbers": [], "appeal_from": None, "origin": "", "entries": []}
        chain = h.walk(("scotus", "25-5930"), lambda c, n: {"ca5": ca5, "scotus": us, "txed": tx}[c])
        self.assertIn("1:22-cv-00040", {d["number"] for d in chain["dockets"]})
        self.assertTrue([e for e in chain["events"] if e["docket"][0] == "scotus" and e["kind"] == "cert_granted" and e.get("key")])
        self.assertFalse([e for e in chain["events"] if e["docket"][0] == "ca5" and e["kind"].startswith("cert") and e.get("key")])


class Walk(unittest.TestCase):
    def test_same_chain_from_any_seed(self):
        for seed in ALL:
            self.assertEqual({d["number"] for d in chain_from(*seed)["dockets"]}, {"1:20-cv-00466", "21-5166", "22-451"})

    def test_all_jumps(self):
        self.assertEqual(trips(chain_from("cadc", "21-5166")), {("dcd", "cadc", "appeal"), ("cadc", "dcd", "mandate"),
                                                                ("cadc", "scotus", "cert"), ("scotus", "cadc", "remand")})

    def test_state_court_not_followed(self):
        raw = dict(US, appeal_from=None, lower_numbers=["12-3"], origin="Supreme Court of Ohio")
        chain = h.walk(("scotus", "22-451"), lambda c, n: raw)
        self.assertEqual(len(chain["dockets"]), 1)
        self.assertIn("Supreme Court of Ohio", chain["notes"][0])

    def test_docket_not_found_is_said(self):
        raw = lambda c, n: ALL.get((c, n)) or dict(CADC, court=c, number=n, found=False, entries=[], lower_numbers=[], appeal_from=None)
        g = dict(DCD, entries=DCD["entries"] + [{"date": "2025-01-01", "text": "USCA Case Number 25-1 for appeal"}])
        chain = h.walk(("dcd", "1:20-cv-00466"), lambda c, n: g if c == "dcd" else raw(c, n))
        self.assertTrue(any("25-1" in n and "not on CourtListener" in n for n in chain["notes"]))

    def test_missing_entries(self):
        self.assertEqual(h.missing_numbers(DCD["entries"]), 38)


class Display(unittest.TestCase):
    def setUp(self):
        self.chain = chain_from("cadc", "21-5166")
        self.keys = [e for e in self.chain["events"] if e.get("key")]

    def test_copy_of_another_courts_action_hidden(self):
        us = dict(US, entries=[{"date": "2024-06-28", "text": "Judgment VACATED and case REMANDED."}])
        chain = h.walk(("cadc", "21-5166"), lambda c, n: us if c == "scotus" else ALL[(c, n)])
        rows = [e for e in chain["events"] if e["kind"] == "remand"]
        self.assertEqual({e["docket"][0]: e["key"] for e in rows}, {"cadc": False, "scotus": True})

    def test_copy_kept_when_acting_court_has_no_entries(self):
        self.assertTrue(any(e["kind"] == "remand" and e["docket"] == ["cadc", "21-5166"] for e in self.keys))

    def test_recalled_mandate(self):
        m = [l for l in self.chain["links"] if l["kind"] == "mandate"][0]
        self.assertEqual(m["recalled"], "2024-07-30")
        self.assertTrue(all(e["vacated"] for e in self.chain["events"] if e["kind"] == "mandate"))

    def test_latest_and_reopened(self):
        late = [e for e in self.keys if e.get("latest") and e["docket"] == ["cadc", "21-5166"]]
        self.assertIn("UNOPPOSED MOTION", late[0]["text"])
        raw = dict(CADC, entries=CADC["entries"] + [
            {"date": "2025-08-13", "text": "PER CURIAM ORDER filed granting the joint motion to hold case in abeyance."}])
        evs, _ = h.read_docket(raw)
        self.assertTrue([e for e in evs if e["kind"] == "terminated"][0]["reopened"])
        self.assertEqual(evs[-1]["kind"], "abeyance")

    def test_second_mandate_kept(self):
        raw = dict(CADC, entries=CADC["entries"] + [{"date": "2025-01-01", "text": "MANDATE ISSUED to Clerk"}])
        chain = h.walk(("cadc", "21-5166"), lambda c, n: raw if c == "cadc" else ALL[(c, n)])
        self.assertEqual(len([l for l in chain["links"] if l["kind"] == "mandate"]), 2)


# Sackett's first round: the district docket never names 08-35854, only the Supreme Court's number.
IDD = {"court": "idd", "number": "2:08-cv-00185", "found": True, "case_name": "Sackett", "date_filed": "2008-04-28", "date_terminated": "2019-03-31",
       "lower_numbers": [], "appeal_from": None, "origin": "", "entries": [
           {"date": "2008-10-16", "number": 29, "text": "NOTICE OF APPEAL as to 22 Judgment by Michael Sackett"},
           {"date": "2010-09-17", "number": 40, "text": "OPINION of USCA as to 29 Notice of Appeal (AFFIRMED)"},
           {"date": "2010-12-09", "number": 42, "text": "MANDATE of USCA as to 29 Notice of Appeal"},
           {"date": "2011-02-25", "number": 43, "text": "Letter from Supreme Court of the United States Re: Petition for a writ of certiorari (10-1062)(dks)"},
           {"date": "2012-03-22", "number": 45, "text": "Letter from Supreme Court of the United States that the judgment is reversed and the case is remanded to the United States of Appeals for the Ninth Circuit"},
           {"date": "2012-05-03", "number": 49, "text": "MANDATE of USCA as to 29 Notice of Appeal"},
           {"date": "2019-05-30", "number": 122, "text": "NOTICE OF APPEAL by Michael Sackett"},
           {"date": "2019-05-30", "number": 123, "text": "USCA Case Number 19-35469 for 122 Notice of Appeal"},
           {"date": "2021-10-08", "number": 133, "text": "MANDATE - of USCA 19-35469 as to 122 Notice of Appeal"},
           {"date": "2023-05-30", "number": 135, "text": "Letter from the United States Supreme Court re: USCA 19-35469. (Attachments: # 1 21-454 Opinion)(jd)"},
       ]}
SACKETT = {
    ("idd", "2:08-cv-00185"): IDD,
    ("ca9", "19-35469"): {"court": "ca9", "number": "19-35469", "found": True, "date_filed": "2019-05-30", "lower_numbers": ["2:08-cv-00185"],
                          "appeal_from": "idd", "origin": "", "entries": [
                              {"date": "2021-10-08", "text": "MANDATE ISSUED.(RMG, MTF and JAO)"},
                              {"date": "2023-05-30", "text": "Supreme Court Case Info Case number: 21-454 Filed on: 09/22/2021 Ruling: Reversed/Remanded, 05/25/2023"}]},
    ("scotus", "21-454"): {"court": "scotus", "number": "21-454", "found": True, "date_filed": "2021-09-24", "lower_numbers": ["19-35469"],
                           "appeal_from": "ca9", "origin": "", "entries": [{"date": "2023-05-25", "text": "Judgment REVERSED and case REMANDED."}]},
    ("scotus", "10-1062"): {"court": "scotus", "number": "10-1062", "found": True, "date_filed": "2011-02-25", "lower_numbers": ["08-35854"],
                            "appeal_from": "ca9", "origin": "", "entries": []},
    ("ca9", "08-35854"): {"court": "ca9", "number": "08-35854", "found": True, "date_filed": "", "lower_numbers": [], "appeal_from": None,
                          "origin": "", "entries": []},
}


class TwoRounds(unittest.TestCase):
    def setUp(self):
        self.chain = h.walk(("scotus", "21-454"), lambda c, n: SACKETT[(c, n)])

    def test_every_docket_found(self):
        self.assertEqual({d["number"] for d in self.chain["dockets"]}, {n for _, n in SACKETT})

    def test_first_round_inferred(self):
        got = {(l["from"][1], l["to"][1], l["kind"], l["date"]) for l in self.chain["links"] if l["how"] == "inferred"}
        self.assertEqual(got, {("2:08-cv-00185", "08-35854", "appeal", "2008-10-16"), ("08-35854", "2:08-cv-00185", "mandate", "2010-12-09"),
                               ("10-1062", "08-35854", "remand", "2012-03-22"), ("08-35854", "2:08-cv-00185", "mandate", "2012-05-03")})

    def test_copies_hidden_only_inside_the_acting_courts_reach(self):
        keys = {(e["date"], e["kind"]) for e in self.chain["events"] if e.get("key") and e["docket"][0] == "idd"}
        self.assertIn(("2010-09-17", "opinion"), keys)      # 08-35854 has no entries: this is the only record
        self.assertNotIn(("2021-10-08", "mandate"), keys)   # 19-35469 has its own

    def test_one_remand_arrow_per_remand(self):
        back = [l for l in self.chain["links"] if l["kind"] == "remand" and l["to"][1] == "19-35469"]
        self.assertEqual([l["date"] for l in back], ["2023-05-25"])

    def test_usca_number_is_not_a_supreme_court_number(self):
        self.assertEqual(h.SCOTUS_NO_RE.findall("Letter re: USCA No. 19-35469"), [])

    def test_graph_order(self):
        lanes = [n for _, n in h.lanes_of(self.chain)]
        self.assertEqual(lanes, ["2:08-cv-00185", "08-35854", "19-35469", "10-1062", "21-454"])
        print("\n" + h.render(self.chain))


class WeakMatches(unittest.TestCase):
    def test_all_candidates_listed_when_no_number(self):
        idd = dict(IDD, entries=IDD["entries"] + [{"date": "2024-01-02", "text": "MANDATE of USCA as to 29 Notice of Appeal"}])
        chain = h.walk(("scotus", "21-454"), lambda c, n: idd if c == "idd" else SACKETT[(c, n)])
        e = [e for e in chain["events"] if e["date"] == "2024-01-02"][0]
        self.assertEqual({x["docket"][1] for x in e["sources"]}, {"08-35854", "19-35469"})
        self.assertTrue(all(x["weak"] for x in e["sources"]))
        arrows = [l for l in chain["links"] if l["date"] == "2024-01-02"]
        self.assertEqual(len(arrows), 2)
        self.assertIn("(?, one of 2)", h.render(chain))
        self.assertIn("mandate from 9th Cir.", h.render(chain))

    def test_number_in_text_is_firm(self):
        idd = dict(IDD, entries=IDD["entries"] + [{"date": "2024-01-02", "text": "MANDATE of USCA 19-35469 as to Notice of Appeal"}])
        chain = h.walk(("scotus", "21-454"), lambda c, n: idd if c == "idd" else SACKETT[(c, n)])
        e = [e for e in chain["events"] if e["date"] == "2024-01-02"][0]
        self.assertEqual(e["sources"], [{"docket": ["ca9", "19-35469"], "weak": False}])

    def test_consolidated_remand(self):
        us = dict(US, lower_numbers=["21-5166", "21-9999"], entries=[{"date": "2024-06-28", "text": "Judgment VACATED and case REMANDED."}])
        got = lambda c, n: us if c == "scotus" else ALL.get((c, n)) or dict(CADC, number=n, entries=[], lower_numbers=[], appeal_from=None)
        chain = h.walk(("scotus", "22-451"), got)
        back = [l for l in chain["links"] if l["kind"] == "remand" and l["from"][0] == "scotus"]
        self.assertEqual(len(back), 2)
        self.assertTrue(all(l["weak"] for l in back))

    def test_district_remand_marked(self):
        e = {"kind": "remand", "docket": ["idd", "1"], "text": "ORDER remanding to state court"}
        self.assertEqual(h.KIND_TEXT(e), "remanded (?)")


# Encino, from CourtListener's real search results: every record of 13-55323 is empty, and nothing names 15-415.
NINTH = {"court": "ca9", "number": "13-55323", "found": True, "case_name": "Hector Navarro v. Encino Motorcars", "date_filed": "", "lower_numbers": [],
         "appeal_from": None, "origin": "", "entries": []}
ENCINO = {("ca9", "13-55323"): NINTH,
          ("scotus", "16-1362"): {"court": "scotus", "number": "16-1362", "found": True, "date_filed": "2017-05-12", "lower_numbers": ["13-55323"],
                                  "appeal_from": "ca9", "origin": "", "entries": [{"date": "2018-04-02", "text": "Judgment REVERSED and case REMANDED."}]},
          ("scotus", "15-415"): {"court": "scotus", "number": "15-415", "found": True, "date_filed": "2015-10-02", "lower_numbers": ["13-55323"],
                                 "appeal_from": "ca9", "origin": "", "entries": []},
          ("cacd", "2:12-cv-08051"): {"court": "cacd", "number": "2:12-cv-08051", "found": True, "date_filed": "2012-09-18", "lower_numbers": [],
                                      "appeal_from": None, "origin": "", "entries": [
                                          {"date": "2013-03-01", "number": 40, "text": "NOTICE OF APPEAL by Navarro"},
                                          {"date": "2013-03-05", "number": 41, "text": "USCA Case Number 13-55323 for 40 Notice of Appeal"}]},
          ("cacd", "2:15-cv-00001"): {"court": "cacd", "number": "2:15-cv-00001", "found": True, "date_filed": "2012-01-01", "lower_numbers": [],
                                      "appeal_from": None, "origin": "", "entries": [{"date": "2013-01-01", "text": "USCA Case Number 13-99999 for appeal"}]}}
OPINIONS = [{"caseName": "Hector Navarro v. Encino Motorcars", "court_id": "ca9", "dateFiled": "2015-03-24", "docketNumber": "13-55323", "citation": ["780 F.3d 1267", "2015 WL 1296074"]},
            {"caseName": "Encino Motorcars, LLC v. Navarro", "court_id": "scotus", "dateFiled": "2016-01-15", "docketNumber": "No. 15\u2013415.", "citation": ["136 S. Ct. 890"]},
            {"caseName": "Encino Motorcars, LLC v. Navarro", "court_id": "scotus", "dateFiled": "2016-06-20", "docketNumber": "15\u2013415.", "citation": ["579 U.S. 211", "136 S. Ct. 2117"]},
            {"caseName": "Other", "court_id": "ca9", "dateFiled": "2016-06-20", "docketNumber": "14-1", "citation": []}]
DOCKETS = [{"court_id": "cacd", "docketNumber": "2:12-cv-08051"}, {"court_id": "cacd", "docketNumber": "2:15-cv-00001"}, {"court_id": "nysd", "docketNumber": "1:12-cv-1"}]


class NameSearch(unittest.TestCase):
    def setUp(self):
        self.chain = h.walk(("scotus", "16-1362"), lambda c, n: ENCINO[(c, n)], find=lambda raw: h.related_from(raw, OPINIONS, DOCKETS))

    def test_query_any_word_of_each_side(self):
        self.assertEqual(h.name_query("Hector Navarro v. Encino Motorcars, LLC"), "caseName:((Hector OR Navarro) AND (Encino OR Motorcars))")
        self.assertIsNone(h.name_query("In re Sackett"))

    def test_numbers_and_year(self):
        self.assertEqual(h.plain_number("No. 15\u2013415."), "15-415")
        self.assertEqual(h.appeal_year("13-55323"), 2013)
        self.assertEqual(h.appeal_year("98-1"), 1998)

    def test_named_candidates_become_firm(self):
        got = {(l["from"][1], l["to"][1], l["kind"], bool(l.get("weak"))) for l in self.chain["links"]}
        self.assertIn(("13-55323", "15-415", "cert", False), got)
        self.assertIn(("2:12-cv-08051", "13-55323", "appeal", False), got)
        self.assertIn(("15-415", "13-55323", "returned", True), got)

    def test_unconfirmed_candidate_shown_not_followed(self):
        self.assertNotIn("13-99999", {d["number"] for d in self.chain["dockets"]})
        cand = {d["number"] for d in self.chain["dockets"] if d.get("candidate")}
        self.assertEqual(cand, {"2:15-cv-00001"})
        self.assertIn("matched by name only", h.render(self.chain))
        self.assertTrue(any(l["how"] == "name" and l["weak"] for l in self.chain["links"]))

    def test_decisions_fill_empty_lanes(self):
        rows = {e["text"] for e in self.chain["events"] if e["kind"] == "decision"}
        self.assertEqual(rows, {"Decision (780 F.3d 1267)", "Decision or order (136 S. Ct. 890)", "Decision (579 U.S. 211)"})
        print("\n" + h.render(self.chain))

    def test_lexis_is_not_the_us_reports(self):
        hits = [{"court_id": "scotus", "dateFiled": "2016-01-15", "docketNumber": "15-415", "citation": ["2016 U.S. LEXIS 635", "136 S. Ct. 890"]}]
        r = h.related_from(NINTH, hits, [])
        self.assertEqual(r["decisions"]["scotus 15-415"][0]["text"], "Decision or order (2016 U.S. LEXIS 635)")

    def test_same_day_records_keep_the_cite(self):
        hits = [{"court_id": "ca9", "dateFiled": "2017-01-09", "docketNumber": "13-55323", "citation": []},
                {"court_id": "ca9", "dateFiled": "2017-01-09", "docketNumber": "13-55323", "citation": ["845 F.3d 925"]}]
        self.assertEqual([r["text"] for r in h.related_from(NINTH, hits, [])["decisions"]["ca9 13-55323"]], ["Decision (845 F.3d 925)"])

    def test_cca_wording(self):
        self.assertEqual(h.USCA_RE.findall("MANDATE of Ninth Circuit Court of Appeals filed re: Notice of Appeal 29 CCA # 13-55323."), ["13-55323"])
        self.assertEqual(h.USCA_RE.findall("NOTIFICATION by Circuit Court of Appellate Docket Number 13-55323, 9th CCA"), ["13-55323"])

    def test_copy_gets_firm_arrow_when_only_opinions_cover_the_date(self):
        cacd = dict(ENCINO[("cacd", "2:12-cv-08051")])
        cacd["entries"] = cacd["entries"] + [{"date": "2015-06-10", "number": 34, "text": "MANDATE of Ninth Circuit Court of Appeals filed re: Notice of Appeal 29 CCA # 13-55323."}]
        chain = h.walk(("scotus", "16-1362"), lambda c, n: cacd if n == "2:12-cv-08051" else ENCINO[(c, n)], find=lambda raw: h.related_from(raw, OPINIONS, DOCKETS))
        m = [l for l in chain["links"] if l["kind"] == "mandate" and l["date"] == "2015-06-10"]
        self.assertEqual([(l["from"][1], l["to"][1], bool(l.get("weak"))) for l in m], [("13-55323", "2:12-cv-08051", False)])

    def test_nothing_found_is_said(self):
        chain = h.walk(("ca9", "13-55323"), lambda c, n: ENCINO[(c, n)], find=lambda raw: {"decisions": {}, "candidates": []})
        self.assertIn("earlier history may be missing", " ".join(chain["notes"]))


class Render(unittest.TestCase):
    def test_graph(self):
        g = h.render(chain_from("cadc", "21-5166"), cites={"dcd": "D.D.C."})
        print("\n" + g)
        for s in ("D.D.C. 1:20-cv-00466", "D.C. Cir. 21-5166", "U.S. 22-451", "remand -> D.C. Cir. 21-5166",
                  "judgment (later vacated or recalled)", "mandate (later vacated or recalled)"):
            self.assertIn(s, g)


if __name__ == "__main__":
    unittest.main()
