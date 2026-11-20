"""Atomic file write + content hashing + byte-exact text I/O.

The single rule: a config file on disk is never observed half-written, and a
file we rewrite keeps its exact encoding, BOM and line endings.

Why bytes and not ``str``: real config files on this machine mix UTF-8 with
GBK-mojibake comments (``~/.dsh/profiles/desktop/cordis.patch.yml`` already
contains ``鏈嶅姟``). Reading UTF-8 and writing UTF-8 back would silently
transcode those bytes; every function here round-trips the raw bytes and only
the ASCII edits we make are new. ``strict=False`` means invalid byte sequences
are carried as lone surrogates (and re-encoded losslessly), never replaced by
U+FFFD.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile

from pathlib import Path

from . import paths as _paths

_BOM = b"\xef\xbb\xbf"
_UTF16BOM = (b"\xff\xfe", b"\xfe\xff")


def read_bytes(path: Path) -> bytes:
    """Raw bytes; a missing file reads as empty."""
    try:
        return Path(_paths.abs(path)).read_bytes()
    except FileNotFoundError:
        return b""


def decode(data: bytes) -> str:
    """Bytes -> text, lossless for any byte sequence."""
    if not data:
        return ""
    return data.decode("utf-8", "surrogateescape")


def encode(text: str) -> bytes:
    """Text -> bytes, the exact inverse of :func:`decode`."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogateescape")


def read_text(path: Path) -> str:
    return decode(read_bytes(path))


def has_bom(data: bytes) -> bool:
    return data.startswith(_BOM)


def is_utf16(data: bytes) -> bool:
    return data.startswith(_UTF16BOM)


def newline_of(text: str) -> str:
    """Detect the dominant line ending so an insertion never mixes CRLF into an LF file.

    Counts real newlines only: ``\\r\\r\\n`` is a CRLF line, not one CRLF plus a
    stray CR, so repeated conversions cannot drift into the wrong convention.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n")
    return "\r\n" if crlf > lf - crlf else "\n"


def to_newline(text: str, newline: str) -> str:
    """Re-express every line ending as ``newline``, idempotently."""
    if newline == "\r\n":
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.replace("\r\n", "\n")


def file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(Path(_paths.abs(path)).stat().st_mode)
    except FileNotFoundError:
        return None


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write bytes atomically: same-dir temp file -> fsync -> os.replace.

    The temp file lives in the SAME directory because a rename across
    filesystems is a copy and therefore not atomic.
    """
    path = Path(_paths.abs(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = file_mode(path)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def same_bytes(a: bytes, b: bytes) -> bool:
    return a == b


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = encode(data)
    return hashlib.sha256(data).hexdigest()
