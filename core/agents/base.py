"""Adapter base: the "apply / restore / drift" contract plus the machinery every
agent must share.

An adapter is one small class per agent. It declares

  * which config files it owns,
  * which *entries* it is about to write (file + key path + original value),
  * how to fold those entries into the file text,
  * how to undo them again.

The base class owns the parts that must be identical everywhere:

  * snapshot on FIRST apply only — re-applying a different model never overwrites
    the recorded originals, so the user can flip models as often as they like and
    still land back on the state before the first apply,
  * byte-exact backup of each file before the first write (the rollback path when
    a rewrite cannot be expressed by the entry list),
  * atomic writes that keep the file's mode,
  * drift detection: the file must still be what we last wrote, otherwise the
    caller has to pass ``force`` before anything is overwritten.
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
from .edit import detect_json_style, json_dump, json_load
from .snapshot import SnapshotStore, make_entry  # noqa: F401  (re-exported)


class DriftError(RuntimeError):
    """The file changed outside this tool since we last wrote it."""


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
    """One key this adapter writes, with the original value it replaces."""

    file: str
    path: list[str]
    present: bool
    value: Any = None
    kind: str = "json"
    #: optional positional hint (e.g. the TOML table that preceded this one) so a
    #: rollback can put a section back where the user had it
    after: str | None = None


class Adapter:
    """One agent's config wiring."""

    agent_id: str = ""
    display_name: str = ""
    #: config files this adapter owns, relative to the user's home
    files: tuple[str, ...] = ()
    #: True when the agent caches its model list / settings at startup
    needs_restart: bool = False
    #: True when the agent would read keys from the shell environment first and
    #: silently shadow everything we write
    shadows_env_keys: tuple[str, ...] = ()

    def __init__(self, home: Path, gateway_url: str, gateway_key: str, store: SnapshotStore,
                 facts: Any = None):
        self.home = Path(home)
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_key = gateway_key
        self.store = store
        #: 网关事实（地址、模型表、档位映射）。通用工具只能猜，能拿到网关自己那份
        #: models.json 的调用方应当传进来；为 None 时适配器退回到写死的常量。
        self.facts = facts

    # ---- paths -----------------------------------------------------------
    def path(self, rel: str) -> Path:
        return self.home / rel

    def all_paths(self) -> list[Path]:
        return [self.path(rel) for rel in self.files]

    def present(self) -> bool:
        return any(p.exists() for p in self.all_paths())

    # ---- text I/O that never normalises the file -------------------------
    @staticmethod
    def load_text(path: Path) -> tuple[str, str, bool]:
        """(text, newline, bom) — the text round-trips byte-exactly."""
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
        """Shape facts a rollback needs but a key list cannot carry."""
        data = read_bytes(path)
        return {"trailing_newline": data.endswith(b"\n")}

    @staticmethod
    def dump_text(text: str, bom: bool) -> bytes:
        return (b"\xef\xbb\xbf" if bom else b"") + encode(text)

    # ---- hooks a subclass implements -------------------------------------
    #: True when the adapter may add lines to a file that lacked a final newline.
    #: The inherited default keeps the user's trailing-newline choice intact.
    keep_final_newline: bool = True

    def plan(self, model: str, effort: str | None) -> list[Entry]:
        """Every key this apply will write, with the CURRENT values read from disk."""
        raise NotImplementedError

    def render(self, path: Path, text: str, newline: str, entries: list[Entry], model: str,
               effort: str | None) -> str:
        """New file text with ``entries`` applied."""
        raise NotImplementedError

    def unrender(self, path: Path, text: str, newline: str, entries: list[Entry]) -> str:
        """New file text with ``entries`` restored to their snapshotted values."""
        raise NotImplementedError

    def extra_warnings(self) -> list[str]:
        return []

    # ---- shared orchestration --------------------------------------------
    def _backup(self, snap: dict[str, Any], path: Path, data: bytes) -> bool:
        """Record the pre-first-write state of one file; returns whether it existed."""
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
                f"Re-run with --force to overwrite it, or roll back first."
            )

    def ensure_snapshot(self) -> dict[str, Any] | None:
        return self.store.load(self.agent_id)

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
                # The originals are recorded ONCE, before the first write.
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

        # Whole files we own (a model catalogue, say) have no keys to splice: they
        # are rendered from the model we were asked for, and removed on rollback.
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
                # The agent had no config before we ran: leave nothing behind.
                if path.exists():
                    path.unlink()
                    res.removed.append(str(path))
                continue
            data = read_bytes(path)
            self._check_drift(path, data, snap, force)
            own = entries_by_file.get(file_str, [])
            kind_own = rec.get("kind") == "own-file" or any(e.kind == "own-file" for e in own)
            if kind_own:
                # A file whose whole content is ours: the recorded bytes are the truth.
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

    # ---- helpers for subclasses ------------------------------------------
    def current(self, path: Path) -> str:
        text, _, _ = self.load_text(path)
        return text

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    # ---- nested-key helpers used by every adapter ------------------------
    def read_json(self, path: Path) -> dict:
        text, _, _ = self.load_text(path)
        return json_load(text)

    def write_json(self, path: Path, obj: dict, newline: str) -> str:
        text, _, _ = self.load_text(path)
        return json_dump(obj, detect_json_style(text))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _final_newline(text: str, newline: str, meta: dict[str, Any]) -> str:
    """Match the file's original end-of-file choice.

    A file the user ended without a newline must not grow one just because we added
    keys, and must not keep one after rollback: that single byte is the whole
    difference between "restored" and "dirty diff". New files default to a newline.
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
