"""tribonian build and tribonian sync-vault: job lines, the date check, mirroring. No pdflatex, no data."""
import os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tribonian import build, sync_vault                       # noqa: E402


def test_list_line():
    assert build.list_line("cases/a.json", "") == "case cases/a.json"
    assert build.list_line("cases/recap/b.json", "") == "recap cases/recap/b.json"
    assert build.list_line("cases\\recap\\b.json", "") == "recap cases\\recap\\b.json"     # a Windows path
    assert build.list_line("xml/usc05.xml", "") == "usc xml/usc05.xml --all"
    assert build.list_line("xml/usc05.xml", "/us/usc/t5/ptI/ch5") == "usc xml/usc05.xml --chapter /us/usc/t5/ptI/ch5"
    assert build.list_line("xml/usc05.xml", "/us/usc/t5/s552 /us/usc/t5/s552a") == \
        "usc xml/usc05.xml --section /us/usc/t5/s552 /us/usc/t5/s552a"
    assert build.list_line("usc", "xml/usc05.xml --chapter x") == "usc xml/usc05.xml --chapter x"


def test_due(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build, "FORCE", False)
    monkeypatch.setattr(build, "MD_ONLY", False)
    src, pdf = tmp_path / "a.json", tmp_path / "a.pdf"
    src.write_text("{}")
    job = "case a.json"
    assert build.due(job, {})                                   # no record
    assert build.due(job, {job: "a.pdf"})                       # PDF gone
    pdf.write_text("%PDF")
    old = time.time() - 100
    os.utime(src, (old, old))
    assert not build.due(job, {job: "a.pdf"})                   # PDF newer than its source
    os.utime(src, None)
    os.utime(pdf, (old, old))
    assert build.due(job, {job: "a.pdf"})                       # source changed since


def test_todo_on_an_empty_work_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["tribonian build", "--todo"])
    assert build.main() == 0
    assert "0 PDFs known, 0 out of date" in capsys.readouterr().out
    assert (tmp_path / "state" / "built.tsv").exists()


def test_mirror(tmp_path):
    src, dest = tmp_path / "md", tmp_path / "vault" / "USC"
    (src / "t01").mkdir(parents=True)
    (src / "t01" / "a.md").write_text("a")
    (dest / "old").mkdir(parents=True)
    (dest / "old" / "stale.md").write_text("x")
    (dest / "image.png").write_text("not a note")
    assert sync_vault.mirror(src, dest, dry=True, force=False) == 1
    assert (dest / "old" / "stale.md").exists()                # a dry run changes nothing
    sync_vault.mirror(src, dest, dry=False, force=False)
    assert (dest / "t01" / "a.md").read_text() == "a"
    assert not (dest / "old").exists()                          # the stale note and its emptied folder
    assert (dest / "image.png").exists()                        # only notes are generator-owned


def test_mirror_refuses_a_partial_source(tmp_path):
    src, dest = tmp_path / "md", tmp_path / "USC"
    src.mkdir()
    dest.mkdir()
    for i in range(10):
        (dest / ("%d.md" % i)).write_text("x")
    (src / "0.md").write_text("x")
    try:
        sync_vault.mirror(src, dest, dry=False, force=False)
    except SystemExit as e:
        assert "partial build" in str(e)
    else:
        raise AssertionError("should refuse")
    assert len(list(dest.glob("*.md"))) == 10
