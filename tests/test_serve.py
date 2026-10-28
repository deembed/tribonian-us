"""tribonian serve: the protocol, without touching data or the network."""
import io, json

from tribonian import serve


def run(*reqs):
    out = io.StringIO()
    serve.serve(io.StringIO("".join(json.dumps(r) + "\n" for r in reqs)), out)
    return [json.loads(l) for l in out.getvalue().splitlines()]


def test_ping_and_quit():
    r = run({"id": 1, "op": "ping"}, {"id": 2, "op": "quit"}, {"id": 3, "op": "ping"})
    assert [x["id"] for x in r] == [1, 2]                # nothing after quit
    assert r[0]["ok"]["protocol"] == serve.PROTOCOL


def test_errors_keep_serving():
    out = io.StringIO()
    serve.serve(io.StringIO('not json\n{"id": 5, "op": "nope"}\n{"id": 6, "op": "law.text"}\n{"id": 7, "op": "ping"}\n'), out)
    r = [json.loads(l) for l in out.getvalue().splitlines()]
    assert "not JSON" in r[0]["error"]
    assert r[1]["id"] == 5 and "no op" in r[1]["error"]
    assert r[2]["id"] == 6 and "key" in r[2]["error"]
    assert r[3]["ok"]["protocol"] == serve.PROTOCOL


def test_prints_go_to_stderr(monkeypatch):
    def noisy(req):
        print("chatter")
        return {"x": 1}
    monkeypatch.setitem(serve.OPS, "noisy", noisy)
    r = run({"id": 1, "op": "noisy"})
    assert r == [{"id": 1, "ok": {"x": 1}}]
