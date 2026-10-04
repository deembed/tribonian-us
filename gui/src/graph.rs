//! An area's lineages drawn like `git log --graph`: one lane per lineage while it lives,
//! time running down the page (or up, with "newest at top"). Dots are a lineage's
//! sub-cluster in one slice, sized by its case count; curves are `links` rows between
//! different lineages (a case group joining or splitting off), faded by Jaccard.
//!
//! Lane rule (kept simple on purpose): lineages are placed oldest first; each takes the
//! free lane nearest its parent's lane, the parent being the lineage with the strongest
//! link into its first slice. A lane frees up after its lineage's last slice.

use crate::model::{GraphData, LineageRow, Page};
use eframe::egui::{self, epaint::CubicBezierShape, Align2, Color32, FontId, Pos2, Rect, Sense, Stroke, Ui, Vec2};
use std::collections::HashMap;

#[derive(Clone, Debug)]
pub struct GraphOpts {
    pub min_jaccard: f32,
    pub min_size: i64,
    pub newest_top: bool,
}

impl Default for GraphOpts {
    fn default() -> Self {
        GraphOpts { min_jaccard: 0.10, min_size: 10, newest_top: false }
    }
}

const PALETTE: [Color32; 8] = [
    Color32::from_rgb(86, 156, 214),
    Color32::from_rgb(220, 150, 70),
    Color32::from_rgb(120, 190, 110),
    Color32::from_rgb(200, 110, 160),
    Color32::from_rgb(170, 140, 220),
    Color32::from_rgb(90, 190, 190),
    Color32::from_rgb(215, 200, 90),
    Color32::from_rgb(220, 100, 100),
];

struct Lin<'a> {
    row: &'a LineageRow,
    cells: Vec<(i64, i64, i64)>, // slice, sub code, size
    lane: usize,
    color: Color32,
}

fn radius(size: i64) -> f32 {
    (2.0 + (size as f32).sqrt() / 3.0).min(9.0)
}

/// Draw the graph; returns a page to open when a dot is clicked.
pub fn area_graph(
    ui: &mut Ui,
    opts: &mut GraphOpts,
    all_slices: &[i64],
    lineages: &[LineageRow],
    g: &GraphData,
    slice_label: &dyn Fn(i64) -> String,
) -> Option<Page> {
    // ---------------------------------------------------------------- controls
    ui.horizontal(|ui| {
        ui.label("Lines at Jaccard ≥");
        ui.add(egui::Slider::new(&mut opts.min_jaccard, 0.0..=0.6).step_by(0.01).fixed_decimals(2));
        ui.separator();
        ui.label("Lineages reaching ≥");
        ui.add(egui::Slider::new(&mut opts.min_size, 1..=1000).logarithmic(true).suffix(" cases"));
        ui.separator();
        ui.checkbox(&mut opts.newest_top, "Newest at top");
    });

    // ---------------------------------------------------------------- lineages kept
    let info: HashMap<i64, &LineageRow> = lineages.iter().map(|l| (l.lineage, l)).collect();
    let mut cells_of: HashMap<i64, Vec<(i64, i64, i64)>> = HashMap::new();
    for c in &g.cells {
        cells_of.entry(c.lineage).or_default().push((c.slice, c.k, c.size));
    }
    let mut lins: Vec<Lin> = Vec::new();
    for (lin, mut cells) in cells_of {
        let Some(row) = info.get(&lin) else { continue };
        if cells.iter().map(|c| c.2).max().unwrap_or(0) < opts.min_size {
            continue;
        }
        cells.sort();
        let color = if row.labelled { PALETTE[(lin as usize) % PALETTE.len()] } else { Color32::from_gray(120) };
        lins.push(Lin { row, cells, lane: 0, color });
    }
    let total = lineages.len();
    if lins.is_empty() {
        ui.weak("No lineage in this area reaches that size.");
        return None;
    }
    // cell -> lineage index, for edges
    let mut at: HashMap<(i64, i64), usize> = HashMap::new();
    lins.sort_by(|a, b| {
        a.cells[0].0.cmp(&b.cells[0].0).then(b.row.weight.total_cmp(&a.row.weight))
    });
    for (i, l) in lins.iter().enumerate() {
        for c in &l.cells {
            at.insert((c.0, c.1), i);
        }
    }
    let edges: Vec<(usize, (i64, i64), usize, (i64, i64), f64)> = g
        .edges
        .iter()
        .filter(|e| e.jaccard >= opts.min_jaccard as f64)
        .filter_map(|e| {
            let a = *at.get(&e.from)?;
            let b = *at.get(&e.to)?;
            (a != b).then_some((a, e.from, b, e.to, e.jaccard))
        })
        .collect();

    // ---------------------------------------------------------------- lanes
    let mut parent: HashMap<usize, (usize, f64)> = HashMap::new();
    for &(a, _, b, to, j) in &edges {
        if lins[b].cells[0].0 == to.0 && parent.get(&b).is_none_or(|p| j > p.1) {
            parent.insert(b, (a, j));
        }
    }
    let mut lane_end: Vec<i64> = Vec::new(); // last slice using each lane
    for i in 0..lins.len() {
        let first = lins[i].cells[0].0;
        let last = lins[i].cells.last().unwrap().0;
        let want = parent.get(&i).map(|p| lins[p.0].lane).unwrap_or(0);
        let free = (0..lane_end.len())
            .filter(|&k| lane_end[k] < first)
            .min_by_key(|&k| (k as i64 - want as i64).abs());
        let lane = match free {
            Some(k) => k,
            None => {
                lane_end.push(0);
                lane_end.len() - 1
            }
        };
        lane_end[lane] = last;
        lins[i].lane = lane;
    }
    let lanes = lane_end.len();

    // ---------------------------------------------------------------- geometry
    let lo = lins.iter().map(|l| l.cells[0].0).min().unwrap();
    let hi = lins.iter().map(|l| l.cells.last().unwrap().0).max().unwrap();
    let mut rows: Vec<i64> = all_slices.iter().copied().filter(|&s| s >= lo && s <= hi).collect();
    if opts.newest_top {
        rows.reverse();
    }
    let row_of: HashMap<i64, usize> = rows.iter().enumerate().map(|(i, s)| (*s, i)).collect();
    let (gutter, dx, dy, pad) = (70.0, 20.0, 28.0, 22.0);
    let markers_count = g.outside.iter().filter(|o| o.jaccard >= opts.min_jaccard as f64 && at.contains_key(&o.here)).count();
    ui.weak(format!(
        "{} of {} lineages shown, in {} lanes · {} lines inside the area, {} to lineages outside it (hollow markers) · hover for names, click to open",
        lins.len(),
        total,
        lanes,
        edges.len(),
        markers_count
    ));
    let text_w = 560.0; // right of the lanes: names of the labelled lineages that start in each row
    let size = Vec2::new(gutter + lanes as f32 * dx + 2.0 * pad + text_w, rows.len() as f32 * dy + 2.0 * pad);
    let (rect, resp) = ui.allocate_exact_size(size, Sense::click());
    let painter = ui.painter_at(rect);
    let pos = |slice: i64, lane: usize| -> Pos2 {
        Pos2::new(
            rect.left() + gutter + pad + lane as f32 * dx,
            rect.top() + pad + row_of.get(&slice).copied().unwrap_or(0) as f32 * dy,
        )
    };

    let text_col = ui.visuals().weak_text_color();
    let grid_col = ui.visuals().faint_bg_color;
    for (i, s) in rows.iter().enumerate() {
        let y = rect.top() + pad + i as f32 * dy;
        if i % 2 == 0 {
            painter.rect_filled(
                Rect::from_min_max(Pos2::new(rect.left(), y - dy / 2.0), Pos2::new(rect.right(), y + dy / 2.0)),
                0.0,
                grid_col,
            );
        }
        painter.text(Pos2::new(rect.left() + 4.0, y), Align2::LEFT_CENTER, slice_label(*s), FontId::proportional(11.0), text_col);
    }

    // like a commit message: the labelled lineages starting in each row, in their lane colours
    let names_x = rect.left() + gutter + pad + lanes as f32 * dx + 8.0;
    let mut starts: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, l) in lins.iter().enumerate() {
        if l.row.labelled {
            starts.entry(l.cells[0].0).or_default().push(i);
        }
    }
    for (s, ids) in &starts {
        let y = pos(*s, 0).y;
        let mut x = names_x;
        for &i in ids {
            if x > rect.right() - 40.0 {
                painter.text(Pos2::new(x, y), Align2::LEFT_CENTER, "…", FontId::proportional(12.0), text_col);
                break;
            }
            let name: String = lins[i].row.name.chars().take(48).collect();
            let r = painter.text(Pos2::new(x, y), Align2::LEFT_CENTER, name, FontId::proportional(12.0), lins[i].color);
            x = r.right() + 14.0;
        }
    }

    // links to lineages outside the area: a short stub to a hollow marker, above the dot when
    // the cases came in from an earlier slice, below when they left for a later one
    let earlier_up = if opts.newest_top { 1.0 } else { -1.0 };
    let mut fan: HashMap<((i64, i64), bool), f32> = HashMap::new();
    let mut markers: Vec<(Pos2, Pos2, usize)> = Vec::new(); // marker, dot, index into g.outside
    for (oi, o) in g.outside.iter().enumerate() {
        if o.jaccard < opts.min_jaccard as f64 {
            continue;
        }
        let Some(&li) = at.get(&o.here) else { continue };
        let dot = pos(o.here.0, lins[li].lane);
        let k = fan.entry((o.here, o.incoming)).or_insert(0.0);
        let dir = if o.incoming { earlier_up } else { -earlier_up };
        let m = Pos2::new(dot.x + 8.0 + *k * 7.0, dot.y + dir * dy * 0.55);
        *k += 1.0;
        markers.push((m, dot, oi));
    }
    let mut hover_out: Option<usize> = None;
    if let Some(p) = resp.hover_pos() {
        hover_out = markers
            .iter()
            .filter(|m| m.0.distance(p) <= 6.0)
            .min_by(|a, b| a.0.distance(p).total_cmp(&b.0.distance(p)))
            .map(|m| m.2);
    }

    // hovered dot (nearest within reach)
    let hover = resp.hover_pos().filter(|_| hover_out.is_none()).and_then(|p| {
        let mut best: Option<(usize, usize, f32)> = None;
        for (i, l) in lins.iter().enumerate() {
            for (ci, c) in l.cells.iter().enumerate() {
                let d = pos(c.0, l.lane).distance(p);
                if d <= radius(c.2) + 5.0 && best.is_none_or(|b| d < b.2) {
                    best = Some((i, ci, d));
                }
            }
        }
        best.map(|b| (b.0, b.1))
    });
    let hl = hover.map(|h| h.0);

    // edges between lineages
    for &(a, from, b, to, j) in &edges {
        let p0 = pos(from.0, lins[a].lane);
        let p3 = pos(to.0, lins[b].lane);
        let mid = (p0.y + p3.y) / 2.0;
        let lit = hl == Some(a) || hl == Some(b);
        let alpha = if lit { 230 } else { (60.0 + 400.0 * j).min(200.0) as u8 };
        let col = lins[if from.0 < to.0 { b } else { a }].color;
        let col = Color32::from_rgba_unmultiplied(col.r(), col.g(), col.b(), alpha);
        painter.add(CubicBezierShape::from_points_stroke(
            [p0, Pos2::new(p0.x, mid), Pos2::new(p3.x, mid), p3],
            false,
            Color32::TRANSPARENT,
            Stroke::new(if lit { 2.0 } else { 1.0 }, col),
        ));
    }
    // outside stubs
    let stub_col = ui.visuals().weak_text_color();
    for &(m, dot, oi) in &markers {
        let lit = hover_out == Some(oi) || at.get(&g.outside[oi].here).is_some_and(|&li| hl == Some(li));
        let col = if lit { ui.visuals().strong_text_color() } else { stub_col };
        painter.line_segment([dot, m], Stroke::new(1.0, col));
        painter.circle_stroke(m, 3.0, Stroke::new(if lit { 2.0 } else { 1.2 }, col));
    }
    // lanes and dots
    for (i, l) in lins.iter().enumerate() {
        let lit = hl == Some(i);
        let w = if lit { 4.0 } else { 2.0 };
        for pair in l.cells.windows(2) {
            painter.line_segment([pos(pair[0].0, l.lane), pos(pair[1].0, l.lane)], Stroke::new(w, l.color));
        }
        for c in &l.cells {
            let p = pos(c.0, l.lane);
            painter.circle_filled(p, radius(c.2), l.color);
            if lit {
                painter.circle_stroke(p, radius(c.2) + 2.0, Stroke::new(1.5, ui.visuals().strong_text_color()));
            }
        }
    }

    if let Some(oi) = hover_out {
        let o = &g.outside[oi];
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
        let head = format!(
            "{} {}",
            if o.incoming { "Cases came in from" } else { "Cases went on to" },
            o.other_name
        );
        let line2 = format!(
            "{} · lineage {} · Jaccard {:.2}{}",
            slice_label(o.other_slice),
            o.other_lineage,
            o.jaccard,
            o.other_area.as_ref().map(|a| format!(" · area: {a}")).unwrap_or_else(|| " · in no area".into())
        );
        resp.clone().on_hover_ui_at_pointer(|ui| {
            ui.strong(head);
            ui.label(line2);
            ui.weak("click to open that lineage");
        });
        if resp.clicked() {
            return Some(Page::Lineage(o.other_lineage));
        }
    }
    if let Some((i, ci)) = hover {
        let l = &lins[i];
        let c = l.cells[ci];
        let (first, last) = (l.cells[0].0, l.cells.last().unwrap().0);
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
        let name = l.row.name.clone();
        let line2 = format!(
            "{}: {} cases · lineage {} ({} – {})",
            slice_label(c.0),
            c.2,
            l.row.lineage,
            slice_label(first),
            slice_label(last)
        );
        resp.clone().on_hover_ui_at_pointer(|ui| {
            ui.strong(name);
            ui.label(line2);
        });
        if resp.clicked() {
            return Some(Page::Lineage(l.row.lineage));
        }
    }
    None
}
