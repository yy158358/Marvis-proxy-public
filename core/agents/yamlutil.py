# -*- coding: utf-8 -*-
"""``YamlDoc`` 之外的小工具：行内 ``{}`` 探测、块缩进、扁平凭据编辑。"""
from __future__ import annotations

from .atomic import newline_of, to_newline


def inline_value(text, path):
    """``path`` 若写成行内的 ``{}``/``[]`` 就返回那个符号，否则 None。

    ``YamlDoc`` 把 ``providers: {}`` 当空映射，``ensure_block`` 会展开它才能追加
    子节点。记住原拼写，回滚才放得回大括号。
    """
    key = str(path[-1])
    want = 2 * (len(path) - 1)
    for line in text.splitlines():
        body = line.rstrip()
        stripped = body.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(body) - len(stripped) != want:
            continue
        if not stripped.startswith(key + ":"):
            continue
        tail = stripped[len(key) + 1:].strip()
        if tail in ("{}", "[]"):
            return tail
    return None


def block_indent(lines):
    """一块 YAML 源码自身的缩进（第一个非空行的前导空格数）。"""
    for line in lines:
        if line.strip():
            return len(line) - len(line.lstrip(" "))
    return 6


# ---- 扁平凭据库：``REF: value`` 一行一条 -------------------------------------

def cred_get(text, ref):
    for line in text.splitlines():
        if line.startswith(ref + ":"):
            return line.split(":", 1)[1].strip()
    return None


def cred_set(text, ref, value):
    if cred_get(text, ref) == value:
        return text
    newline = newline_of(text) if text else "\n"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(ref + ":"):
            lines[index] = "%s: %s" % (ref, value)
            break
    else:
        lines.append("%s: %s" % (ref, value))
    return to_newline(newline.join(lines) + newline, newline)


def cred_drop(text, ref):
    newline = newline_of(text) if text else "\n"
    lines = [line for line in text.splitlines() if not line.startswith(ref + ":")]
    body = newline.join(lines)
    return body + newline if body else ""


def reorder(obj, order):
    """按 ``order`` 重排已知键，未知键按原顺序跟在后面。"""
    known = [k for k in order if k in obj]
    extra = [k for k in obj if k not in order]
    rebuilt = {k: obj[k] for k in (*known, *extra)}
    obj.clear()
    obj.update(rebuilt)


__all__ = [
    "inline_value",
    "block_indent",
    "cred_get",
    "cred_set",
    "cred_drop",
    "reorder",
]
