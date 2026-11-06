#!/usr/bin/env python3
"""tribonian setup: config writes, restartable, checksummed downloads (against a local server), key and data mode, the Sync rows.

    python -m pytest tests/test_setup_guide.py
"""
import bz2, http.server, json, os, subprocess, sys, tempfile, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tribonian import setup_guide, tconfig                     # noqa: E402


class ConfigText(unittest.TestCase):
    def test_adds_replaces_removes_and_keeps_the_rest(self):
        text = '# keep me\nbulk_dir = "/old"  # note\n# opinions_db = "/commented"\n\n[gui]\npython = "py"\n'
        t = setup_guide.set_text(text, "bulk_dir", "/new")
        self.assertIn('bulk_dir = "/new"', t)
        self.assertNotIn("/old", t)
        self.assertIn('# opinions_db = "/commented"', t)
        t = setup_guide.set_text(t, "courtlistener_token", "abc")
        self.assertLess(t.index("courtlistener_token"), t.index("[gui]"))        # top level, not inside [gui]
        t = setup_guide.set_text(t, "gui.python", "/v/bin/python")
        self.assertEqual(tconfig.parse(t)["gui"]["python"], "/v/bin/python")
        t = setup_guide.set_text(t, "bulk_dir", "")
        self.assertNotIn("bulk_dir", t)
        self.assertEqual(tconfig.parse(t)["courtlistener_token"], "abc")

    def test_new_table_and_windows_path(self):
        t = setup_guide.set_text("", "gui.python", "C:\\v\\python.exe")
        self.assertEqual(tconfig.parse(t)["gui"]["python"], "C:\\v\\python.exe")

    def test_file_is_private_and_a_broken_one_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "config.toml"
            setup_guide.write_setting(p, "courtlistener_token", "abc")
            if os.name != "nt":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            p.write_text("bulk_dir = \n[[[\n")
            with self.assertRaises(ValueError):
                setup_guide.write_setting(p, "courtlistener_token", "abc")
            self.assertIn("[[[", p.read_text())
            self.assertEqual([x.name for x in p.parent.iterdir()], ["config.toml"])


FILES = {"a": bz2.compress(os.urandom(3000)), "b": bz2.compress(os.urandom(4000))}
ETAG = {"a": '"e-a"', "b": '"e-b"'}
FAIL = []            # paths to answer with a dropped connection, once each
CRC = {}             # name -> the CRC64-NVME the server claims on HEAD (no entry: no checksum header)
SEEN = {}            # header name -> last value received


def crc_of(data):
    c = setup_guide.Crc()
    c.add(data)
    return c.b64()
LIST = {"a": ("2026-03-31", "2026-06-30"), "b": ("2026-03-31",)}       # only March is complete


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        name = self.path.split("/")[-1].split("-")[0]
        SEEN["checksum-mode"] = self.headers.get("x-amz-checksum-mode")
        self.send_response(200)
        if name in CRC:
            self.send_header("x-amz-checksum-crc64nvme", CRC[name])
            self.send_header("x-amz-checksum-type", "FULL_OBJECT")
        self.send_header("Content-Length", str(len(FILES[name])))
        self.end_headers()

    def do_GET(self):
        SEEN["user-agent"] = self.headers.get("User-Agent")
        if self.path.startswith("/?"):
            name = self.path.rsplit("/", 1)[1].rstrip("-")
            body = "<r>" + "".join("<Contents><Key>bulk-data/%s-%s.csv.bz2</Key><Size>%d</Size><ETag>%s</ETag></Contents>"
                                   % (name, d, len(FILES[name]), ETAG[name].replace('"', "&quot;"))
                                   for d in LIST[name]) + "</r>"
            body = body.encode()
            self.send_response(200)
        else:
            if self.path in FAIL:
                FAIL.remove(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "999999")
                self.end_headers()
                self.wfile.write(FILES[self.path.split("/")[-1].split("-")[0]][:10])     # the right first bytes, then gone
                return
            name = self.path.split("/")[-1].split("-")[0]
            body = FILES[name]
            r = self.headers.get("Range")
            if r and self.headers.get("If-Range") not in (None, ETAG[name]):
                r = None                                               # changed since: the whole file
            if r:
                body = body[int(r[6:-1]):]
            self.send_response(206 if r else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Download(unittest.TestCase):
    def setUp(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_port
        os.environ.pop("HTTP_PROXY", None)
        os.environ["NO_PROXY"] = "127.0.0.1"
        os.environ["no_proxy"] = "127.0.0.1"
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def test_one_set_resumes_and_skips(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a-2026-03-31.csv.bz2.part").write_bytes(FILES["a"][:500])      # interrupted earlier
            date = setup_guide.download(d, base=self.base, names=["a", "b"], say=lambda s: None)
            self.assertEqual(date, "2026-03-31")                                  # the newest date both files have
            self.assertEqual((d / "a-2026-03-31.csv.bz2").read_bytes(), FILES["a"])
            self.assertEqual((d / "b-2026-03-31.csv.bz2").read_bytes(), FILES["b"])
            self.assertFalse(list(d.glob("*.part")))
            said = []
            setup_guide.download(d, base=self.base, names=["a", "b"], say=said.append)
            self.assertTrue(all(s.startswith("have") for s in said))

    def test_a_date_without_every_file_stops(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(OSError):
                setup_guide.download(d, date="2026-06-30", base=self.base, names=["a", "b"], say=lambda s: None)


    def test_a_changed_file_starts_over_and_a_dropped_connection_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a-2026-03-31.csv.bz2.part").write_bytes(b"stale bytes")
            (d / "a-2026-03-31.csv.bz2.part.json").write_text(json.dumps({"size": len(FILES["a"]), "etag": '"old"'}))
            FAIL.append("/bulk-data/b-2026-03-31.csv.bz2")
            said = []
            setup_guide.download(d, base=self.base, names=["a", "b"], say=said.append, delays=(0,))
            self.assertEqual((d / "a-2026-03-31.csv.bz2").read_bytes(), FILES["a"])
            self.assertEqual((d / "b-2026-03-31.csv.bz2").read_bytes(), FILES["b"])
            self.assertTrue(any("starting over" in s for s in said))
            self.assertTrue(any("trying again" in s for s in said))
            self.assertFalse(list(d.glob("*.part*")))

    def test_names_itself(self):
        with tempfile.TemporaryDirectory() as d:
            setup_guide.download(d, base=self.base, names=["a", "b"], say=lambda s: None)
            self.assertTrue(SEEN["user-agent"].startswith("tribonian-us/"))


@unittest.skipIf(setup_guide._crc64nvme is None, "awscrt not installed")
class Checksum(Download):
    def setUp(self):
        super().setUp()
        CRC.clear()
        CRC.update({n: crc_of(b) for n, b in FILES.items()})
        self.addCleanup(CRC.clear)

    def test_known_value(self):
        self.assertEqual(setup_guide.Crc().b64(), "AAAAAAAAAAA=")
        c = setup_guide.Crc()
        c.add(b"123456789")
        self.assertEqual(c.v, 0xAE8B14860A799888)                   # the CRC-64/NVME check value

    def test_matches_after_a_resume_and_a_dropped_connection(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a-2026-03-31.csv.bz2.part").write_bytes(FILES["a"][:500])      # interrupted earlier
            FAIL.append("/bulk-data/b-2026-03-31.csv.bz2")
            said = []
            setup_guide.download(d, base=self.base, names=["a", "b"], say=said.append, delays=(0,))
            self.assertEqual(SEEN["checksum-mode"], "ENABLED")
            self.assertEqual(sum("checksum matches" in s for s in said), 2)
            self.assertFalse(list(d.glob("*.part*")))

    def test_a_mismatch_deletes_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            good = FILES["a"]
            FILES["a"] = bytes(len(good))                              # right size, wrong bytes
            try:
                with self.assertRaises(OSError) as e:
                    setup_guide.download(d, base=self.base, names=["a", "b"], only=["a"], say=lambda s: None, delays=())
            finally:
                FILES["a"] = good
            self.assertIn("checksum mismatch", str(e.exception))
            self.assertFalse(list(d.iterdir()))

    def test_no_checksum_on_the_server_checks_the_size(self):
        CRC.pop("a")
        with tempfile.TemporaryDirectory() as d:
            said = []
            setup_guide.download(d, base=self.base, names=["a", "b"], only=["a"], say=said.append, delays=())
            self.assertTrue(any("no checksum on the server" in s for s in said))


class EmptyInstall(unittest.TestCase):
    def run_setup(self, d, *args, stdin=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CL_") and k != "COURTLISTENER_TOKEN"}
        env.update(TRIBONIAN_CONFIG=str(Path(d) / "config.toml"), TRIBONIAN_WORK=str(Path(d) / "work"), PYTHONPATH=str(ROOT / "src"),
                   PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")       # the file fallback, never the real key store
        return subprocess.run([sys.executable, "-m", "tribonian", "setup", *args], input=stdin, capture_output=True, text=True, env=env)

    def report(self, d):
        return json.loads(self.run_setup(d, "--json").stdout)

    def rows(self, d):
        return {r["id"]: r["status"] for r in self.report(d)["rows"]}

    def config(self, d):
        return tconfig.parse((Path(d) / "config.toml").read_text())

    def test_key_rows_and_the_mode_chosen_only_when_a_row_changes(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.report(d)
            self.assertFalse(r["required_ok"])
            self.assertEqual(self.rows(d)["key"], "fail")
            self.run_setup(d, "key", "skip")
            self.assertEqual(self.rows(d)["key"], "skipped")
            self.assertEqual(self.config(d)["data_mode"], "offline")
            self.assertTrue(self.report(d)["unavailable"])
            out = self.run_setup(d, "key", "set", stdin="sekret-token\n")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn("sekret-token", out.stdout)
            self.assertNotIn("sekret-token", json.dumps(self.report(d)))
            self.assertEqual(self.rows(d)["key"], "ok")
            self.assertNotIn("token_skipped", self.config(d))
            self.assertEqual(self.config(d)["data_mode"], "api")
            self.run_setup(d, "set", "data_mode", "complete")                       # the user's own pick
            self.run_setup(d, "key", "set", stdin="another\n")                     # key row unchanged: pick kept
            self.assertEqual(self.config(d)["data_mode"], "complete")
            self.run_setup(d, "key", "forget")                                     # key row changed
            self.assertEqual(self.config(d)["data_mode"], "offline")

    def test_set_token_goes_through_the_key_store_path(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_setup(d, "set", "courtlistener_token", stdin="abc\n")
            self.assertEqual(self.rows(d)["key"], "ok")

    def test_refuses_other_keys(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(self.run_setup(d, "set", "work_dir", "/x").returncode, 0)


class Modes(unittest.TestCase):
    def test_table(self):
        self.assertEqual(setup_guide.pick_mode(False, None), "offline")
        self.assertEqual(setup_guide.pick_mode(True, None), "api")
        self.assertEqual(setup_guide.pick_mode(False, "full"), "offline")
        self.assertEqual(setup_guide.pick_mode(True, "full"), "full")
        self.assertEqual(setup_guide.pick_mode(True, "lite"), "lite")


class LocalFiles(unittest.TestCase):
    def test_complete_partial_and_sets(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for n in setup_guide.BULK_FILES:
                (d / ("%s-2026-03-31.csv.bz2" % n)).write_bytes(b"")
            (d / "opinions-2026-06-30.csv.bz2.part").write_bytes(b"x" * 25)
            (d / "opinions-2026-06-30.csv.bz2.part.json").write_text('{"size": 100, "etag": ""}')
            f = setup_guide.local_files(d)
            self.assertEqual(f["opinions"]["part"], ("2026-06-30", 25, 100))
            self.assertEqual(f["opinion-clusters"]["complete"], ["2026-03-31"])        # not taken for "opinions"
            self.assertEqual(setup_guide.complete_set(f), "2026-03-31")


if __name__ == "__main__":
    unittest.main()
