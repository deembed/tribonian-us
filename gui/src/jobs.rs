//! v2 actions: run the repo's own `tribonian` commands for one case and stream their output into a log panel.
//! The app itself still writes nothing; the scripts do, exactly as they would from a terminal.
//!
//! Build = `tribonian map get --cluster N` (case JSON and note, from the local database; skipped when
//! the case file already exists), then `tribonian case build cases/<file>` (note, PDF, and the case
//! profile with good-law status). Quick build passes --quick-goodlaw: the quick good-law check, which
//! reads only the newest 100 citing opinions (status now, no year) instead of up to ~2,000; a current
//! full result is kept.
//!
//! The same queue also runs single script commands for a case (`run_script`): "Find coupled cases"
//! queues `tribonian map coupling --cluster N`, which writes cases/citedby/<N>/coupling.json.
//!
//! And a run of commands that stops at the first failure (`run_steps`): the SCOTUS page's
//! "Fetch & build" queues `tribonian scotus fetch <docket>` then `tribonian scotus build <docket>` as one job.

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::Stdio;

use crate::config::Py;
use std::sync::mpsc::{channel, Receiver, Sender};

pub enum Msg {
    Line(String),
    Done { ok: bool },
}

pub struct Running {
    pub title: String,
    pub r#ref: i64,
    /// a Build or Quick build (else a single script command): only the status line's wording differs
    pub build: bool,
    pub rx: Receiver<Msg>,
}

/// The case's JSON file as cases/_index.json names it (written by `tribonian map get`); `work` is the
/// work folder.
pub fn full_file(work: &Path, r: i64) -> Option<String> {
    let text = std::fs::read_to_string(work.join("cases").join("_index.json")).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v[r.to_string()]["full"].as_str().filter(|s| !s.is_empty()).map(String::from)
}

/// Run one `tribonian` command (args after `tribonian`); true when it exits 0 and, if `need` is given, printed a line starting with it
/// (`tribonian case build` prints "wrote pdf/..." only when pdflatex succeeded, and exits 0 either way,
/// so build_all.sh checks for that line too).
fn run_step(py: &Py, args: &[String], need: Option<&str>, tx: &Sender<Msg>, wake: &(dyn Fn() + Send + Sync)) -> bool {
    let mut seen = need.is_none();
    let _ = tx.send(Msg::Line(format!("$ tribonian {}", args.join(" "))));
    let child = py
        .cmd()
        .args(args)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(Msg::Line(format!("could not start {} -m tribonian: {e}", py.python)));
            return false;
        }
    };
    // stderr on its own thread so neither pipe can fill up and stall the script
    let err = child.stderr.take().map(|s| {
        let tx = tx.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(s).lines().map_while(Result::ok) {
                let _ = tx.send(Msg::Line(line));
            }
        })
    });
    if let Some(out) = child.stdout.take() {
        for line in BufReader::new(out).lines().map_while(Result::ok) {
            if need.is_some_and(|n| line.starts_with(n)) {
                seen = true;
            }
            let _ = tx.send(Msg::Line(line));
            wake();
        }
    }
    if let Some(h) = err {
        let _ = h.join();
    }
    match child.wait() {
        Ok(st) if st.success() && seen => true,
        Ok(st) if st.success() => {
            let _ = tx.send(Msg::Line(format!("no \"{}\" line: the PDF was not built", need.unwrap_or(""))));
            false
        }
        Ok(st) => {
            let _ = tx.send(Msg::Line(format!("exit status {}", st.code().map(|c| c.to_string()).unwrap_or("signal".into()))));
            false
        }
        Err(e) => {
            let _ = tx.send(Msg::Line(format!("wait failed: {e}")));
            false
        }
    }
}

/// Build (or rebuild) one case's note and PDF, on a thread; messages arrive on the returned job.
pub fn build_case(py: Py, r: i64, title: String, quick: bool, wake: impl Fn() + Send + Sync + 'static) -> Running {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let ok = (|| {
            // a case that already has its file (fetched from the API, or built before) is only
            // rebuilt: `get` would replace that file with the local database's version
            let exists = full_file(&py.work, r).is_some_and(|f| py.work.join("cases").join(f).is_file());
            if !exists
                && !run_step(&py, &["map".into(), "get".into(), "--cluster".into(), r.to_string()], None, &tx, &wake)
            {
                return false;
            }
            let Some(full) = full_file(&py.work, r) else {
                let _ = tx.send(Msg::Line("tribonian map get did not record a case file in cases/_index.json".into()));
                return false;
            };
            // Quick build: the quick good-law check (newest 100 citing opinions; a current full result is kept)
            let mut args: Vec<String> = vec!["case".into(), "build".into()];
            if quick {
                args.push("--quick-goodlaw".into());
            }
            args.push(format!("cases/{full}"));
            run_step(&py, &args, Some("wrote "), &tx, &wake)
        })();
        let _ = tx.send(Msg::Done { ok });
        wake();
    });
    Running { title, r#ref: r, build: true, rx }
}

/// Run one command for a case (args after `tribonian`, e.g. map coupling --cluster N),
/// on a thread; done when it exits 0.
pub fn run_script(py: Py, r: i64, title: String, args: Vec<String>, wake: impl Fn() + Send + Sync + 'static) -> Running {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let ok = run_step(&py, &args, None, &tx, &wake);
        let _ = tx.send(Msg::Done { ok });
        wake();
    });
    Running { title, r#ref: r, build: false, rx }
}

/// Run several commands in order, on one thread; stops at the first failure. Each step is
/// (args after `tribonian`, a line its success must print or None).
pub fn run_steps(py: Py, r: i64, title: String, steps: Vec<(Vec<String>, Option<String>)>, wake: impl Fn() + Send + Sync + 'static) -> Running {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let ok = steps.iter().all(|(args, need)| run_step(&py, args, need.as_deref(), &tx, &wake));
        let _ = tx.send(Msg::Done { ok });
        wake();
    });
    Running { title, r#ref: r, build: false, rx }
}
