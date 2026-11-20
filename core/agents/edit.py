"""Format-preserving text editing.

Two different strategies, deliberately:

  * JSON  -> parsed, mutated, written back with the file's own indent/newline style.
             objects keep key order (dict preserves insertion order), and we do NOT
             sort keys, so an unrelated key is never moved.
  * TOML  -> NOT re-serialised. We locate the exact byte range of the keys we own and
             splice new text in place, so comments, blank lines, table order and any
             syntax our parser does not model survive untouched.

Everything here is pure text -> text so it can be unit-tested without touching disk.
"""
from __future__ import annotations

import json
import re
from typing import Any

# --------------------------------------------------------------------------- JSON


def detect_json_style(text: str) -> dict[str, Any]:
    """Guess indent, newline and trailing newline so a rewrite looks like the user's file."""
    m = re.search(r"\r?\n([ \t]+)\S", text)
    indent: int | str = 2
    if m:
        raw = m.group(1)
        indent = "\t" if raw.startswith("\t") else len(raw)
    compact = not m and "\n" not in text.strip()
    # The separator is part of the user's style: `{"a":1}` and `{"a": 1}` are the
    # same document but not the same file, and a rollback that swaps them is wrong.
    spaced = bool(re.search(r'":\s+[^\s]', text)) or bool(re.search(r",\s", text))
    return {
        "indent": None if compact else indent,
        "newline_at_end": text.endswith("\n") or not text,
        "newline": "\r\n" if "\r\n" in text else "\n",
        "separator": (", ", ": ") if spaced else (",", ":"),
    }


def json_load(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    return json.loads(text)


def json_dump(obj: dict, style: dict[str, Any]) -> str:
    indent = style.get("indent")
    separators = style.get("separator")
    if not (isinstance(separators, (list, tuple)) and len(separators) == 2):
        separators = None
    if indent is not None:
        # Python writes the item separator after the colon-space of an indented
        # value, so ", " turns every non-last key into `"a": 1, ` with a trailing
        # space. Indented output always uses ","; only compact output varies.
        separators = (",", ": ") if separators is None or separators[1] == ": " else (",", ":")
    out = json.dumps(obj, indent=indent, ensure_ascii=False, separators=separators)
    if style.get("newline") == "\r\n":
        out = out.replace("\n", "\r\n")
    if style["newline_at_end"]:
        out += style.get("newline", "\n")
    return out


def json_get(obj: dict, dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def json_has(obj: dict, dotted: str) -> bool:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def json_set(obj: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def json_delete(obj: dict, dotted: str) -> bool:
    """Delete a dotted key; also drop parent objects that became empty."""
    parts = dotted.split(".")
    chain: list[dict] = [obj]
    for part in parts[:-1]:
        nxt = chain[-1].get(part)
        if not isinstance(nxt, dict):
            return False
        chain.append(nxt)
    if parts[-1] not in chain[-1]:
        return False
    del chain[-1][parts[-1]]
    for i in range(len(chain) - 1, 0, -1):
        if chain[i]:
            break
        chain[i - 1].pop(parts[i - 1], None)
    return True


def json_render(obj: dict, style: dict[str, Any]) -> str:
    """Serialise with the style of the file the object came from."""
    return json_dump(obj, style)


# --------------------------------------------------------------------------- TOML
#
# Minimal span-preserving TOML helper. It does not try to be a full TOML parser:
# it knows (a) top-level `key = value` lines and (b) `[table]` sections, which is all
# config.toml needs for the keys we touch.
#
# The one invariant that matters: whatever we did not mean to change, we do not
# change. In particular, setting a key whose *value* keeps its value is a no-op, and
# a table rewrite replaces exactly the table's own lines — it never eats or invents
# blank lines, because those belong to the user, not to us.

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key_pattern(key: str) -> str:
    q = re.escape(key)
    if _BARE_KEY.match(key):
        return rf"(?:{q}|\"{q}\"|'{q}')"
    return rf"(?:\"{q}\"|'{q}')"


def _table_header(table: str) -> re.Pattern[str]:
    return re.compile(rf"^[ \t]*\[\s*{re.escape(table)}\s*\][ \t]*\r?$", re.MULTILINE)


def _next_header(text: str, start: int) -> int:
    m = re.compile(r"^[ \t]*\[", re.MULTILINE).search(text, start)
    return m.start() if m else len(text)


def toml_tables(text: str) -> list[str]:
    """Table names in file order (``[a.b]`` -> ``"a.b"``)."""
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            names.append(stripped.strip("[]").strip())
    return names


def toml_table_neighbours(text: str, table: str) -> tuple[str | None, str | None]:
    """(table before, table after) around ``table``, or (None, None) if absent."""
    names = toml_tables(text)
    if table not in names:
        return None, None
    i = names.index(table)
    return (names[i - 1] if i > 0 else None), (names[i + 1] if i + 1 < len(names) else None)


def toml_tables(text: str) -> list[str]:
    """Table names in file order (``[a.b]`` -> ``"a.b"``)."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            out.append(stripped.strip("[]").strip())
    return out


def toml_find_key(text: str, key: str, table: str | None) -> tuple[int, int, str] | None:
    """Return (start, end, value_text) of the line holding `key` inside `table`.

    `table=None` means the top-level section (before the first [header]).
    The span covers the whole line including its newline, so replacing it with ""
    removes the line cleanly.
    """
    lines = text.splitlines(keepends=True)
    section = None
    offset = 0
    pat = re.compile(rf"^\s*{_key_pattern(key)}\s*=")
    for line in lines:
        start = offset
        end = offset + len(line)
        offset = end
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").strip()
            continue
        if section != table:
            continue
        if pat.match(line):
            value = line.split("=", 1)[1].rstrip("\r\n").strip()
            return start, end, value
    return None


def toml_get_value(text: str, key: str, table: str | None) -> str | None:
    hit = toml_find_key(text, key, table)
    return hit[2] if hit else None


def toml_has_table(text: str, table: str) -> bool:
    return bool(_table_header(table).search(text))


def toml_set_key(text: str, key: str, table: str | None, value: str) -> str:
    """Set ``key = value`` in place, or append it to the right section.

    A key that already holds exactly ``value`` is left alone, so repeated applies
    do not shuffle a comment onto a different line.
    """
    hit = toml_find_key(text, key, table)
    if hit is not None:
        start, end, old = hit
        if old == value:
            return text
        line = text[start:end]
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        eq = body.index("=")
        return text[:start] + body[: eq + 1] + " " + value + eol + text[end:]

    eol = "\r\n" if "\r\n" in text else "\n"
    new_line = f"{key} = {value}{eol}"
    if table is None:
        # Top-level: append after the last top-level key, or right after the last
        # comment header when the file has no keys yet — never glued to a table.
        lines = text.splitlines(keepends=True)
        last = None
        for line_no, line in enumerate(lines):
            if re.match(r"^[ \t]*\[", line):
                break
            if re.match(rf"^\s*{_key_pattern(key)}\s*=", line):
                last = line_no
        if last is not None:
            head = "".join(lines[: last + 1])
            tail = "".join(lines[last + 1:])
            if not head.endswith("\n"):
                head += "\n"
            if not tail:
                new_line += eol
            return _final_newline(head + new_line + tail, eol)
        head_end = 0
        for line in lines:
            if re.match(r"^[ \t]*\[", line):
                break
            head_end += len(line)
        head, tail = text[:head_end], text[head_end:]
        if not head:
            return new_line + (eol + tail if tail else "")
        # Insert the new top-level key directly under the last existing line and
        # keep the trailing gap the file had, so the first table stays where it was.
        stripped = head.rstrip("\r\n")
        orig_gap = head[len(stripped):]
        if not orig_gap:
            rest = eol * 2 if tail else eol
        elif len(orig_gap) <= len(eol):
            rest = eol
        else:
            rest = orig_gap[len(eol):]
        return stripped + eol + new_line + rest + tail

    if not toml_has_table(text, table):
        body = text
        if body and not body.endswith("\n"):
            body += "\n"
        return body + "\n" + f"[{table}]\n" + new_line
    # Append as the table's last line (right after its last non-blank line).
    m = _table_header(table).search(text)
    nxt = _next_header(text, m.end())
    section = text[m.end():nxt]
    lines = section.splitlines(keepends=True)
    pad = 0
    while pad < len(lines) and not lines[len(lines) - 1 - pad].strip():
        pad += 1
    keep = "".join(lines[: len(lines) - pad]) if pad else section
    if keep and not keep.endswith("\n"):
        keep += "\n"
    rest = "".join(lines[len(lines) - pad:]) if pad else ""
    return text[: m.end()] + keep + new_line + rest + text[nxt:]


def toml_delete_key(text: str, key: str, table: str | None) -> str:
    hit = toml_find_key(text, key, table)
    if hit is None:
        return text
    start, end, _ = hit
    return text[:start] + text[end:]


def toml_table_span(text: str, table: str) -> tuple[int, int] | None:
    """(start, end) covering the ``[table]`` header and its body, not the blank tail."""
    m = _table_header(table).search(text)
    if not m:
        return None
    nxt = _next_header(text, m.end())
    lines = text[m.start():nxt].splitlines(keepends=True)
    keep = len(lines)
    while keep > 1 and not lines[keep - 1].strip():
        keep -= 1
    end = m.start() + sum(len(line) for line in lines[:keep])
    return m.start(), end


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _final_newline(text: str, eol: str) -> str:
    """Give ``text`` the final newline the file already had.

    Adding keys to a file that did not end with a newline must not leave that
    newline behind: rollback would then hand back a file that differs by one byte.
    """
    return text if text.endswith(eol) else text + eol


def toml_set_table(text: str, table: str, entries: dict[str, str]) -> str:
    """Create or fully rewrite `[table]` with the given key -> raw-value lines."""
    eol = _eol(text)
    body = f"[{table}]{eol}" + "".join(f"{k} = {v}{eol}" for k, v in entries.items())
    span = toml_table_span(text, table)
    if span is None:
        prefix = text
        if prefix and not prefix.endswith(eol):
            prefix += eol
        if prefix and not prefix.endswith(eol * 2):
            prefix += eol
        return _final_newline(prefix + body, eol)
    start, end = span
    return _final_newline(text[:start] + body + text[end:], eol)


def toml_insert_table(text: str, table: str, entries: dict[str, str], before: str | None) -> str:
    """Insert `[table]` before the table named ``before`` (or append if unknown).

    Used to put a table back where the user had it instead of appending it, which is
    the difference between a one-line rollback diff and a moved section.
    """
    eol = _eol(text)
    body = f"[{table}]{eol}" + "".join(f"{k} = {v}{eol}" for k, v in entries.items())
    at = None
    if before:
        m = _table_header(before).search(text)
        if m:
            at = m.start()
    if at is None:
        return toml_set_table(text, table, entries)
    head = text[:at]
    if not head.endswith(eol):
        head += eol
    if not head.endswith(eol * 2):
        head += eol
    return head + body + eol + text[at:]


def toml_remove_table(text: str, table: str) -> str:
    span = toml_table_span(text, table)
    if span is None:
        return text
    eol = _eol(text)
    start, end = span
    body_end = end - start
    rest = text[start:]
    # Take one separator line with us so removal leaves no double blank line, and
    # prefer the blank *before* the table when there is one.
    if rest[body_end:].startswith(eol) and (start == 0 or text[:start].endswith(eol)):
        end += len(eol)
    elif start > 0 and text[:start].endswith(eol * 2):
        start -= len(eol)
    out = text[:start] + text[end:]
    if start == 0 and out.startswith(eol):
        out = out[len(eol):]
    return out


def toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
