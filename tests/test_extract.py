"""extract.py's text format, as the builds read it: a page's notes block and the look-alike marks."""
from tribonian import case2md as c
from tribonian import extract as x


def page(body, notes):
    return "\n".join(body + [x.NOTES_START] + notes)


def test_given_notes_labels_paragraphs_and_continuation():
    pg = page(["Some text."], ["the rest of the last note.", x.NOTE_LABEL + "4", "Id. at 5.", "",
                               "A second paragraph.", x.NOTE_LABEL + "5", "See ibid."])
    body, notes = c.given_notes(pg, x)
    assert body == "Some text."
    assert notes == [(None, [["the rest of the last note."]]), ("4", [["Id. at 5."], ["A second paragraph."]]),
                     ("5", [["See ibid."]])]


def test_page_without_block_is_left_to_the_text_rules():
    assert c.given_notes("Just text.\n\n1 A note", x) == ("Just text.\n\n1 A note", None)


def test_layout_pages_takes_the_block_and_counts_on():
    p1 = page(["The year was 1822." + "62".translate(x.TO_MARK) + " More text here."], [x.NOTE_LABEL + "62", "Id. at 155."])
    lines, notes, _ = c.layout_pages(p1)
    assert [n[0] for n in notes] == ["62"]
    assert notes[0][2] == [["Id. at 155."]]
    assert all(x.NOTES_START not in l.text and "Id. at 155" not in l.text for l in lines)


def test_place_marks_finds_the_look_alike_mark_after_a_year():
    text = "Revolving guns in 1822." + "62".translate(x.TO_MARK) + " The Post lauded it."
    blocks = [{"kind": "p", "pieces": [(text, 0)]}]
    warned = []
    c.place_marks(blocks, [("62", 0, [["Id."]])], warned.append, spaced=True)
    assert not warned
    kinds = [p if isinstance(p, tuple) else p.tag for p in blocks[0]["pieces"]]
    assert kinds[1] == "footnotemark"
    assert blocks[0]["pieces"][0][0].endswith("1822.")


def test_plain_and_drop_marks():
    t = page(["Cite 597 U.S. 1." + "2".translate(x.TO_MARK)], [x.NOTE_LABEL + "2", "Note."]) + "\fNext page"
    assert x.plain(t) == "Cite 597 U.S. 1.\fNext page"
    assert x.marks_to_digits("a." + "12".translate(x.TO_MARK)) == "a.12"


def test_numbering_that_starts_again_gets_its_own_labels():
    p1 = page(["Body one." + "1".translate(x.TO_MARK) + " more." + "2".translate(x.TO_MARK)],
              [x.NOTE_LABEL + "1", "First.", x.NOTE_LABEL + "2", "Second."])
    p2 = page(["An opinion in the appendix." + "1".translate(x.TO_MARK)], [x.NOTE_LABEL + "1", "Its first."])
    _, notes, _ = c.layout_pages(p1 + "\f" + p2)
    assert [n[0] for n in notes] == ["1", "2", "b1"]
    blocks = [{"kind": "p", "pieces": [("An opinion in the appendix." + "1".translate(x.TO_MARK) + " Text.", 1)]}]
    warned = []
    c.place_marks(blocks, [("b1", 1, [["Its first."]])], warned.append, spaced=True)
    assert not warned and blocks[0]["pieces"][1].kids == ["b1"]
