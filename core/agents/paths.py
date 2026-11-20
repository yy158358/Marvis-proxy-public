# -*- coding: utf-8 -*-
"""路径视图：让「写到哪个 ~/」这件事可替换。

为什么要这一层：wire 会去改用户家目录里的 Claude Code / Codex / DSH 配置。
这样的代码在一个跑着真实用户数据的反代里，必须能先「干跑一遍」。

    with paths.redirect(tmp_home):
        wire.apply(ctx, ["claude-code"], model)

組内所有落盘经过 :func:`abs` / :func:`home`，所以一次 redirect 就能把整条链
（含原子写的临时文件、快照、备份）都指向临时目录，跑完不留痕。
没开 redirect 时开销就是一次函数调用，和直接 os.path.expanduser 一样。
"""
import contextlib
import os
import threading

_LOCAL = threading.local()


def _override():
    return getattr(_LOCAL, "home", None)


def _selected_home():
    return _override() or os.path.expanduser("~")


def home():
    """当前生效的用户家目录。"""
    return _selected_home()


def abs(path):
    """把路径固定下来：家目录重定向在写入时仍然有效，事后不受影响。"""
    raw = os.fspath(path)
    if raw.startswith("~"):
        raw = _selected_home() + raw[1:]
    return os.path.abspath(raw)


def under_home(*parts):
    return abs(os.path.join(_selected_home(), *parts))


@contextlib.contextmanager
def redirect(new_home):
    """在这个 with 块里，把家目录换成 new_home。线程内生效。"""
    old = _override()
    _LOCAL.home = os.path.abspath(os.fspath(new_home))
    try:
        yield _LOCAL.home
    finally:
        _LOCAL.home = old
