//! An area's lineages drawn like `git log --graph`: one lane per lineage while it lives,
//! time running down the page (or up, with "newest at top"), or left to right. Dots are a
//! lineage's sub-cluster in one slice, sized by its case count; curves are `links` rows between
//! different lineages (a case group joining or splitting off), faded by Jaccard.
//!
//! Lane rule (kept simple on purpose): lineages are placed oldest first; each takes the
//! free lane nearest its parent's lane, the parent being the lineage with the strongest
//! link into its first slice. A lane frees up after its lineage's last slice.
//!
//! The work is split in two so both orientations (and the SVG export) share one answer:
//! [`Layout`] holds everything in abstract units (time index along, lane index across), and
//! [`Geom`] maps those units to pixels for one orientation. The egui painter and the SVG
//! writer both draw from the same Layout + Geom.

use crate::model::{GraphData, LineageRow, Page};
use eframe::egui::{self, epaint::CubicBezierShape, Align2, Color32, FontId, Pos2, Rect, Sense, Stroke, Ui, Vec2};
use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Orient {
    TopToBottom,
    LeftToRight,
}

impl Orient {
    /// Used in the export's file name.
    fn slug(self) -> &'static str {
        match self {
            Orient::TopToBottom => "top-to-bottom",
            Orient::LeftToRight => "left-to-right",
        }
    }
}

#[derive(Clone, Debug)]
pub struct GraphOpts {
    pub min_jaccard: f32,
    pub min_size: i64,
    /// Newest slice first: at the top (top to bottom) or at the left (left to right).
    pub newest_top: bool,
    pub orient: Orient,
    /// Outcome of the last Export SVG, shown beside the button.
    pub export_msg: Option<String>,
}

impl Default for GraphOpts {
    fn default() -> Self {
        GraphOpts { min_jaccard: 0.10, min_size: 10, newest_top: false, orient: Orient::TopToBottom, export_msg: None }
    }
}

/// What the export needs that the graph data doesn't carry: where to write and what to call it.
pub struct ExportInfo {
    /// `<work_dir>/build/exports`.
    pub dir: PathBuf,
    pub area_code: String,
    pub area_name: String,
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

/// Longest lineage name drawn on the graph (the tooltip has the full one).
const NAME_CHARS: usize = 48;

struct Lin<'a> {
    row: &'a LineageRow,
    cells: Vec<(i64, i64, i64)>, // slice, sub code, size
    lane: usize,
    color: Color32,
}

/// A curve between two lineages: (lineage a, its slice), (lineage b, its slice), Jaccard.
struct Edge {
    a: usize,
    from: i64,
    b: usize,
    to: i64,
    j: f64,
}

/// A link to a lineage outside the area: a short stub from a dot to a hollow marker, toward
/// the earlier slice when the cases came in, toward the later one when they left.
struct Marker {
    li: usize,
    slice: i64,
    /// +1 or -1 along the time axis (in time-index terms, so it follows "newest first").
    dir: f32,
    /// k-th marker on the same side of the same dot: fanned out across.
    fan: f32,
    /// Index into `GraphData::outside`.
    oi: usize,
}

/// Lanes, dots, curves and markers in abstract units; no pixels, no orientation.
struct Layout<'a> {
    lins: Vec<Lin<'a>>,
    edges: Vec<Edge>,
    markers: Vec<Marker>,
    /// Slices along the time axis, in drawing order.
    slices: Vec<i64>,
    t_of: HashMap<i64, usize>,
    lanes: usize,
    /// (sub-cluster cell) -> lineage index
    at: HashMap<(i64, i64), usize>,
}

impl<'a> Layout<'a> {
    fn build(opts: &GraphOpts, all_slices: &[i64], lineages: &'a [LineageRow], g: &GraphData) -> Option<Layout<'a>> {
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
        if lins.is_empty() {
            return None;
        }
        lins.sort_by(|a, b| a.cells[0].0.cmp(&b.cells[0].0).then(b.row.weight.total_cmp(&a.row.weight)));
        let mut at: HashMap<(i64, i64), usize> = HashMap::new();
        for (i, l) in lins.iter().enumerate() {
            for c in &l.cells {
                at.insert((c.0, c.1), i);
            }
        }
        let edges: Vec<Edge> = g
            .edges
            .iter()
            .filter(|e| e.jaccard >= opts.min_jaccard as f64)
            .filter_map(|e| {
                let a = *at.get(&e.from)?;
                let b = *at.get(&e.to)?;
                (a != b).then_some(Edge { a, from: e.from.0, b, to: e.to.0, j: e.jaccard })
            })
            .collect();

        // lanes
        let mut parent: HashMap<usize, (usize, f64)> = HashMap::new();
        for e in &edges {
            if lins[e.b].cells[0].0 == e.to && parent.get(&e.b).is_none_or(|p| e.j > p.1) {
                parent.insert(e.b, (e.a, e.j));
            }
        }
        let mut lane_end: Vec<i64> = Vec::new(); // last slice using each lane
        for i in 0..lins.len() {
            let first = lins[i].cells[0].0;
            let last = lins[i].cells.last().unwrap().0;
            let want = parent.get(&i).map(|p| lins[p.0].lane).unwrap_or(0);
            let free = (0..lane_end.len()).filter(|&k| lane_end[k] < first).min_by_key(|&k| (k as i64 - want as i64).abs());
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

        // time axis
        let lo = lins.iter().map(|l| l.cells[0].0).min().unwrap();
        let hi = lins.iter().map(|l| l.cells.last().unwrap().0).max().unwrap();
        let mut slices: Vec<i64> = all_slices.iter().copied().filter(|&s| s >= lo && s <= hi).collect();
        if opts.newest_top {
            slices.reverse();
        }
        let t_of: HashMap<i64, usize> = slices.iter().enumerate().map(|(i, s)| (*s, i)).collect();

        // outside markers
        let earlier = if opts.newest_top { 1.0 } else { -1.0 };
        let mut fan: HashMap<((i64, i64), bool), f32> = HashMap::new();
        let mut markers = Vec::new();
        for (oi, o) in g.outside.iter().enumerate() {
            if o.jaccard < opts.min_jaccard as f64 {
                continue;
            }
            let Some(&li) = at.get(&o.here) else { continue };
            let k = fan.entry((o.here, o.incoming)).or_insert(0.0);
            let dir = if o.incoming { earlier } else { -earlier };
            markers.push(Marker { li, slice: o.here.0, dir, fan: *k, oi });
            *k += 1.0;
        }
        Some(Layout { lins, edges, markers, slices, t_of, lanes: lane_end.len(), at })
    }

    fn t(&self, slice: i64) -> f32 {
        self.t_of.get(&slice).copied().unwrap_or(0) as f32
    }

    /// Labelled lineages by the slice they start in, in lineage order.
    fn starts(&self) -> Vec<(i64, Vec<usize>)> {
        let mut m: Vec<(i64, Vec<usize>)> = Vec::new();
        for (i, l) in self.lins.iter().enumerate() {
            if !l.row.labelled {
                continue;
            }
            let s = l.cells[0].0;
            match m.iter_mut().find(|e| e.0 == s) {
                Some(e) => e.1.push(i),
                None => m.push((s, vec![i])),
            }
        }
        m
    }
}

/// A name placed on the graph, in pixels relative to the graph's top-left corner.
/// `li == None` is the "…" that ends a full row.
struct NameSlot {
    li: Option<usize>,
    text: String,
    pos: Pos2,
}

/// Pixel mapping for one orientation, plus the overall size and where the names go.
struct Geom {
    orient: Orient,
    /// Pixel position of (time 0, lane 0), relative to the top-left corner.
    origin: Vec2,
    /// Pixels per time step and per lane.
    dt: f32,
    dl: f32,
    size: Vec2,
    names: Vec<NameSlot>,
}

const NAME_FONT: f32 = 12.0;
const LABEL_FONT: f32 = 11.0;

impl Geom {
    /// `width` measures a name at NAME_FONT; egui measures it, the SVG writer estimates it.
    fn new(orient: Orient, lay: &Layout, width: &dyn Fn(&str) -> f32) -> Geom {
        let pad = 22.0;
        let n_t = lay.slices.len() as f32;
        let mut names = Vec::new();
        match orient {
            Orient::TopToBottom => {
                // like a commit message: the labelled lineages starting in each row, to the right of the lanes
                let (gutter, dl, dt, text_w) = (70.0, 20.0, 28.0, 560.0);
                let origin = Vec2::new(gutter + pad, pad);
                let size = Vec2::new(gutter + lay.lanes as f32 * dl + 2.0 * pad + text_w, n_t * dt + 2.0 * pad);
                let names_x = origin.x + lay.lanes as f32 * dl + 8.0;
                for (s, ids) in lay.starts() {
                    let y = origin.y + lay.t(s) * dt;
                    let mut x = names_x;
                    for i in ids {
                        if x > size.x - 40.0 {
                            names.push(NameSlot { li: None, text: "…".into(), pos: Pos2::new(x, y) });
                            break;
                        }
                        let text: String = lay.lins[i].row.name.chars().take(NAME_CHARS).collect();
                        let w = width(&text);
                        names.push(NameSlot { li: Some(i), text, pos: Pos2::new(x, y) });
                        x += w + 14.0;
                    }
                }
                Geom { orient, origin, dt, dl, size, names }
            }
            Orient::LeftToRight => {
                // slice labels along the top; names in a band under the lanes, each starting under
                // its lineage's first dot, on the first text row where it doesn't run into another
                let (label_h, dl, dt, row_h) = (18.0, 20.0, 56.0, 15.0);
                let origin = Vec2::new(pad + 16.0, label_h + pad);
                let band_y = origin.y + (lay.lanes.max(1) - 1) as f32 * dl + pad;
                let mut row_end: Vec<f32> = Vec::new();
                let mut right = origin.x + (n_t - 1.0).max(0.0) * dt + dt / 2.0; // room for the last slice label
                let mut starts = lay.starts();
                starts.sort_by(|a, b| lay.t(a.0).total_cmp(&lay.t(b.0)));
                for (s, ids) in starts {
                    let x = origin.x + lay.t(s) * dt - 4.0;
                    for i in ids {
                        let text: String = lay.lins[i].row.name.chars().take(NAME_CHARS).collect();
                        let w = width(&text);
                        let row = match row_end.iter().position(|&e| e < x) {
                            Some(r) => r,
                            None => {
                                row_end.push(0.0);
                                row_end.len() - 1
                            }
                        };
                        row_end[row] = x + w + 12.0;
                        right = right.max(x + w + pad);
                        names.push(NameSlot { li: Some(i), text, pos: Pos2::new(x, band_y + row as f32 * row_h) });
                    }
                }
                let size = Vec2::new(right, band_y + row_end.len() as f32 * row_h + pad / 2.0);
                Geom { orient, origin, dt, dl, size, names }
            }
        }
    }

    /// Abstract (time, lane) to pixels, relative to `top_left`.
    fn at(&self, top_left: Pos2, t: f32, lane: f32) -> Pos2 {
        let o = top_left + self.origin;
        match self.orient {
            Orient::TopToBottom => Pos2::new(o.x + lane * self.dl, o.y + t * self.dt),
            Orient::LeftToRight => Pos2::new(o.x + t * self.dt, o.y + lane * self.dl),
        }
    }

    fn dot(&self, top_left: Pos2, lay: &Layout, slice: i64, lane: usize) -> Pos2 {
        self.at(top_left, lay.t(slice), lane as f32)
    }

    /// The hollow marker of an outside link: half a step along time, fanned out across.
    fn marker(&self, top_left: Pos2, lay: &Layout, m: &Marker) -> Pos2 {
        let lane = lay.lins[m.li].lane as f32 + (8.0 + m.fan * 7.0) / self.dl;
        let along = m.dir * 0.55 * 28.0 / self.dt; // same stub length in both orientations
        self.at(top_left, lay.t(m.slice) + along, lane)
    }

    /// The four control points of a curve between lineages: leave along time, cross over midway.
    fn curve(&self, top_left: Pos2, lay: &Layout, e: &Edge) -> [Pos2; 4] {
        let (t0, t3) = (lay.t(e.from), lay.t(e.to));
        let (l0, l3) = (lay.lins[e.a].lane as f32, lay.lins[e.b].lane as f32);
        let mid = (t0 + t3) / 2.0;
        [self.at(top_left, t0, l0), self.at(top_left, mid, l0), self.at(top_left, mid, l3), self.at(top_left, t3, l3)]
    }

    /// The band behind time step i (striped every other one), and where its label goes.
    fn stripe(&self, top_left: Pos2, i: usize) -> (Rect, Pos2, Align2) {
        let c = self.at(top_left, i as f32, 0.0);
        match self.orient {
            Orient::TopToBottom => (
                Rect::from_min_max(Pos2::new(top_left.x, c.y - self.dt / 2.0), Pos2::new(top_left.x + self.size.x, c.y + self.dt / 2.0)),
                Pos2::new(top_left.x + 4.0, c.y),
                Align2::LEFT_CENTER,
            ),
            Orient::LeftToRight => (
                Rect::from_min_max(Pos2::new(c.x - self.dt / 2.0, top_left.y), Pos2::new(c.x + self.dt / 2.0, top_left.y + self.size.y)),
                Pos2::new(c.x, top_left.y + 4.0),
                Align2::CENTER_TOP,
            ),
        }
    }
}

fn radius(size: i64) -> f32 {
    (2.0 + (size as f32).sqrt() / 3.0).min(9.0)
}

/// Draw the graph; returns a page to open when a dot or marker is clicked.
pub fn area_graph(
    ui: &mut Ui,
    opts: &mut GraphOpts,
    all_slices: &[i64],
    lineages: &[LineageRow],
    g: &GraphData,
    slice_label: &dyn Fn(i64) -> String,
    export: Option<ExportInfo>,
) -> Option<Page> {
    // ---------------------------------------------------------------- controls
    let mut do_export = false;
    ui.horizontal(|ui| {
        ui.label("Lines at Jaccard ≥");
        ui.add(egui::Slider::new(&mut opts.min_jaccard, 0.0..=0.6).step_by(0.01).fixed_decimals(2));
        ui.separator();
        ui.label("Lineages reaching ≥");
        ui.add(egui::Slider::new(&mut opts.min_size, 1..=1000).logarithmic(true).suffix(" cases"));
        ui.separator();
        ui.radio_value(&mut opts.orient, Orient::TopToBottom, "Top to bottom");
        ui.radio_value(&mut opts.orient, Orient::LeftToRight, "Left to right");
        let newest = if opts.orient == Orient::TopToBottom { "Newest at top" } else { "Newest at left" };
        ui.checkbox(&mut opts.newest_top, newest);
        ui.separator();
        if ui
            .add_enabled(export.is_some(), egui::Button::new("Export SVG"))
            .on_hover_text("write this graph as shown (orientation and filters) to build/exports/ in the repo, then open it")
            .clicked()
        {
            do_export = true;
        }
    });
    if let Some(m) = &opts.export_msg {
        ui.weak(m);
    }

    let Some(lay) = Layout::build(opts, all_slices, lineages, g) else {
        ui.weak("No lineage in this area reaches that size.");
        return None;
    };
    if do_export && let Some(ex) = &export {
        opts.export_msg = Some(match export_svg(ex, opts.orient, &lay, g, slice_label) {
            Ok(path) => {
                crate::cases::open_external(&path.to_string_lossy());
                format!("wrote {}", path.display())
            }
            Err(e) => format!("export failed: {e}"),
        });
    }
    ui.weak(format!(
        "{} of {} lineages shown, in {} lanes · {} lines inside the area, {} to lineages outside it (hollow markers) · hover for names, click to open",
        lay.lins.len(),
        lineages.len(),
        lay.lanes,
        lay.edges.len(),
        lay.markers.len()
    ));

    // ---------------------------------------------------------------- geometry
    let measure = |s: &str| {
        ui.fonts_mut(|f| f.layout_no_wrap(s.to_string(), FontId::proportional(NAME_FONT), Color32::WHITE).size().x)
    };
    let geo = Geom::new(opts.orient, &lay, &measure);
    let (rect, resp) = ui.allocate_exact_size(geo.size, Sense::click());
    let tl = rect.min;
    let painter = ui.painter_at(rect);
    let pos = |slice: i64, lane: usize| geo.dot(tl, &lay, slice, lane);

    let text_col = ui.visuals().weak_text_color();
    let grid_col = ui.visuals().faint_bg_color;
    for (i, s) in lay.slices.iter().enumerate() {
        let (band, at, align) = geo.stripe(tl, i);
        if i % 2 == 0 {
            painter.rect_filled(band, 0.0, grid_col);
        }
        painter.text(at, align, slice_label(*s), FontId::proportional(LABEL_FONT), text_col);
    }
    for n in &geo.names {
        let col = n.li.map(|i| lay.lins[i].color).unwrap_or(text_col);
        painter.text(tl + n.pos.to_vec2(), Align2::LEFT_CENTER, &n.text, FontId::proportional(NAME_FONT), col);
    }

    // ---------------------------------------------------------------- hover
    let markers: Vec<(Pos2, Pos2, usize)> = lay
        .markers
        .iter()
        .map(|m| (geo.marker(tl, &lay, m), pos(m.slice, lay.lins[m.li].lane), m.oi))
        .collect();
    let hover_out: Option<usize> = resp.hover_pos().and_then(|p| {
        markers
            .iter()
            .filter(|m| m.0.distance(p) <= 6.0)
            .min_by(|a, b| a.0.distance(p).total_cmp(&b.0.distance(p)))
            .map(|m| m.2)
    });
    // hovered dot (nearest within reach)
    let hover = resp.hover_pos().filter(|_| hover_out.is_none()).and_then(|p| {
        let mut best: Option<(usize, usize, f32)> = None;
        for (i, l) in lay.lins.iter().enumerate() {
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

    // ---------------------------------------------------------------- drawing
    for e in &lay.edges {
        let lit = hl == Some(e.a) || hl == Some(e.b);
        let alpha = if lit { 230 } else { edge_alpha(e.j) };
        let col = lay.lins[if e.from < e.to { e.b } else { e.a }].color;
        let col = Color32::from_rgba_unmultiplied(col.r(), col.g(), col.b(), alpha);
        painter.add(CubicBezierShape::from_points_stroke(
            geo.curve(tl, &lay, e),
            false,
            Color32::TRANSPARENT,
            Stroke::new(if lit { 2.0 } else { 1.0 }, col),
        ));
    }
    let stub_col = ui.visuals().weak_text_color();
    for &(m, dot, oi) in &markers {
        let lit = hover_out == Some(oi) || lay.at.get(&g.outside[oi].here).is_some_and(|&li| hl == Some(li));
        let col = if lit { ui.visuals().strong_text_color() } else { stub_col };
        painter.line_segment([dot, m], Stroke::new(1.0, col));
        painter.circle_stroke(m, 3.0, Stroke::new(if lit { 2.0 } else { 1.2 }, col));
    }
    for (i, l) in lay.lins.iter().enumerate() {
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

    // ---------------------------------------------------------------- tooltips and clicks
    if let Some(oi) = hover_out {
        let o = &g.outside[oi];
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
        let head = format!("{} {}", if o.incoming { "Cases came in from" } else { "Cases went on to" }, o.other_name);
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
        let l = &lay.lins[i];
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

/// Curves fade with weaker links.
fn edge_alpha(j: f64) -> u8 {
    (60.0 + 400.0 * j).min(200.0) as u8
}

// -------------------------------------------------------------------- SVG export

/// Write the graph as a standalone SVG to `<dir>/area-<code>-<orientation>.svg`.
///
/// Besides the recent-searches list (`recent.rs`, outside the repo), exports are the only files
/// the app itself writes; everything else on disk comes from the Python scripts. Light background whatever the app theme,
/// since exports are for printing and sharing.
fn export_svg(ex: &ExportInfo, orient: Orient, lay: &Layout, g: &GraphData, slice_label: &dyn Fn(i64) -> String) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(&ex.dir)?;
    let code: String = ex.area_code.chars().map(|c| if c.is_ascii_alphanumeric() || c == '.' { c } else { '-' }).collect();
    let path = ex.dir.join(format!("area-{code}-{}.svg", orient.slug()));
    std::fs::write(&path, svg(ex, orient, lay, g, slice_label))?;
    Ok(path)
}

fn svg(ex: &ExportInfo, orient: Orient, lay: &Layout, g: &GraphData, slice_label: &dyn Fn(i64) -> String) -> String {
    use std::fmt::Write;
    // no font metrics outside egui: a rough average glyph width for 12px sans-serif
    let estimate = |s: &str| s.chars().count() as f32 * NAME_FONT * 0.55;
    let geo = Geom::new(orient, lay, &estimate);
    let title_h = 34.0;
    let tl = Pos2::new(0.0, title_h);
    let (w, h) = (geo.size.x.max(estimate(&ex.area_name) + 120.0), geo.size.y + title_h);
    let hex = |c: Color32| format!("#{:02x}{:02x}{:02x}", c.r(), c.g(), c.b());
    let mut s = String::new();
    let _ = writeln!(
        s,
        r#"<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0}" height="{h:.0}" viewBox="0 0 {w:.0} {h:.0}" font-family="sans-serif">"#
    );
    let _ = writeln!(s, r##"<rect width="100%" height="100%" fill="#ffffff"/>"##);
    let _ = writeln!(
        s,
        r##"<text x="12" y="22" font-size="15" font-weight="bold" fill="#222222">Area {} · {}</text>"##,
        xml(&ex.area_code),
        xml(&ex.area_name)
    );
    for (i, sl) in lay.slices.iter().enumerate() {
        let (band, at, _) = geo.stripe(tl, i);
        if i % 2 == 0 {
            let _ = writeln!(
                s,
                r##"<rect x="{:.1}" y="{:.1}" width="{:.1}" height="{:.1}" fill="#f2f2f2"/>"##,
                band.min.x,
                band.min.y,
                band.width(),
                band.height()
            );
        }
        let (anchor, baseline) = match orient {
            Orient::TopToBottom => ("start", "middle"),
            Orient::LeftToRight => ("middle", "hanging"),
        };
        let _ = writeln!(
            s,
            r##"<text x="{:.1}" y="{:.1}" font-size="{LABEL_FONT}" fill="#666666" text-anchor="{anchor}" dominant-baseline="{baseline}">{}</text>"##,
            at.x,
            at.y,
            xml(&slice_label(*sl))
        );
    }
    for n in &geo.names {
        let col = n.li.map(|i| hex(lay.lins[i].color)).unwrap_or_else(|| "#666666".into());
        let p = tl + n.pos.to_vec2();
        let _ = writeln!(
            s,
            r#"<text x="{:.1}" y="{:.1}" font-size="{NAME_FONT}" fill="{col}" dominant-baseline="middle">{}</text>"#,
            p.x,
            p.y,
            xml(&n.text)
        );
    }
    for e in &lay.edges {
        let [p0, p1, p2, p3] = geo.curve(tl, lay, e);
        let col = lay.lins[if e.from < e.to { e.b } else { e.a }].color;
        let _ = writeln!(
            s,
            r#"<path d="M{:.1},{:.1} C{:.1},{:.1} {:.1},{:.1} {:.1},{:.1}" fill="none" stroke="{}" stroke-opacity="{:.2}" stroke-width="1"/>"#,
            p0.x,
            p0.y,
            p1.x,
            p1.y,
            p2.x,
            p2.y,
            p3.x,
            p3.y,
            hex(col),
            edge_alpha(e.j) as f32 / 255.0
        );
    }
    for m in &lay.markers {
        let mp = geo.marker(tl, lay, m);
        let dot = geo.dot(tl, lay, m.slice, lay.lins[m.li].lane);
        let o = &g.outside[m.oi];
        let _ = writeln!(
            s,
            r##"<g stroke="#888888" fill="none"><title>{}</title><line x1="{:.1}" y1="{:.1}" x2="{:.1}" y2="{:.1}" stroke-width="1"/><circle cx="{:.1}" cy="{:.1}" r="3" stroke-width="1.2"/></g>"##,
            xml(&format!("{} {}", if o.incoming { "Cases came in from" } else { "Cases went on to" }, o.other_name)),
            dot.x,
            dot.y,
            mp.x,
            mp.y,
            mp.x,
            mp.y
        );
    }
    for l in &lay.lins {
        let col = hex(l.color);
        let _ = writeln!(s, "<g fill=\"{col}\"><title>{}</title>", xml(&l.row.name));
        for pair in l.cells.windows(2) {
            let (a, b) = (geo.dot(tl, lay, pair[0].0, l.lane), geo.dot(tl, lay, pair[1].0, l.lane));
            let _ = writeln!(s, r#"<line x1="{:.1}" y1="{:.1}" x2="{:.1}" y2="{:.1}" stroke="{col}" stroke-width="2"/>"#, a.x, a.y, b.x, b.y);
        }
        for c in &l.cells {
            let p = geo.dot(tl, lay, c.0, l.lane);
            let _ = writeln!(s, r#"<circle cx="{:.1}" cy="{:.1}" r="{:.1}"/>"#, p.x, p.y, radius(c.2));
        }
        s.push_str("</g>\n");
    }
    s.push_str("</svg>\n");
    s
}

/// Escape text for an XML element or attribute.
fn xml(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => o.push_str("&amp;"),
            '<' => o.push_str("&lt;"),
            '>' => o.push_str("&gt;"),
            '"' => o.push_str("&quot;"),
            '\'' => o.push_str("&apos;"),
            c if (c as u32) < 0x20 && c != '\t' && c != '\n' => {} // not allowed in XML 1.0
            c => o.push(c),
        }
    }
    o
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn xml_escapes_specials() {
        assert_eq!(xml(r#"A & B <c> "d" 'e'"#), "A &amp; B &lt;c&gt; &quot;d&quot; &apos;e&apos;");
    }
}
