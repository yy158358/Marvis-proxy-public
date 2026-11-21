"""原子写、内容哈希、字节保真的文本读写。

两条铁律：配置文件不会以「写了一半」的状态被读到；重写后的编码、BOM、换行
原样不动。

为什么走 bytes 而不是 str：本机配置里混着 UTF-8 与 GBK 乱码注释（例如
``cordis.patch.yml`` 里的 ``鏈嶅姟``）。按 UTF-8 读再写回会静默转码这些字节。
这里全程走原始字节，``surrogateescape`` 让非法字节以孤立代理字符往返，
绝不被替换成 U+FFFD。
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
    """原始字节；文件不存在时返回空。"""
    try:
        return Path(_paths.abs(path)).read_bytes()
    except FileNotFoundError:
        return b""


def decode(data: bytes) -> str:
    """字节转文本，任意字节序列都无损。"""
    if not data:
        return ""
    return data.decode("utf-8", "surrogateescape")


def encode(text: str) -> bytes:
    """文本转字节，:func:`decode` 的精确逆运算。"""
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
    """判断主导换行符，避免往 LF 文件里插进 CRLF。

    ``\\r\\r\\n`` 算作一个 CRLF，不是 CRLF 加一个孤立 CR，这样反复转换也不会漂移。
    """
    crlf = text.count("\r\n")
    lf = text.count("\n")
    return "\r\n" if crlf > lf - crlf else "\n"


def to_newline(text: str, newline: str) -> str:
    """把所有换行统一成 ``newline``，幂等。"""
    if newline == "\r\n":
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.replace("\r\n", "\n")


def file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(Path(_paths.abs(path)).stat().st_mode)
    except FileNotFoundError:
        return None


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """同目录临时文件 → fsync → os.replace。

    临时文件必须同目录：跨文件系统 rename 会退化成复制，也就失去原子性。
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


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = encode(data)
    return hashlib.sha256(data).hexdigest()
