"""Word-processor documents (RTF, Word .doc/.docx) -> HTML for the html-paragraphs family (Oklahoma's constitution is
RTF). [<ST>.<code>.pandoc].from = "rtf" | "docx" | "doc". Converted by LibreOffice headless (soffice --convert-to html),
which reads all three; pandoc is used for docx when LibreOffice is missing (older pandoc has no RTF reader).
Needs LibreOffice on the machine that builds (apt install libreoffice-writer-nogui, or libreoffice)."""
import pathlib
import shutil
import subprocess
import sys
import tempfile


def to_html(raw, formula):
    cfg = (formula or {}).get("pandoc", {})
    src = cfg.get("from", "rtf") if isinstance(cfg, dict) else "rtf"
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    with tempfile.TemporaryDirectory() as tmp:
        inp = pathlib.Path(tmp) / ("doc." + src)
        inp.write_bytes(raw)
        if soffice:
            subprocess.run([soffice, "--headless", "--convert-to", "html:HTML (StarWriter):UTF8", "--outdir", tmp, str(inp)],
                           check=True, capture_output=True)
            return (pathlib.Path(tmp) / "doc.html").read_bytes()
        if src == "docx" and shutil.which("pandoc"):
            out = subprocess.run(["pandoc", "-f", "docx", "-t", "html", "--wrap=none", str(inp)],
                                 check=True, capture_output=True).stdout
            return b'<html><body><div id="doc">\n' + out + b"\n</div></body></html>"
        sys.exit("LibreOffice (soffice) not found: install it (apt install libreoffice-writer-nogui) to read %s documents" % src)
