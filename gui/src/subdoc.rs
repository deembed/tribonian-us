//! "Explain this sub-cluster" on sub-cluster pages (and unsplit clusters): queues the repo's
//! `cocite.py subdoc <slice:sub> --view <view>` through the build queue, then opens what it wrote:
//! md-cocite/Sub-clusters/[<view>/]<slice>/<slice> <code> - <name>.md and the same under pdf/Co-citation/.
//! The file names carry the label, so they are found by their "<slice> <code> - " prefix.

use crate::cases::open_external;
use crate::App;
use eframe::egui::{self, Ui};
use std::path::{Path, PathBuf};

/// The view case notes use; its files sit directly under the slice folders.
pub const NOTE_VIEW: &str = "appellate-sentence-m0.2";

/// "cocite-appellate-sentence-m0.2" -> "appellate-sentence-m0.2"; "cocite" (all courts) -> "all".
pub fn view_name(stem: &str) -> String {
    stem.strip_prefix("cocite-").map(String::from).unwrap_or_else(|| "all".into())
}

/// Build-queue key: negative, clear of case ids, docket keys (-1e9 ..) and `recent` (-1).
pub fn key(slice: i64, sub: i64) -> i64 {
    -(2_000_000_000 + slice * 1_000_000 + sub)
}

/// "2.3" for a sub-cluster, "2" for an unsplit cluster's only one (as cocite.py's sub_label).
pub fn code(sub: i64) -> String {
    if sub % 1000 == 0 { format!("{}", sub / 1000) } else { format!("{}.{}", sub / 1000, sub % 1000) }
}

fn rel_dir(view: &str, slice_label: &str) -> PathBuf {
    let mut p = PathBuf::new();
    if view != NOTE_VIEW {
        p.push(view);
    }
    p.push(slice_label);
    p
}

fn find(dir: &Path, prefix: &str, ext: &str) -> Option<PathBuf> {
    let mut hits: Vec<PathBuf> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.extension().is_some_and(|x| x == ext)
                && p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with(prefix))
        })
        .collect();
    hits.sort_by_key(|p| std::fs::metadata(p).and_then(|m| m.modified()).ok());
    hits.pop() // newest, if a label rename left an older one
}

/// Buttons on a sub-cluster page: Explain, Explain + good law, Open PDF, Open note.
pub fn bar(app: &mut App, ui: &mut Ui, slice: i64, sub: i64) {
    let Some(cfg) = app.cfg.clone() else { return };
    let Some(view) = app.view_sel.and_then(|i| app.views.get(i)).map(|v| view_name(&v.stem)) else { return };
    let sl = app.slice_label(slice);
    let label = format!("{sl}:{}", code(sub));
    let prefix = format!("{sl} {} - ", code(sub));
    let rel = rel_dir(&view, &sl);
    let md_dir = cfg.work_dir.join("md-cocite").join("Sub-clusters").join(&rel);
    let pdf = find(&cfg.work_dir.join("pdf").join("Co-citation").join(&rel), &prefix, "pdf");
    let md = find(&md_dir, &prefix, "md");
    let k = key(slice, sub);
    let queued = app.is_queued(k);
    ui.horizontal(|ui| {
        let args = |gl: bool| {
            let mut a = vec!["cocite".to_string(), "subdoc".into(), label.clone(), "--view".into(), view.clone()];
            if gl {
                a.push("--goodlaw".into());
            }
            a
        };
        let again = if pdf.is_some() { " again" } else { "" };
        let b = ui
            .add_enabled(!queued, egui::Button::new(format!("Explain{again} (note + PDF)")))
            .on_hover_text(format!(
                "Runs cocite.py subdoc {label} --view {view}: every member case with how often it is cited, its \
                 parentheticals, good law (cached; grey when dormant), the lineage's joins and departures. \
                 Writes md-cocite/Sub-clusters/ and pdf/Co-citation/."
            ))
            .on_disabled_hover_text("in the build queue");
        if b.clicked() {
            app.enqueue_steps(k, format!("Explain {label}"), vec![(args(false), Some("wrote pdf/".into()))]);
        }
        let b = ui
            .add_enabled(!queued, egui::Button::new("Explain + good law"))
            .on_hover_text(
                "The same, after the quick good-law check (the newest 100 citing opinions each) on every member with no \
                 result cached; cached after. For the full check with years: cocite.py subdoc ... --goodlaw full.",
            )
            .on_disabled_hover_text("in the build queue");
        if b.clicked() {
            app.enqueue_steps(k, format!("Explain {label} + good law"), vec![(args(true), Some("wrote pdf/".into()))]);
        }
        match &pdf {
            Some(p) => {
                if ui.button("Open PDF").on_hover_text(p.to_string_lossy()).clicked() {
                    open_external(&p.to_string_lossy());
                }
            }
            None => {
                ui.add_enabled(false, egui::Button::new("Open PDF")).on_disabled_hover_text("not explained yet");
            }
        }
        if let Some(m) = &md {
            // sync_vault.sh mirrors md-cocite/ to the folder beside the case notes' (08-Reference/Co-citation)
            let url = cfg.obsidian_vault.as_ref().map(|v| {
                let base = Path::new(cfg.obsidian_cases_dir.trim_end_matches('/'))
                    .parent()
                    .map(|p| p.join("Co-citation"))
                    .unwrap_or_else(|| PathBuf::from("Co-citation"));
                let file = base.join("Sub-clusters").join(&rel).join(m.file_stem().unwrap_or_default());
                format!("obsidian://open?vault={}&file={}", pct(v), pct(&file.to_string_lossy()))
            });
            match url {
                Some(u) => {
                    if ui.button("Open note in Obsidian").on_hover_text(format!("{u}\n(after sync_vault.sh)")).clicked() {
                        open_external(&u);
                    }
                }
                None => {
                    if ui.button("Open note file").clicked() {
                        open_external(&m.to_string_lossy());
                    }
                }
            }
        }
        if queued {
            ui.weak("in the build queue");
        }
    });
}

fn pct(s: &str) -> String {
    let mut o = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => o.push(b as char),
            _ => o.push_str(&format!("%{b:02X}")),
        }
    }
    o
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_keys() {
        assert_eq!(view_name("cocite-appellate-sentence-m0.2"), "appellate-sentence-m0.2");
        assert_eq!(view_name("cocite"), "all");
        assert_eq!(code(2003), "2.3");
        assert_eq!(code(7000), "7");
        assert_eq!(rel_dir(NOTE_VIEW, "2020-24"), PathBuf::from("2020-24"));
        assert_eq!(rel_dir("appellate", "2020-24"), PathBuf::from("appellate/2020-24"));
        assert!(key(2025, 999_999) < -1_000_000_000 - 99 * 100_000 - 99_999);
        assert_ne!(key(2020, 2003), key(2020, 2004));
    }
}
