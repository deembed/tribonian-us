//! A client for `tribonian serve`: one long-running Python process, one JSON request per line on its
//! stdin, one reply per line on its stdout (protocol in src/tribonian/serve.py). Reads that need the
//! scripts' code (law text, fetches, the law browser) go through it instead of the app opening files.
//!
//! Blocking: call it from a worker thread, never the UI thread. Dropping it asks the process to quit.

use crate::config::Py;
use serde_json::{json, Map, Value};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Stdio};

/// The protocol this app speaks (serve.py PROTOCOL).
pub const PROTOCOL: i64 = 1;

pub struct Serve {
    child: Child,
    stdin: ChildStdin,
    out: BufReader<ChildStdout>,
    next: u64,
}

impl Serve {
    /// Start `tribonian serve` (through TRIBONIAN_COMMAND_PREFIX when set) and check it answers a ping
    /// in this protocol. Its stderr (progress, warnings) goes to the app's.
    pub fn start(py: &Py) -> Result<Serve, String> {
        let mut child = py
            .cmd()
            .arg("serve")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| format!("could not start tribonian serve: {e}"))?;
        let stdin = child.stdin.take().ok_or("tribonian serve: no stdin")?;
        let out = BufReader::new(child.stdout.take().ok_or("tribonian serve: no stdout")?);
        let mut s = Serve { child, stdin, out, next: 1 };
        let p = s.call("ping", json!({}))?;
        match p.get("protocol").and_then(Value::as_i64) {
            Some(PROTOCOL) => Ok(s),
            other => Err(format!("tribonian serve speaks protocol {other:?}; this app speaks {PROTOCOL} (rebuild one of them)")),
        }
    }

    /// One request: `op` plus the fields of `args` (an object). -> the reply's "ok", or its "error".
    pub fn call(&mut self, op: &str, args: Value) -> Result<Value, String> {
        let id = self.next;
        self.next += 1;
        let mut req = match args {
            Value::Object(m) => m,
            Value::Null => Map::new(),
            _ => return Err("serve args must be a JSON object".into()),
        };
        req.insert("id".into(), json!(id));
        req.insert("op".into(), json!(op));
        let line = Value::Object(req).to_string();
        writeln!(self.stdin, "{line}").and_then(|_| self.stdin.flush()).map_err(|e| format!("tribonian serve has stopped: {e}"))?;
        let mut reply = String::new();
        match self.out.read_line(&mut reply) {
            Ok(0) => return Err("tribonian serve has stopped (see the terminal for why)".into()),
            Err(e) => return Err(format!("tribonian serve: {e}")),
            Ok(_) => {}
        }
        let v: Value = serde_json::from_str(&reply).map_err(|e| format!("tribonian serve sent something not JSON ({e}): {}", reply.trim()))?;
        if v.get("id").and_then(Value::as_u64) != Some(id) {
            return Err(format!("tribonian serve answered out of order: {}", reply.trim()));
        }
        if let Some(e) = v.get("error") {
            return Err(e.as_str().map(String::from).unwrap_or_else(|| e.to_string()));
        }
        Ok(v.get("ok").cloned().unwrap_or(Value::Null))
    }
}

impl Drop for Serve {
    fn drop(&mut self) {
        let _ = writeln!(self.stdin, "{}", json!({"op": "quit"}));
        let _ = self.stdin.flush();
        let _ = self.child.wait();
    }
}
