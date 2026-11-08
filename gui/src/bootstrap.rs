//! The Sync page's first row: put the Python pipeline on a machine that has only this app.
//!
//! 1. uv: already in the app's folder, else on PATH, else Astral's installer, run into the app's folder
//!    (UV_INSTALL_DIR) without touching shell profiles.
//! 2. `uv tool install --force` of the wheel published with this app's GitHub release (same version as this app), into
//!    the app's folder (UV_TOOL_DIR). uv brings its own Python when the machine has none.
//! 3. Check: the installed `python -m tribonian --version` names this app's version.
//!
//! After that every Sync row goes through `tribonian` subcommands, so the same steps work from the command line.
//! TRIBONIAN_WHEEL overrides the wheel (a local file or another URL), for testing a build before its release.

use crate::config::{app_data_dir, installed_python, quiet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc::{channel, Receiver, Sender};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const RELEASES: &str = "https://github.com/deembed/tribonian-us/releases";

pub enum Msg {
    Line(String),
    Done(Result<(), String>),
}

pub fn wheel() -> String {
    std::env::var("TRIBONIAN_WHEEL")
        .unwrap_or_else(|_| format!("{RELEASES}/download/v{VERSION}/tribonian_us-{VERSION}-py3-none-any.whl"))
}

/// The version the installed tribonian reports, or None when it doesn't run.
pub fn installed_version() -> Option<String> {
    let o = quiet(&mut Command::new(installed_python())).args(["-m", "tribonian", "--version"]).output().ok()?;
    let t = String::from_utf8_lossy(&o.stdout);
    t.trim().strip_prefix("tribonian ").map(str::to_string)
}

pub fn spawn(wake: impl Fn() + Send + 'static) -> Receiver<Msg> {
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        let r = run(&tx);
        let _ = tx.send(Msg::Done(r));
        wake();
    });
    rx
}

fn run(tx: &Sender<Msg>) -> Result<(), String> {
    let data = app_data_dir();
    std::fs::create_dir_all(&data).map_err(|e| format!("{}: {e}", data.display()))?;
    let uv = match find_uv(&data) {
        Some(uv) => uv,
        None => install_uv(&data, tx)?,
    };
    let _ = tx.send(Msg::Line(format!("uv: {}", uv.display())));
    let w = wheel();
    let _ = tx.send(Msg::Line(format!("installing tribonian {VERSION} from {w}")));
    let mut c = Command::new(&uv);
    c.args(["tool", "install", "--force", "--python", "3.12", &w])
        .env("UV_TOOL_DIR", data.join("tools"))
        .env("UV_TOOL_BIN_DIR", data.join("bin"));
    if let Err((e, lines)) = logged(c, tx) {
        // the release this app belongs to isn't published (yet, or as a draft): say so instead of uv's 404
        if std::env::var("TRIBONIAN_WHEEL").is_err() && lines.iter().any(|l| l.contains("404")) {
            return Err(format!(
                "no published release for {VERSION} yet ({RELEASES}): publish it, or set TRIBONIAN_WHEEL to a wheel \
                 file (for example from a release workflow run's artifacts) to install one by hand"
            ));
        }
        return Err(e);
    }
    match installed_version() {
        Some(v) if v == VERSION => {
            let _ = tx.send(Msg::Line(format!("tribonian {v} is installed")));
            Ok(())
        }
        Some(v) => Err(format!("installed tribonian {v}, but this app is {VERSION}")),
        None => Err(format!("{} -m tribonian didn't run after the install", installed_python().display())),
    }
}

fn uv_name() -> &'static str {
    if cfg!(windows) { "uv.exe" } else { "uv" }
}

fn find_uv(data: &Path) -> Option<PathBuf> {
    for p in [data.join("uv").join(uv_name()), data.join("uv").join("bin").join(uv_name())] {
        if p.is_file() {
            return Some(p);
        }
    }
    let on_path = quiet(&mut Command::new("uv")).arg("--version").output().is_ok_and(|o| o.status.success());
    on_path.then(|| PathBuf::from("uv"))
}

fn install_uv(data: &Path, tx: &Sender<Msg>) -> Result<PathBuf, String> {
    let dir = data.join("uv");
    let _ = tx.send(Msg::Line(format!("installing uv into {}", dir.display())));
    let mut c = if cfg!(windows) {
        let mut c = Command::new("powershell");
        c.args(["-NoProfile", "-ExecutionPolicy", "ByPass", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"]);
        c
    } else {
        let mut c = Command::new("sh");
        c.args(["-c", "if command -v curl >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh; \
                       else wget -qO- https://astral.sh/uv/install.sh; fi | sh"]);
        c
    };
    c.env("UV_INSTALL_DIR", &dir).env("UV_NO_MODIFY_PATH", "1").env("INSTALLER_NO_MODIFY_PATH", "1");
    logged(c, tx).map_err(|(e, _)| e)?;
    find_uv(data).filter(|p| p.is_absolute()).ok_or_else(|| format!("uv's installer ran, but there is no {} in {}", uv_name(), dir.display()))
}

/// Run c, sending each line of its output (stdout and stderr) as it comes. Err carries the last lines, so the
/// caller can tell what went wrong.
fn logged(mut c: Command, tx: &Sender<Msg>) -> Result<(), (String, Vec<String>)> {
    use std::sync::{Arc, Mutex};
    quiet(&mut c);
    let mut child = c.stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e| (format!("{c:?}: {e}"), vec![]))?;
    let tail = Arc::new(Mutex::new(std::collections::VecDeque::<String>::new()));
    let keep = |tail: &Arc<Mutex<std::collections::VecDeque<String>>>, l: &str| {
        let mut t = tail.lock().unwrap();
        t.push_back(l.to_string());
        if t.len() > 40 {
            t.pop_front();
        }
    };
    let err = child.stderr.take().map(|e| {
        let (tx, tail) = (tx.clone(), tail.clone());
        std::thread::spawn(move || {
            crate::jobs::each_line(e, |l| {
                keep(&tail, &l);
                let _ = tx.send(Msg::Line(l));
            })
        })
    });
    if let Some(o) = child.stdout.take() {
        crate::jobs::each_line(o, |l| {
            keep(&tail, &l);
            let _ = tx.send(Msg::Line(l));
        });
    }
    if let Some(h) = err {
        let _ = h.join();
    }
    let st = child.wait().map_err(|e| (e.to_string(), vec![]))?;
    let lines: Vec<String> = tail.lock().unwrap().iter().cloned().collect();
    if st.success() { Ok(()) } else { Err((format!("{:?} exited with {st}", c.get_program()), lines)) }
}
