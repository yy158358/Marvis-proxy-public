# -*- coding: utf-8 -*-
"""路径视图：让「写到哪个家目录」可以替换。

这层存在的理由：wire 会改用户家目录下的 Claude Code / Codex / DSH 配置。
跑在真实数据旁边的代码，得能先干跑一遍：

    with paths.redirect(tmp_home):
        wire.apply(ctx, ["claude-code"], model)

所有落盘都经过 :func:`abs` / :func:`home`，一次 redirect 就把整条链（含原子写
临时文件、快照、备份）指向临时目录，跑完不留痕。没开 redirect 时只是一次函数
调用。
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
    """当前生效的家目录。"""
    return _selected_home()


def abs(path):
    """立刻定下绝对路径，写入时不受之后的 redirect 影响。"""
    raw = os.fspath(path)
    if raw.startswith("~"):
        raw = _selected_home() + raw[1:]
    return os.path.abspath(raw)


def under_home(*parts):
    return abs(os.path.join(_selected_home(), *parts))


@contextlib.contextmanager
def redirect(new_home):
    """在这个 with 块内把家目录换成 new_home，仅当前线程有效。"""
    old = _override()
    _LOCAL.home = os.path.abspath(os.fspath(new_home))
    try:
        yield _LOCAL.home
    finally:
        _LOCAL.home = old
