# -*- coding: utf-8 -*-
"""适配器基类：apply / restore / drift 的契约，以及每个 agent 都要共用的机械工作。

一个适配器就是一个 agent 的小类，它声明：自己占哪些配置文件、这次要写哪些 *entry*
（文件 + 键路径 + 原值）、怎么折进文件文本、怎么撤销。

基类管必须处处一致的部分：

* 只在**第一次** apply 时建快照 —— 换模型重跑不会覆盖记下的原值，用户怎么来回切都能
  回到第一次 apply 之前的状态；
* 首次写之前做逐字节备份（entry 表达不了的重写，靠它兜底回滚）；
* 原子写，并保留文件权限；
* 漂移检测：文件必须还是我们上次写的样子，否则要先拿到 ``force`` 才动。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomic import (
    atomic_write_bytes,
    content_hash,
    decode,
    encode,
    file_mode,
    has_bom,
    is_utf16,
    newline_of,
    read_bytes,
)
from .snapshot import SnapshotStore, make_entry  # noqa: F401  (re-exported)


class DriftError(RuntimeError):
    """文件在上次写入之后被外部改过。"""


@dataclass
class ApplyResult:
    agent: str
    changed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_restart: bool = False
    files_created: list[str] = field(default_factory=list)


@dataclass
class RollbackResult:
    agent: str
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_restart: bool = False


@dataclass
class Entry:
    """适配器要写的一个键，连同它替换掉的原值。"""

    file: str
    path: list[str]
    present: bool
    value: Any = None
    kind: str = "json"
    #: 可选的位置提示（例如这张表前面是哪张 TOML 表），回滚时好放回原位
    after: str | None = None


class Adapter:
    """单个 agent 的配置接线。"""

    agent_id: str = ""
    display_name: str = ""
    #: 本适配器占的配置文件，相对于用户家目录
    files: tuple[str, ...] = ()
    #: agent 启动时缓存模型清单/设置，改完必须重启
    needs_restart: bool = False
    #: agent 会先读 shell 环境里的同名变量，静默压掉我们写的一切
    shadows_env_keys: tuple[str, ...] = ()

    def __init__(self, home: Path, gateway_url: str, gateway_key: str, store: SnapshotStore,
                 facts: Any = None):
        self.home = Path(home)
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_key = gateway_key
        self.store = store
        #: 网关事实（地址、模型表、档位映射）。能拿到网关自己那份 models.json 就传进来；
        #: 为 None 时适配器退回写死的常量。
        self.facts = facts

    # ---- 路径 -----------------------------------------------------------
    def path(self, rel: str) -> Path:
        return self.home / rel

    def all_paths(self) -> list[Path]:
        return [self.path(rel) for rel in self.files]

    def present(self) -> bool:
        return any(p.exists() for p in self.all_paths())

    # ---- 绝不规整文件的文本读写 -----------------------------------------
    @staticmethod
    def load_text(path: Path) -> tuple[str, str, bool]:
        """(text, newline, bom)，文本能逐字节往返。"""
        data = read_bytes(path)
        if is_utf16(data):
            raise ValueError(f"{path}: UTF-16 config files are not supported (rewrite as UTF-8 first)")
        bom = has_bom(data)
        if bom:
            data = data[3:]
        text = decode(data)
        return text, newline_of(text), bom

    @staticmethod
    def load_meta(path: Path) -> dict[str, Any]:
        """回滚需要、但键清单装不下的形态信息。"""
        data = read_bytes(path)
        return {"trailing_newline": data.endswith(b"\n")}

    @staticmethod
    def dump_text(text: str, bom: bool) -> bytes:
        return (b"\xef\xbb\xbf" if bom else b"") + encode(text)

    # ---- 子类实现的钩子 -------------------------------------------------
    #: 是否允许往「原本没有结尾换行」的文件里加行；默认保留用户的选择。
    keep_final_newline: bool = True

    def plan(self, model: str, effort: str | None) -> list[Entry]:
        """本次要写的每个键，附带从磁盘读到的当前值。"""
        raise NotImplementedError

    def render(self, path: Path, text: str, newline: str, entries: list[Entry], model: str,
               effort: str | None) -> str:
        """把这些 entry 折进文件后的新文本。"""
        raise NotImplementedError

    def unrender(self, path: Path, text: str, newline: str, entries: list[Entry]) -> str:
        """把这些 entry 还原成快照值后的新文本。"""
        raise NotImplementedError

    def extra_warnings(self) -> list[str]:
        return []

    # ---- 共用编排 -------------------------------------------------------
    def _backup(self, snap: dict[str, Any], path: Path, data: bytes) -> bool:
        """记下某文件首次写入前的状态，返回它原本是否存在。"""
        existed = path.exists()
        rec = snap["files"].setdefault(
            str(path), {"existed": existed, "mode": file_mode(path), "hash_original": None,
                        "hash_written": None, "backup": None}
        )
        if rec.get("backup") is None and rec.get("existed"):
            index = len(snap["files"]) - 1
            rec["backup"] = str(self.store.write_backup(self.agent_id, index, path, data, rec.get("mode")))
        rec["hash_original"] = content_hash(data)
        return existed

    def _check_drift(self, path: Path, data: bytes, snap: dict[str, Any], force: bool) -> None:
        rec = snap["files"].get(str(path))
        if not rec:
            return
        expected = rec.get("hash_written")
        if expected is None:
            return
        found = content_hash(data)
        if found != expected and not force:
            raise DriftError(
                f"{path} changed outside this tool since the last write "
                f"(expected sha256 {expected[:12]}…, found {found[:12]}…). "
                f"Pass force=true to overwrite it, or roll back first."
            )

    def apply(self, model: str, effort: str | None = None, force: bool = False) -> ApplyResult:
        res = ApplyResult(agent=self.agent_id)
        res.warnings.extend(self.extra_warnings())

        entries = self.plan(model, effort)
        own_files = [e for e in entries if e.path == ["*"]]
        entries = [e for e in entries if e.path != ["*"]]
        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.file, []).append(entry)

        snap = self.store.load(self.agent_id)
        fresh = snap is None
        if snap is None:
            snap = {"agent": self.agent_id, "created_at": _now(), "files": {},
                    "entries": [], "created_files": [],
                    "gateway": {"url": self.gateway_url, "key": self.gateway_key}}

        for rel_file, file_entries in by_file.items():
            path = Path(rel_file)
            data = read_bytes(path)
            existed = path.exists()

            if fresh:
                existed = self._backup(snap, path, data)
                rec = snap["files"][str(path)]
                rec["meta"] = self.load_meta(path)
                if not existed:
                    snap["created_files"].append(str(path))
                # 原值只在首次写入前记一次。
                for entry in file_entries:
                    if entry.kind == "own-file":
                        continue  # a whole file we own is restored by its backup
                    record = {"file": entry.file, "path": entry.path, "present": entry.present,
                              "value": entry.value, "kind": entry.kind}
                    if entry.after is not None:
                        record["after"] = entry.after
                    snap["entries"].append(record)

            self._check_drift(path, data, snap, force)

            text, newline, bom = self.load_text(path)
            new_text = self.render(path, text, newline, file_entries, model, effort)
            meta = (snap["files"].get(str(path)) or {}).get("meta") or {}
            new_data = self.dump_text(_final_newline(new_text, newline, meta), bom)
            rec = snap["files"].setdefault(
                str(path), {"existed": existed, "mode": file_mode(path), "hash_original": content_hash(data),
                            "hash_written": None, "backup": None}
            )
            if new_data == data:
                res.skipped.append(str(path))
                rec["hash_written"] = content_hash(data)
                continue
            atomic_write_bytes(path, new_data, rec.get("mode"))
            rec["hash_written"] = content_hash(new_data)
            res.changed.append(str(path))
            if not existed:
                res.files_created.append(str(path))

        # 整份归我们的文件（例如模型清单）没有键可接：按请求的模型整体渲染，回滚时删掉。
        for entry in own_files:
            path = Path(entry.file)
            if fresh:
                self._backup(snap, path, read_bytes(path))
                snap["files"][str(path)]["meta"] = self.load_meta(path)
                if not snap["files"][str(path)].get("existed"):
                    snap["created_files"].append(str(path))
            data = read_bytes(path)
            self._check_drift(path, data, snap, force)
            new_data = encode(self.render(path, decode(data), newline_of(decode(data)), [entry], model, effort))
            rec = snap["files"].setdefault(
                str(path), {"existed": path.exists(), "mode": file_mode(path),
                            "hash_original": content_hash(data), "hash_written": None, "backup": None}
            )
            rec["kind"] = "own-file"
            if new_data == data:
                res.skipped.append(str(path))
                continue
            atomic_write_bytes(path, new_data, rec.get("mode"))
            rec["hash_written"] = content_hash(new_data)
            res.changed.append(str(path))
            if not rec.get("existed"):
                res.files_created.append(str(path))

        snap["applied"] = {"model": model, "effort": effort, "at": _now()}
        self.store.save(snap)
        res.needs_restart = self.needs_restart
        return res

    def rollback(self, force: bool = False) -> RollbackResult:
        res = RollbackResult(agent=self.agent_id)
        snap = self.store.load(self.agent_id)
        if snap is None:
            res.warnings.append("no snapshot for this agent: nothing to restore")
            return res

        created = set(snap.get("created_files", []))
        entries_by_file: dict[str, list[Entry]] = {}
        for raw in snap.get("entries", []):
            entries_by_file.setdefault(raw["file"], []).append(
                Entry(file=raw["file"], path=raw["path"], present=raw["present"],
                      value=raw["value"], kind=raw["kind"], after=raw.get("after"))
            )

        for file_str, rec in snap["files"].items():
            path = Path(file_str)
            if file_str in created and not rec.get("existed"):
                # 我们动手之前这个 agent 没有配置：别留任何东西。
                if path.exists():
                    path.unlink()
                    res.removed.append(str(path))
                continue
            data = read_bytes(path)
            self._check_drift(path, data, snap, force)
            own = entries_by_file.get(file_str, [])
            kind_own = rec.get("kind") == "own-file" or any(e.kind == "own-file" for e in own)
            if kind_own:
                # 整份内容都是我们的：备份里的字节就是真相。
                if not rec.get("existed"):
                    if path.exists():
                        path.unlink()
                        res.removed.append(str(path))
                    continue
                original = self.store.read_backup(rec["backup"]) if rec.get("backup") else b""
                if data != original:
                    atomic_write_bytes(path, original, rec.get("mode"))
                    res.restored.append(str(path))
                continue
            text, newline, bom = self.load_text(path)
            new_text = self.unrender(path, text, newline, own)
            new_data = self.dump_text(_final_newline(new_text, newline, rec.get("meta") or {}), bom)
            if new_data != data:
                atomic_write_bytes(path, new_data, rec.get("mode"))
                res.restored.append(str(path))

        if not res.restored and not res.removed:
            res.warnings.append("config already matches the snapshot")
        self.store.delete(self.agent_id)
        self.store.purge_backups(self.agent_id)
        res.needs_restart = self.needs_restart
        return res

    # ---- 子类小工具 -----------------------------------------------------
    def current(self, path: Path) -> str:
        text, _, _ = self.load_text(path)
        return text

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _final_newline(text: str, newline: str, meta: dict[str, Any]) -> str:
    """对齐文件原本的结尾换行。

    用户没写结尾换行的文件，不能因为我们加了键就长出一个，也不能回滚后留着：这一个
    字节就是「已还原」和「脏 diff」的全部差别。新文件默认带换行。
    """
    if meta.get("trailing_newline", True):
        return text if text.endswith("\n") else text + newline
    return text.rstrip("\r\n")


__all__ = [
    "Adapter",
    "ApplyResult",
    "RollbackResult",
    "DriftError",
    "Entry",
    "SnapshotStore",
    "make_entry",
]
