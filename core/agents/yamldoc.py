"""Comment-preserving YAML editing for config *documents* (settings / patch files).

Why this exists
---------------
The YAML files this tool touches are edited by humans and by other tools:

    ~/.dsh/settings.yaml                       (DSH settings, namespaced sections)
    ~/.dsh/profiles/<profile>/cordis.patch.yml (DSH loader patch list)

PyYAML is neither installed nor a dependency we may add, and a full
re-serialisation would destroy comments, key order, anchors and block scalars.
So a document is modelled as a tree of nodes that know their **line and column
span**, and every edit is one of:

  * ``set(path, value)``  -> replace the value span on one physical line
  * ``insert(path, ..)``  -> insert new lines at the end (or head) of a block
  * ``delete(path)``      -> blank the line of one scalar entry

Every line we did not touch is emitted from its original text, byte for byte,
which is what keeps the mojibake comments and the user's own formatting intact.

Parser scope: block mappings, block sequences, flow scalars, quoted scalars,
block scalars (``|``/``>``), comments and blank lines. ``!!js`` expressions stay
opaque scalars, which is exactly what a value we never rewrite needs.

Grammar note: a ``-`` dash and its mapping key may share a line
(``- id: llm-pi-ai``). The loader records such a mapping as starting on the dash
line at column ``dash+2``; every key below it lives at that column, which keeps
the parse a single indentation check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

# --------------------------------------------------------------------------- model


class YNode:
    """Base class for every YAML node."""

    line: int = -1
    indent: int = 0

    def children(self) -> Iterator["YNode"]:
        return iter(())


@dataclass
class YScalar(YNode):
    """A one-line value; ``start``/``end`` are its column span inside ``line``."""

    text: str = ""
    line: int = -1
    indent: int = 0
    start: int = 0
    end: int = 0
    empty: bool = False
    block: bool = False  # `|` / `>` scalar: never rewritten


@dataclass
class YMap(YNode):
    """A block mapping, keeping the original key order."""

    indent: int = 0
    line: int = -1
    items: dict[str, YNode] = field(default_factory=dict)

    def children(self) -> Iterator[YNode]:
        return iter(self.items.values())


@dataclass
class YSeq(YNode):
    """A block sequence, keeping the original item order."""

    indent: int = 0
    line: int = -1
    items: list[YNode] = field(default_factory=list)

    def children(self) -> Iterator[YNode]:
        return iter(self.items)


# --------------------------------------------------------------------------- document

_MAP_ENTRY = re.compile(r"^(\"[^\"]*\"|'[^']*'|[^:#\s][^:]*?)[ \t]*:(?=[ \t]|$)")


class YamlDoc:
    """A YAML document that edits in place."""

    def __init__(self, lines: list[str], newline: str = "\n"):
        self.lines = lines
        self.newline = newline
        self.nodes: dict[tuple, YNode] = {}
        self.root: YNode | None = None
        self._parse()

    @classmethod
    def parse(cls, text: str, newline: str = "\n") -> "YamlDoc":
        return cls(text.splitlines(keepends=True), newline)

    def render(self) -> str:
        return "".join(self.lines)

    # -- queries -------------------------------------------------------
    def node(self, path: tuple) -> YNode | None:
        return self.nodes.get(tuple(path))

    def scalar(self, path: tuple) -> str | None:
        node = self.nodes.get(tuple(path))
        return node.text if isinstance(node, YScalar) and not node.empty else None

    def keys(self, path: tuple) -> list[str]:
        if not path:
            return list(self.root.items) if isinstance(self.root, YMap) else []
        node = self.nodes.get(tuple(path))
        return list(node.items) if isinstance(node, YMap) else []

    def paths(self) -> list[tuple]:
        return list(self.nodes)

    # -- edits ---------------------------------------------------------
    def set(self, path: tuple, value: str) -> bool:
        """Replace one scalar value, keeping the rest of its line (incl. comment)."""
        node = self.nodes.get(tuple(path))
        if not isinstance(node, YScalar) or node.line < 0 or node.block:
            return False
        line = self.lines[node.line]
        nl = _eol(line)
        body = line[: len(line) - len(nl)] if nl else line
        if node.empty:
            self.lines[node.line] = body + " " + value + nl
        else:
            head = body[: node.start]
            tail = body[node.end:]
            if not head.endswith((" ", "\t")):
                # Some parsers hand back a value column that starts ON the key text:
                # rewriting `displayName` in place turned it into `dimagpiee`. A key
                # separator restores the shape of the line.
                head = head.rstrip() + ": "
            self.lines[node.line] = head + value + tail + nl
        return True

    def put(self, path: tuple, value: str) -> bool:
        """Set ``path`` to ``value``, creating the key when the section lacks it."""
        self._resync()
        if isinstance(self.nodes.get(tuple(path)), YScalar):
            return self.set(path, value)
        if self.nodes.get(tuple(path)) is not None:
            return False  # a block where a scalar belongs: leave it to the caller
        parent = tuple(path[:-1])
        if parent and self.nodes.get(parent) is None:
            self.ensure_block(parent)
        self._resync()
        node = self.nodes.get(parent) if parent else self.root
        if node is None:
            raise KeyError("no such path: " + ".".join(map(str, path)))
        if isinstance(node, YMap):
            indent = node.indent + 2
            if node.items:
                # Align with the keys this mapping already has: a section written by
                # another tool may sit two columns deeper than its header suggests.
                indent = min(child.indent for child in node.items.values())
        else:
            indent = node.indent + 2
        at = (_last_line_index(self, parent) + 1) if parent else len(self.lines)
        self.lines.insert(at, " " * indent + f"{path[-1]}: {value}" + self.newline)
        self._resync()
        return True

    def _resync(self) -> None:
        """Reparse this document from its own lines.

        Every edit that inserts lines invalidates the line numbers stored on the
        nodes, so each step that needs a position parses again rather than trusting
        a handle taken before the previous insert.
        """
        fresh = YamlDoc(list(self.lines), self.newline)
        self.lines, self.nodes, self.root = fresh.lines, fresh.nodes, fresh.root

    def delete(self, path: tuple) -> None:
        """Blank the declaration line of one scalar entry."""
        node = self.nodes.get(tuple(path))
        if isinstance(node, YScalar) and node.line >= 0:
            self.lines[node.line] = ""

    def ensure_block(self, path: tuple) -> bool:
        """Create an empty mapping at ``path`` (and its ancestors) when it is missing.

        Returns True when a block was already there. Callers that add a section to a
        file the user may never have written (a fresh settings file) use this instead
        of guessing the layout themselves.
        """
        noderaw = self.root if not path else self.nodes.get(tuple(path))
        if noderaw is None and path:
            # Section headers are words, not paths: the loader nests each plugin's
            # settings under its own `config:`, so `llm-pi-ai` holds providers one
            # level down. Accept the shorthand rather than fail on a plain mistake.
            for depth in (1, 2):
                deeper = tuple(path) + ("config",) * depth
                if deeper in self.nodes:
                    noderaw = self.nodes[deeper]
                    path = deeper
                    break
        node = noderaw
        if isinstance(node, YScalar) and node.empty and 0 <= node.line < len(self.lines):
            # `providers: {}` parses as a mapping, but a section written as `x: {}`
            # still ends its line above the children, so the first child we append
            # would hang off nothing. Fold the braces into a plain header first.
            body = self.lines[node.line].rstrip("\r\n")
            if body.rstrip().endswith(("{}", "[]")):
                cut = body.rstrip()[:-2].rstrip()
                self.lines[node.line] = cut + self.newline
                self._resync()
                node = self.nodes.get(tuple(path)) if path else self.root
        if node is not None and isinstance(node, YMap) and not node.items:
            body = self.lines[node.line].rstrip("\r\n") if 0 <= node.line < len(self.lines) else ""
            if body.rstrip().endswith("{}"):
                self.lines[node.line] = body.rstrip()[:-2].rstrip() + self.newline
                self._resync()
                node = self.nodes.get(tuple(path)) if path else self.root
        if node is not None and not (isinstance(node, YScalar) and not node.empty):
            return True
        if not path:
            return self.root is not None
        if path[:-1] and self.nodes.get(tuple(path[:-1])) is None:
            # The parent section is missing too: create it first, then come back.
            self.ensure_block(tuple(path[:-1]))
        if path[:-1] and self.nodes.get(tuple(path[:-1])) is None:
            return False
        if path[:-1]:
            indent = self.child_indent(tuple(path[:-1]))
        else:
            # Top level: the root mapping's own column, not a child's.
            indent = self.root.indent if isinstance(self.root, YMap) else 0
        new_line = " " * indent + str(path[-1]) + ":"
        if not path[:-1]:
            inputs = [raw.rstrip("\r\n") for raw in self.lines]
            while inputs and not inputs[-1].strip():
                inputs.pop()
            inputs.append(new_line)
            text = self.newline.join(inputs) + self.newline
        else:
            parent = tuple(path[:-1])
            inputs = [raw.rstrip("\r\n") for raw in self.lines]
            if node is not None and _is_empty_body(inputs, node):
                # `providers:` on its own line with nothing under it: keep the line
                # and simply stop there, so the new child follows it directly.
                pos = node.line + 1
            else:
                pos = self.end_of(parent)
            inputs.insert(pos, new_line)
            text = self.newline.join(inputs)
            if not text.endswith(self.newline):
                text += self.newline
        fresh = YamlDoc.parse(text, self.newline)
        self.lines, self.nodes, self.root = fresh.lines, fresh.nodes, fresh.root
        return False

    def insert(self, parent: tuple, block: list[str] | str | list[list[str]], where: str = "end") -> None:
        """Insert a block of lines as children of ``parent``, re-indented to fit.

        ``block`` may be a string (split on newlines, empty lines dropped) or a list
        of lines. Lines are iterated as a list, never as characters — a list of
        one-letter strings is flattened back into lines, never treated as columns.
        """
        block = _flatten(block)
        if _needs_unfold(self, parent):
            # An empty `providers:` next to a sibling would swallow the insert: the
            # new lines would be appended after that sibling and read as its keys.
            self.ensure_block(parent)
        # Re-read after ensure_block: it reparses the document and replaces nodes,
        # so a handle taken before it would still point at the pre-insert layout.
        pnode = self.root if not parent else self.nodes.get(tuple(parent))
        if pnode is None:
            raise KeyError("no such path: " + ".".join(map(str, parent)))
        if isinstance(pnode, YScalar) and not pnode.empty:
            raise KeyError("not a block: " + ".".join(map(str, parent)))
        pos = _insert_position(self, parent, where)
        shifted = _reindent(block, self.child_indent(parent))
        self.lines[pos:pos] = [ln if ln.endswith("\n") else ln + self.newline for ln in shifted]

    def child_indent(self, path: tuple) -> int:
        """The column where a NEW child of ``path`` belongs.

        Only mappings make useful parents: a sequence's "children" are its items
        (whose column is tied to the dash), so asking a sequence for a child column
        would hand back a key column and produce a mapping inside a list.
        """
        node = self.nodes.get(tuple(path)) if path else self.root
        if node is None:
            return 0
        kids = list(node.children()) if isinstance(node, YMap) else []
        if kids:
            # A child's ``indent`` is the column where its VALUE starts, which for a
            # nested mapping is one level deeper than its key. New siblings belong
            # at the key column, so read that from the child's own source line.
            return min(self._key_col(k) for k in kids)
        return node.indent + 2

    def _key_col(self, node: YNode) -> int:
        """The column of ``node``'s key in the source, for sibling alignment."""
        if 0 <= node.line < len(self.lines):
            body = self.lines[node.line].rstrip("\r\n")
            lead = len(body) - len(body.lstrip(" "))
            if body.lstrip().startswith("- "):
                return lead
            return lead
        return node.indent

    def text_of(self, path: tuple) -> list[str]:
        """The exact source lines that belong to ``path`` (dedented to its column)."""
        node = self.nodes.get(tuple(path))
        if node is None:
            raise KeyError("no such path: " + ".".join(map(str, path)))
        first = node.line
        last = _end_line(node)
        out: list[str] = []
        for i in range(first, last + 1):
            raw = self.lines[i]
            if not raw.strip() or raw.lstrip().startswith("#"):
                out.append("")
                continue
            body = raw.rstrip("\r\n")
            cut = node.indent if node.indent <= len(body) - len(body.lstrip(" ")) else 0
            out.append(body[cut:])
        return out

    def graft(self, parent: tuple, child_lines: list[str], prefix: list[str], where: str = "end") -> int:
        """Insert one child entry into ``parent`` without reformatting it.

        ``child_lines`` is the (dedented) source of an existing node being moved or
        copied; ``prefix`` is the text its first line should gain (``key:``). Both
        are laid out at ``child_indent(parent)``, so a moved subtree keeps every
        nested line exactly as it was, only shifted by a constant column count.
        """
        pnode = self.nodes.get(tuple(parent))
        if pnode is None:
            raise KeyError("no such path: " + ".".join(map(str, parent)))
        indent = self.child_indent(parent)
        block: list[str] = []
        for i, line in enumerate(child_lines):
            if not line.strip():
                block.append("")
            else:
                block.append(" " * indent + (prefix[0] + line if i == 0 else line))
        pos = self.end_of(parent) if where == "end" else self.start_of_children(parent)
        self.lines[pos:pos] = [ln if ln.endswith("\n") else ln + self.newline for ln in block]
        if pnode.indent < indent:
            # A parent with nothing but empty values (`providers:`) has no children
            # to learn its open column from; record it so later edits agree.
            pnode.indent = max(0, indent - 2)
        return pos

    def end_of(self, path: tuple) -> int:
        """Index just past the last physical line that belongs to ``path``."""
        node = self.root if not path else self.nodes.get(tuple(path))
        if node is None:
            raise KeyError("no such path: " + ".".join(map(str, path)))
        if isinstance(node, YScalar):
            return node.line + 1
        last = node.line
        for k in node.children():
            last = max(last, _end_line(k))
        return last + 1

    def start_of_children(self, path: tuple) -> int:
        node = self.root if not path else self.nodes.get(tuple(path))
        if node is None:
            raise KeyError("no such path: " + ".".join(map(str, path)))
        kids = list(node.children())
        if not kids:
            return node.line + 1
        return min(k.line if k.line >= 0 else node.line + 1 for k in kids)

    def delete_block(self, path: tuple) -> bool:
        """Remove a whole key/entry and its subtree, plus a blank line we caused."""
        if not path:
            return False
        node = self.nodes.get(tuple(path))
        if isinstance(node, YScalar) and not node.empty:
            return False
        first = _last_key_line(self, tuple(path))
        if first < 0:
            return False
        # A section whose children were already deleted ("providers:" with nothing
        # under it) still has to go: the parser made it an empty scalar, so fall
        # back to the key line and take any deeper lines that trail it.
        last = _end_line(node) if node is not None and not isinstance(node, YScalar) else first
        last = max(last, first)
        if last < first:
            last = first
        del self.lines[first:last + 1]
        fresh = YamlDoc(list(self.lines), self.newline)
        self.lines, self.nodes, self.root = fresh.lines, fresh.nodes, fresh.root
        return True

    # -- parsing -------------------------------------------------------
    def _parse(self) -> None:
        rows: list[_Row] = []
        for i, raw in enumerate(self.lines):
            body = raw.rstrip("\r\n")
            if not body.strip() or body.lstrip().startswith("#"):
                continue
            rows.append(_Row(i, body))
        pos = 0
        if rows:
            self.root, pos = _parse_block(self, rows, pos, rows[0].indent, ())


@dataclass
class _Row:
    line: int
    text: str

    @property
    def indent(self) -> int:
        return len(self.text) - len(self.text.lstrip(" "))


def _end_line(node: YNode) -> int:
    if isinstance(node, YScalar):
        return node.line
    last = node.line
    for k in node.children():
        last = max(last, _end_line(k))
    return last


def _is_item(text: str) -> bool:
    body = text.lstrip(" ")
    return body == "-" or body.startswith("- ")


def _parse_block(doc: YamlDoc, rows: list[_Row], pos: int, indent: int, path: tuple) -> tuple[YNode | None, int]:
    if pos >= len(rows):
        return None, pos
    if _is_item(rows[pos].text):
        return _parse_seq(doc, rows, pos, indent, path)
    return _parse_map(doc, rows, pos, indent, path)


def _parse_seq(doc: YamlDoc, rows: list[_Row], pos: int, indent: int, path: tuple) -> tuple[YSeq, int]:
    seq = YSeq(indent=indent, line=rows[pos].line)
    index = 0
    while pos < len(rows):
        row = rows[pos]
        if row.indent != indent or not _is_item(row.text):
            break
        rest = row.text[indent + 1:]
        item_path = path + (index,)
        probe = rest.strip()
        if probe and _MAP_ENTRY.match(probe):
            # `- key: value` -> mapping starting on this line at column indent+2
            offset = indent + 1 + (len(rest) - len(rest.lstrip(" ")))
            node, pos = _parse_map(doc, rows, pos, offset, item_path, dash_line=row.line)
        else:
            value_col = indent + 1 + (len(rest) - len(rest.lstrip(" ")))
            node, pos = _parse_scalar(rows, pos, value_col, item_path, indent)
        seq.items.append(node)
        doc.nodes[item_path] = node
        index += 1
    return seq, pos


def _parse_map(
    doc: YamlDoc, rows: list[_Row], pos: int, indent: int, path: tuple, dash_line: int | None = None
) -> tuple[YMap, int]:
    """Parse a block mapping whose keys sit at ``indent``.

    ``dash_line`` marks a mapping that opens on a sequence item's dash line: its
    first key's text starts at ``indent`` although the line itself is indented less.
    """
    first_line = dash_line if dash_line is not None else rows[pos].line
    node = YMap(indent=indent, line=first_line)
    while pos < len(rows):
        row = rows[pos]
        on_dash = dash_line is not None and row.line == dash_line
        if on_dash:
            offset = indent
        else:
            # A block mapping is a mapping only while every key sits at the SAME
            # column: one dedent (or any deeper line that is not a value we
            # already consumed) ends it.
            if row.indent != indent:
                break
            offset = indent
        text = row.text[offset:]
        if _is_item(text) or not text.strip() or text.lstrip().startswith("#"):
            # A dash, a blank line or a comment cell carries no key: reading it as
            # one made an appended block land half a column off (`di: magpiee`).
            break
        m = _MAP_ENTRY.match(text)
        if not m:
            break
        dash_line = None  # only the first key shares the dash line
        key = _unquote(m.group(1))
        child_path = path + (key,)
        after = text[m.end():]
        value_col = offset + m.end() + (len(after) - len(after.lstrip(" ")))
        block_scalar = after.lstrip().startswith(("|", ">"))
        if not after.strip():
            key_line = row.line
            pos, entry = _nested_or_empty(doc, rows, pos, offset, child_path)
            if not isinstance(entry, YScalar):
                # The block's own line is the `key:` line, not its first child's,
                # so spans (and therefore whole-block deletes) start where they must.
                entry.line = key_line
        else:
            entry, pos = _parse_scalar(rows, pos, value_col, child_path, offset)
        if block_scalar and isinstance(entry, YScalar):
            # A block scalar owns every deeper line, so the mapping cannot resume
            # until one dedents; skip them here rather than parsing them as keys.
            while pos < len(rows) and rows[pos].indent > offset:
                pos += 1
        node.items[key] = entry
        doc.nodes[child_path] = entry
    return node, pos


def _nested_or_empty(doc: YamlDoc, rows: list[_Row], pos: int, offset: int, path: tuple) -> tuple[int, YNode]:
    nxt = pos + 1
    if nxt < len(rows):
        row = rows[nxt]
        if row.indent > offset or (row.indent == offset and _is_item(row.text)):
            start_indent = row.indent if row.indent > offset else offset
            nested, after = _parse_block(doc, rows, nxt, start_indent, path)
            if nested is not None:
                return after, nested
    row = rows[pos]
    return pos + 1, YScalar(text="", line=row.line, indent=offset, start=len(row.text), end=len(row.text), empty=True)


def _parse_scalar(rows: list[_Row], pos: int, value_col: int, path: tuple, indent: int) -> tuple[YScalar, int]:
    row = rows[pos]
    raw = row.text[value_col:]
    if raw.lstrip().startswith(("|", ">")):
        end = pos + 1
        while end < len(rows) and rows[end].indent > indent:
            end += 1
        return YScalar(text=raw.strip(), line=row.line, indent=indent, start=value_col, end=len(row.text), block=True), end
    value, _comment = _split_comment(raw)
    if not value.strip():
        return YScalar(text="", line=row.line, indent=indent, start=len(row.text), end=len(row.text), empty=True), pos + 1
    if value.strip() == "{}":
        # `providers: {}` is an empty mapping we can grow children into; treating it
        # as a scalar would make the first insert fail on a perfectly normal file.
        return YMap(indent=indent + 2, line=row.line), pos + 1
    if value.strip() == "[]":
        return YSeq(indent=indent, line=row.line), pos + 1
    lead = len(value) - len(value.lstrip(" "))
    start = value_col + lead
    end = start + len(value.rstrip(" "))
    return YScalar(text=value.strip(), line=row.line, indent=indent, start=start, end=end), pos + 1


# --------------------------------------------------------------------------- helpers

_QUOTES = ('"', "'")


def _unquote(key: str) -> str:
    key = key.strip()
    if len(key) >= 2 and key[0] in _QUOTES and key[-1] == key[0]:
        return key[1:-1]
    return key


def _split_comment(raw: str) -> tuple[str, str]:
    """Split ``value   # comment`` respecting quotes; returns (value, comment)."""
    quote = None
    for i, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in _QUOTES:
            quote = ch
        elif ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            return raw[:i], raw[i:]
    return raw, ""


def _eol(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _reindent(block: list[str], indent: int) -> list[str]:
    widths = [len(ln) - len(ln.lstrip(" ")) for ln in block if ln.strip()]
    shift = min(widths) if widths else 0
    return [(" " * indent + ln[shift:]) if ln.strip() else ln for ln in block]


def _flatten(block: list[str]) -> list[str]:
    """Accept a line list or raw text; never iterate a string character by character."""
    if isinstance(block, str):
        return [line for line in block.splitlines() if line.strip()]
    out: list[str] = []
    for item in block:
        if isinstance(item, str):
            out.extend(line for line in item.splitlines() if line.strip())
        else:
            out.extend(item)
    return out


def _insert_position(doc: "YamlDoc", parent: tuple, where: str = "end") -> int:
    """Where new children of ``parent`` may be spliced in.

    ``end_of`` stops at the close of the last child, which is the wrong place twice:
    a blank line there belongs to the gap between the parent and its sibling, and a
    sibling's own key line is even worse — pushing it under our block moves it into
    the parent. Both cases step back to just after the last child that is really
    there, so the sibling (and its blank line) stays put.
    """
    if where != "end":
        return doc.start_of_children(parent)
    pos = doc.end_of(parent)
    if pos >= len(doc.lines):
        return pos
    here = doc.lines[pos].rstrip("\r\n")
    if not here.strip():
        # A blank line closes the block for YAML purposes; splicing after it would
        # give our child to whatever mapping comes next instead of to the parent.
        return _last_line_index(doc, parent) + 1
    # Splicing right before this line is only safe while it belongs to the parent.
    # A line indented at or left of the parent's own column is its sibling: adding
    # a line above it would push it down, and it would then read as a child.
    pnode = doc.root if not parent else doc.nodes.get(tuple(parent))
    pcol = pnode.indent if pnode is not None and not isinstance(pnode, YScalar) else -1
    if len(here) - len(here.lstrip(" ")) <= pcol:
        return _last_line_index(doc, parent) + 1
    return pos


def _last_key_line(doc: "YamlDoc", path: tuple) -> int:
    """The physical line that holds the ``key:`` for ``path`` (-1 if it has none).

    ``YNode.line`` is the key line for maps and sequences, but for an *empty*
    scalar produced by a bare ``key:`` header it can point at the header while
    ``end``/``start`` stay on the value column, so callers that delete a block
    must not trust it blindly. This helper reconciles the two.
    """
    node = doc.nodes.get(tuple(path))
    if node is None:
        return -1
    if isinstance(node, YScalar) and not node.empty:
        return node.line
    want = str(path[-1])
    ln = node.line
    if 0 <= ln < len(doc.lines) and doc.lines[ln].lstrip().startswith(want + ":"):
        return ln
    # Node line drifted (a block was re-parsed after edits): scan upward for the
    # nearest line at the key's own indentation that spells the key.
    for i in range(ln, max(-1, ln - 64), -1):
        if 0 <= i < len(doc.lines):
            body = doc.lines[i]
            stripped = body.lstrip()
            if stripped.startswith(want + ":") or stripped.startswith(want + " :"):
                return i
    return ln


def _last_line_index(doc: "YamlDoc", path: tuple) -> int:
    """Index of the last physical line that belongs to ``path``."""
    node = doc.root if not path else doc.nodes.get(tuple(path))
    if node is None:
        raise KeyError("no such path: " + ".".join(map(str, path)))
    if isinstance(node, YScalar):
        return node.line
    last = node.line
    for k in node.children():
        last = max(last, _end_line(k))
    return last


def _is_empty_body(lines: list[str], node: "YNode") -> bool:
    """Is the line at ``node.line`` a header with no body under it?"""
    if node.line < 0 or node.line >= len(lines):
        return False
    for line in lines[node.line + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        return len(line) - len(line.lstrip(" ")) <= node.indent
    return True


def _needs_unfold(doc: "YamlDoc", parent: tuple) -> bool:
    """Does ``parent`` have no block body of its own?

    True for an inline ``{}``/``[]`` and for a section whose next line belongs to a
    sibling. Both need a body before a child can be inserted at "end of parent".
    """
    if parent and doc.nodes.get(tuple(parent)) is None:
        return False  # missing entirely; ensure_block creates the ancestors
    node = doc.root if not parent else doc.nodes.get(tuple(parent))
    if node is None or isinstance(node, YScalar):
        return False
    if node.line >= 0 and node.line < len(doc.lines):
        body = doc.lines[node.line].rstrip("\r\n")
        if body.rstrip().endswith(("{}", "[]")):
            return True
    kids = list(node.children()) if isinstance(node, YMap) else []
    return not kids
