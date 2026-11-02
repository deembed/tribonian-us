#!/usr/bin/env python3
"""tribonian setup: config writes, one-set downloads with resume (against a local server), status on an empty install.

    python -m pytest tests/test_setup_guide.py
"""
import http.server, json, os, subprocess, sys, tempfile, threading, unittest
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


FILES = {"a": b"x" * 2000, "b": b"y" * 3000}
LIST = {"a": ("2026-03-31", "2026-06-30"), "b": ("2026-03-31",)}       # only March is complete


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/?"):
            name = self.path.rsplit("/", 1)[1].rstrip("-")
            body = "<r>" + "".join("<Contents><Key>bulk-data/%s-%s.csv.bz2</Key><Size>%d</Size></Contents>" % (name, d, len(FILES[name]))
                                   for d in LIST[name]) + "</r>"
            body = body.encode()
            self.send_response(200)
        else:
            body = FILES[self.path.split("/")[-1].split("-")[0]]
            r = self.headers.get("Range")
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
            (d / "a-2026-03-31.csv.bz2.part").write_bytes(b"x" * 500)            # interrupted earlier
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


class EmptyInstall(unittest.TestCase):
    def run_setup(self, d, *args, stdin=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CL_") and k != "COURTLISTENER_TOKEN"}
        env.update(TRIBONIAN_CONFIG=str(Path(d) / "config.toml"), TRIBONIAN_WORK=str(Path(d) / "work"), PYTHONPATH=str(ROOT / "src"))
        return subprocess.run([sys.executable, "-m", "tribonian", "setup", *args], input=stdin, capture_output=True, text=True, env=env)

    def report(self, d):
        return json.loads(self.run_setup(d, "--json").stdout)

    def test_token_and_api_only_clear_their_steps_and_the_token_is_never_printed(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.report(d)
            steps = {s["id"]: s for s in r["steps"]}
            self.assertFalse(steps["database"]["done"])
            self.assertEqual(steps["database"]["actions"][0]["commands"], [["setup", "download"], ["db", "load"]])
            out = self.run_setup(d, "set", "courtlistener_token", stdin="sekret-token\n")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn("sekret-token", out.stdout)
            r = self.report(d)
            self.assertNotIn("sekret-token", json.dumps(r))
            self.assertTrue({s["id"]: s for s in r["steps"]}["token"]["done"])
            self.run_setup(d, "set", "data_mode", "api")
            self.assertTrue({s["id"]: s for s in self.report(d)["steps"]}["database"]["done"])

    def test_refuses_other_keys(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(self.run_setup(d, "set", "work_dir", "/x").returncode, 0)


if __name__ == "__main__":
    unittest.main()
