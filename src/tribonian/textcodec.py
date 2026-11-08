"""textcodec.py -- how opinion text is stored in opinions.sqlite: one place for every reader and writer.

New loads store zstd (level 3); databases loaded before 0.2.2 hold zlib. A zstd frame starts with the bytes
28 B5 2F FD and a zlib stream with 78, so unpack() tells them apart with no codec column, and an older database
reads as before until it is rebuilt.

Measured on real opinion text (bblinux, 2026-11-06), per core: zlib 6 writes 59 MB/s, reads 478 MB/s, 26.1% of the
input; zstd 3 writes 339 MB/s, reads 1,454 MB/s, 27.2%. zstd releases Python's lock while it works, like zlib, so the
load's worker threads compress in parallel.
"""
import threading, zlib

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
LEVEL = 3

try:
    import zstandard
except ImportError:                     # an older checkout without the dependency: write zlib, read zlib
    zstandard = None

_local = threading.local()             # zstandard's (de)compressor objects are not safe to share between threads


def _zc():
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = zstandard.ZstdCompressor(level=LEVEL)
    return c


def _zd():
    d = getattr(_local, "d", None)
    if d is None:
        d = _local.d = zstandard.ZstdDecompressor()
    return d


def pack(data):
    """Compress UTF-8 bytes for opinions.sqlite: zstd, or zlib 6 when zstandard isn't installed."""
    if zstandard is None:
        return zlib.compress(data, 6)
    return _zc().compress(data)


def unpack_bytes(blob):
    """The stored bytes back, whichever codec wrote them."""
    if blob[:4] == ZSTD_MAGIC:
        if zstandard is None:
            raise RuntimeError("this opinion text is zstd-compressed: pip install zstandard")
        return _zd().decompress(blob)
    return zlib.decompress(blob)


def unpack(blob):
    """Stored opinion text as a str (bad UTF-8 replaced, as everywhere else)."""
    return unpack_bytes(blob).decode("utf-8", "replace")
