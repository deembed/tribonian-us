//! v2 actions: run the repo's own scripts for one case and stream their output into a log panel.
//! The app itself still writes nothing; the scripts do, exactly as they would from a terminal.
//!
//! Build = `casemap.py get --cluster N` (case JSON and note, from the local database; skipped when
//! the case file already exists), then `case2md.py build cases/<file>` (note, PDF, and the case
//! profile with good-law status). Quick build passes --no-goodlaw: the same minus the slow
//! good-law check, which reads up to ~2,000 citing opinions.
//!
//! The same queue also runs single script commands for a case (`run_script`): "Find coupled cases"
//! queues `casemap.py coupling --cluster N`, which writes cases/citedby/<N>/coupling.json.
//!
//! And a run of commands that stops at the first failure (`run_steps`): the SCOTUS page's
//! "Fetch & build" queues `scotus.py fetch <docket>` then `scotus.py build <docket>` as one job.

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
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

/// The case's JSON file as cases/_index.json names it (written by `casemap.py get`).
fn full_file(repo: &Path, r: i64) -> Option<String> {
    let text = std::fs::read_to_string(repo.join("cases").join("_index.json")).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v[r.to_string()]["full"].as_str().filter(|s| !s.is_empty()).map(String::from)
}

/// Run one script; true when it exits 0 and, if `need` is given, printed a line starting with it
/// (case2md.py build prints "wrote pdf/..." only when pdflatex succeeded, and exits 0 either way,
/// so build_all.sh checks for that line too).
fn run_step(python: &str, repo: &Path, args: &[String], need: Option<&str>, tx: &Sender<Msg>, wake: &(dyn Fn() + Send + Sync)) -> bool {
    let mut seen = need.is_none();
    let _ = tx.send(Msg::Line(format!("$ {python} {}", args.join(" "))));
    let child = Command::new(python)
        .args(args)
        .current_dir(repo)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(Msg::Line(format!("could not start {python}: {e}")));
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
pub fn build_case(python: String, repo: PathBuf, r: i64, title: String, quick: bool, wake: impl Fn() + Send + Sync + 'static) -> Running {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let ok = (|| {
            // a case that already has its file (fetched from the API, or built before) is only
            // rebuilt: `get` would replace that file with the local database's version
            let exists = full_file(&repo, r).is_some_and(|f| repo.join("cases").join(f).is_file());
            if !exists
                && !run_step(&python, &repo, &["casemap.py".into(), "get".into(), "--cluster".into(), r.to_string()], None, &tx, &wake)
            {
                return false;
            }
            let Some(full) = full_file(&repo, r) else {
                let _ = tx.send(Msg::Line("casemap.py get did not record a case file in cases/_index.json".into()));
                return false;
            };
            // Quick build: everything but the good-law check (case2md.py --no-goodlaw keeps an earlier result)
            let mut args: Vec<String> = vec!["case2md.py".into(), "build".into()];
            if quick {
                args.push("--no-goodlaw".into());
            }
            args.push(format!("cases/{full}"));
            run_step(&python, &repo, &args, Some("wrote "), &tx, &wake)
        })();
        let _ = tx.send(Msg::Done { ok });
        wake();
    });
    Running { title, r#ref: r, build: true, rx }
}

/// Run one script command for a case (args after the interpreter, e.g. casemap.py coupling --cluster N),
/// on a thread; done when it exits 0.
pub fn run_script(python: String, repo: PathBuf, r: i64, title: String, args: Vec<String>, wake: impl Fn() + Send + Sync + 'static) -> Running {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let ok = run_step(&python, &repo, &args, None, &tx, &wake);
        let _ = tx.send(Msg::Done { ok });
        wake();
    });
    Running { title, r#ref: r, build: false, rx }
}

/// Run several commands in order, on one thread; stops at the first failure. Each step is
/// (args after the interpreter, a line its success must print or None).
pub fn run_steps(python: String, repo: PathBuf, r: i64, title: String, steps: Vec<(Vec<String>, Option<String>)>, wake: impl Fn() + Send + Sync + 'static) -> Running {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let ok = steps.iter().all(|(args, need)| run_step(&python, &repo, args, need.as_deref(), &tx, &wake));
        let _ = tx.send(Msg::Done { ok });
        wake();
    });
    Running { title, r#ref: r, build: false, rx }
}
