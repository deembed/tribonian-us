//! cocite.py's edge cache (`cocite-cache/` beside the cocite*.sqlite views, written by `tribonian cocite prep`),
//! read for Cited together. One row per (citing decision, cited case), opinions already collapsed to
//! their decision:
//!   citing.npy        int32, citing decision per edge, sorted
//!   by_cited.npy      int32, edge indexes ordered by cited case
//!   cited_sorted.npy  int32, cited case per edge in that order (binary search: every decision citing X)
//!   ref_year.npy      int16, filing year by cluster id (0 = none)
//! Read with positioned reads, no memory map: a binary search is ~27 small reads, a case's citing
//! edges one contiguous read, and their decisions one read each in edge order (mostly sequential).

use std::fs::File;
use std::path::Path;

/// Fill buf from the file at offset off, without moving a shared cursor: pread on Unix, seek_read
/// (which may return short, so loop) on Windows.
#[cfg(unix)]
fn read_exact_at(file: &File, buf: &mut [u8], off: u64) -> std::io::Result<()> {
    std::os::unix::fs::FileExt::read_exact_at(file, buf, off)
}

#[cfg(windows)]
fn read_exact_at(file: &File, mut buf: &mut [u8], mut off: u64) -> std::io::Result<()> {
    use std::os::windows::fs::FileExt;
    while !buf.is_empty() {
        match file.seek_read(buf, off) {
            Ok(0) => return Err(std::io::Error::new(std::io::ErrorKind::UnexpectedEof, "file ended early")),
            Ok(n) => {
                buf = &mut buf[n..];
                off += n as u64;
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// One .npy array: its file, where the data starts, item count and size.
pub struct Npy {
    file: File,
    off: u64,
    pub len: u64,
    item: u64,
}

impl Npy {
    /// Open a 1-d little-endian array of the given dtype ("<i4", "<i2").
    pub fn open(path: &Path, descr: &str) -> Result<Npy, String> {
        let file = File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
        let mut head = [0u8; 12];
        read_exact_at(&file, &mut head, 0).map_err(|e| format!("{}: {e}", path.display()))?;
        if &head[..6] != b"\x93NUMPY" {
            return Err(format!("{}: not a .npy file", path.display()));
        }
        let (hlen, start) = if head[6] == 1 { (u16::from_le_bytes([head[8], head[9]]) as u64, 10) } else { (u32::from_le_bytes([head[8], head[9], head[10], head[11]]) as u64, 12) };
        let mut h = vec![0u8; hlen as usize];
        read_exact_at(&file, &mut h, start).map_err(|e| e.to_string())?;
        let h = String::from_utf8_lossy(&h);
        if !h.contains(&format!("'descr': '{descr}'")) || h.contains("'fortran_order': True") {
            return Err(format!("{}: expected a {descr} array, header {h}", path.display()));
        }
        let item = if descr.ends_with('4') { 4 } else { 2 };
        let off = start + hlen;
        let size = file.metadata().map_err(|e| e.to_string())?.len();
        Ok(Npy { file, off, len: (size - off) / item, item })
    }

    fn read(&self, lo: u64, hi: u64) -> Result<Vec<u8>, String> {
        let mut buf = vec![0u8; ((hi - lo) * self.item) as usize];
        read_exact_at(&self.file, &mut buf, self.off + lo * self.item).map_err(|e| e.to_string())?;
        Ok(buf)
    }

    pub fn i32s(&self, lo: u64, hi: u64) -> Result<Vec<i32>, String> {
        Ok(self.read(lo, hi)?.chunks_exact(4).map(|b| i32::from_le_bytes([b[0], b[1], b[2], b[3]])).collect())
    }

    pub fn i32_at(&self, i: u64) -> Result<i32, String> {
        Ok(self.i32s(i, i + 1)?[0])
    }

    pub fn i16_at(&self, i: u64) -> Result<i16, String> {
        let b = self.read(i, i + 1)?;
        Ok(i16::from_le_bytes([b[0], b[1]]))
    }

    /// First index whose value is >= x (or > x with `after`), in a sorted int32 array.
    fn search(&self, x: i32, after: bool) -> Result<u64, String> {
        let (mut lo, mut hi) = (0u64, self.len);
        while lo < hi {
            let mid = (lo + hi) / 2;
            let v = self.i32_at(mid)?;
            if v < x || (after && v == x) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        Ok(lo)
    }
}

pub struct EdgeCache {
    citing: Npy,
    by_cited: Npy,
    cited_sorted: Npy,
    ref_year: Npy,
    /// stamp.txt: the bulk load the cache was made from
    pub stamp: String,
}

impl EdgeCache {
    pub fn open(dir: &Path) -> Result<EdgeCache, String> {
        let need = |f: &str| {
            let p = dir.join(f);
            if p.exists() { Ok(p) } else { Err(format!("no {} (run tribonian cocite prep)", p.display())) }
        };
        Ok(EdgeCache {
            citing: Npy::open(&need("citing.npy")?, "<i4")?,
            by_cited: Npy::open(&need("by_cited.npy")?, "<i4")?,
            cited_sorted: Npy::open(&need("cited_sorted.npy")?, "<i4")?,
            ref_year: Npy::open(&need("ref_year.npy")?, "<i2")?,
            stamp: std::fs::read_to_string(dir.join("stamp.txt")).unwrap_or_default(),
        })
    }

    /// Every decision citing case `r`, sorted, without repeats.
    pub fn citing(&self, r: i64) -> Result<Vec<i32>, String> {
        let Ok(r) = i32::try_from(r) else { return Ok(Vec::new()) };
        let (lo, hi) = (self.cited_sorted.search(r, false)?, self.cited_sorted.search(r, true)?);
        if lo == hi {
            return Ok(Vec::new());
        }
        let mut edges = self.by_cited.i32s(lo, hi)?;
        edges.sort_unstable();
        let mut out = Vec::with_capacity(edges.len());
        // runs of neighbouring edges in one read each
        let mut k = 0;
        while k < edges.len() {
            let mut j = k + 1;
            while j < edges.len() && edges[j] - edges[j - 1] < 1024 && edges[j] - edges[k] < 262_144 {
                j += 1;
            }
            let (a, b) = (edges[k] as u64, edges[j - 1] as u64 + 1);
            let block = self.citing.i32s(a, b)?;
            out.extend(edges[k..j].iter().map(|&e| block[(e as u64 - a) as usize]));
            k = j;
        }
        out.sort_unstable();
        out.dedup();
        Ok(out)
    }

    /// Filing year of a case (0 = unknown).
    pub fn year(&self, r: i64) -> i64 {
        if r < 0 || r as u64 >= self.ref_year.len {
            return 0;
        }
        self.ref_year.i16_at(r as u64).map(|y| y as i64).unwrap_or(0)
    }
}

/// Write a 1-d .npy (tests).
#[cfg(test)]
pub fn write_npy(path: &Path, descr: &str, data: &[u8]) {
    let item = if descr.ends_with('4') { 4 } else { 2 };
    let mut h = format!("{{'descr': '{descr}', 'fortran_order': False, 'shape': ({},), }}", data.len() / item);
    while (10 + h.len() + 1) % 64 != 0 {
        h.push(' ');
    }
    h.push('\n');
    let mut out = b"\x93NUMPY\x01\x00".to_vec();
    out.extend((h.len() as u16).to_le_bytes());
    out.extend(h.as_bytes());
    out.extend(data);
    std::fs::write(path, out).unwrap();
}
