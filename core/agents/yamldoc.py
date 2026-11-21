# -*- coding: utf-8 -*-
"""保留注释的 YAML 文档编辑（settings / patch 文件用）。

为什么自己写：这里碰的两个文件既有人的手笔也有别工具的笔迹 ——

    ~/.dsh/settings.yaml                        （DSH 运行时设置，带命名空间的段）
    ~/.dsh/profiles/<profile>/cordis.patch.yml  （DSH 加载器的插件列表）

本机既没装 PyYAML，也不该为它加依赖；整份重新序列化又会毁掉注释、键序、锚点和块
标量。所以把文档建模成「知道自己行/列区间的节点树」，每次编辑只做三件事之一：

  * ``set(path, value)``   —— 在一行上替换值的区间
  * ``insert(path, ..)``   —— 在块尾（或头部）插入整行
  * ``delete(path)``       —— 清空某个标量条目所在的行

没动过的行按原文本逐字节输出，乱码注释和用户排版才留得住。

解析范围：块映射、块序列、流式标量、引号标量、块标量（``|``/``>``）、注释与空行。
``!!js`` 表达式当作不透明标量 —— 我们不会重写的值本来就该这样。

语法提醒：``-`` 与它后面的映射键可以同行（``- id: llm-pi-ai``）。这种映射记作「起于
dash 行、列在 dash+2」，下面所有键都在那一列，判断缩进只需一次比较。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

# --------------------------------------------------------------------------- 节点


class YNode:
    """所有 YAML 节点的基类。"""

    line: int = -1
    indent: int = 0

    def children(self) -> Iterator["YNode"]:
        return iter(())


@dataclass
class YScalar(YNode):
    """单行值；``start``/``end`` 是它在 ``line`` 里的列区间。"""

    text: str = ""
    line: int = -1
    indent: int = 0
    start: int = 0
    end: int = 0
    empty: bool = False
    block: bool = False  # `|` / `>` 块标量：永不重写


@dataclass
class YMap(YNode):
    """块映射，保留原键序。"""

    indent: int = 0
    line: int = -1
    items: dict[str, YNode] = field(default_factory=dict)

    def children(self) -> Iterator[YNode]:
        return iter(self.items.values())


@dataclass
class YSeq(YNode):
    """块序列，保留原项序。"""

    indent: int = 0
    line: int = -1
    items: list[YNode] = field(default_factory=list)

    def children(self) -> Iterator[YNode]:
        return iter(self.items)


# --------------------------------------------------------------------------- 文档

_MAP_ENTRY = re.compile(r"^(\"[^\"]*\"|'[^']*'|[^:#\s][^:]*?)[ \t]*:(?=[ \t]|$)")


class YamlDoc:
    """就地编辑的 YAML 文档。"""

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

    # -- 查询 ---------------------------------------------------------
    def _lookup(self, path) -> YNode | None:
        """``self.nodes`` 按真实下标取；路径里带 ``{"id": x}`` 时先解析。"""
        if not path:
            return self.root
        if any(isinstance(step, dict) for step in path):
            hit = self.entry(path)
            return None if hit is None else hit[1]
        return self.nodes.get(tuple(path))

    def _real(self, path) -> tuple:
        """把可能带 ``{"id": x}`` 的路径换成真实下标路径；解析不出来就原样返回。"""
        path = tuple(path)
        if not any(isinstance(step, dict) for step in path):
            return path
        hit = self.entry(path)
        if hit is not None:
            return hit[0]
        # 末段还没生成：只要前缀能解析，就把前缀换成真实下标，末段原样留着。
        for cut in range(len(path) - 1, 0, -1):
            if not any(isinstance(step, dict) for step in path[:cut]):
                continue
            hit = self.entry(path[:cut])
            if hit is not None:
                return hit[0] + path[cut:]
        return path

    @staticmethod
    def _hashable(path) -> tuple | None:
        """把可能还带 ``{"id": x}`` 的路径变成可哈希的真实键。"""
        out = []
        for step in tuple(path):
            out.append(step if not isinstance(step, dict) else tuple(step.items()))
        try:
            hash(tuple(out))
        except TypeError:  # pragma: no cover - 防御：调用方给了不可哈希的东西
            return None
        return tuple(out)

    def node(self, path: tuple) -> YNode | None:
        if any(isinstance(step, dict) for step in path):
            hit = self.entry(path)
            return None if hit is None else hit[1]
        return self.nodes.get(tuple(path))

    def entry(self, path: tuple) -> tuple[tuple, YNode] | None:
        """把「序列里的某项」按 id 而不是序号寻址。

        DSH 的 profile 是插件列表 ``- id: llm-pi-ai``；按 ``(1,)`` 定位，用户插一个
        插件就全错。序列那层允许写 ``{"id": "llm-pi-ai"}``，这里换成真实下标。
        返回 (真实路径, 节点)，找不到给 None。
        """
        current: YNode | None = self.root
        real: list = []
        for step in path:
            if current is None:
                return None
            if isinstance(step, dict):
                wanted = step.get("id")
                found = None
                if isinstance(current, YSeq):
                    for index, item in enumerate(current.items):
                        if isinstance(item, YMap):
                            key_node = item.items.get("id")
                            if (isinstance(key_node, YScalar)
                                    and key_node.text.strip("'\"") == wanted):
                                found = index
                                break
                elif isinstance(current, YMap) and wanted in current.items:
                    found = wanted
                if found is None:
                    return None
                real.append(found)
                current = current.items[found]
                continue
            if isinstance(current, YSeq):
                index = step if isinstance(step, int) else -1
                if not (0 <= index < len(current.items)):
                    return None
                real.append(index)
                current = current.items[index]
                continue
            if isinstance(current, YMap) and step in current.items:
                real.append(step)
                current = current.items[step]
                continue
            return None
        return tuple(real), current

    def scalar(self, path: tuple) -> str | None:
        node = self._lookup(path)
        return node.text if isinstance(node, YScalar) and not node.empty else None

    def keys(self, path: tuple) -> list[str]:
        if not path:
            return list(self.root.items) if isinstance(self.root, YMap) else []
        node = self._lookup(path)
        return list(node.items) if isinstance(node, YMap) else []

    def paths(self) -> list[tuple]:
        return list(self.nodes)

    # -- 编辑 ---------------------------------------------------------
    def set(self, path: tuple, value: str) -> bool:
        """替换一个标量值，其余部分（含行尾注释）不动。"""
        node = self._lookup(path)
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
                # 值列有可能从键文本上开始：就地重写 `displayName` 曾把它变成
                # `dimagpiee`。补一个键值分隔符把这一行的形状修回来。
                head = head.rstrip() + ": "
            self.lines[node.line] = head + value + tail + nl
        return True

    def put(self, path: tuple, value: str) -> bool:
        """把 ``path`` 设为 ``value``，段里没这个键就建一个。"""
        self._resync()
        real = self._real(path)
        if isinstance(self.nodes.get(tuple(real)), YScalar):
            return self.set(real, value)
        if self.nodes.get(tuple(real)) is not None:
            return False  # 该放标量的位置是个块：交给调用方
        parent = tuple(real[:-1])
        if parent and self.nodes.get(parent) is None:
            self.ensure_block(parent)
        self._resync()
        real = self._real(path)
        parent = tuple(real[:-1])
        node = self.nodes.get(parent) if parent else self.root
        if node is None:
            raise KeyError("no such path: " + ".".join(map(str, path)))
        if isinstance(node, YMap):
            indent = node.indent + 2
            if node.items:
                # 对齐这个映射已有的键：别的工具写的段可能比表头深两列。
                indent = min(child.indent for child in node.items.values())
        else:
            indent = node.indent + 2
        at = (_last_line_index(self, parent) + 1) if parent else len(self.lines)
        self.lines.insert(at, " " * indent + f"{real[-1]}: {value}" + self.newline)
        self._resync()
        return True

    def _resync(self) -> None:
        """用自己当前的行重新解析。

        任何插入都会作废节点上的行号，所以每步要定位时都重新解析，不信上一次插入前
        拿到的句柄。
        """
        fresh = YamlDoc(list(self.lines), self.newline)
        self.lines, self.nodes, self.root = fresh.lines, fresh.nodes, fresh.root

    def delete(self, path: tuple) -> None:
        """清空某个标量条目所在的行。"""
        node = self._lookup(path)
        if isinstance(node, YScalar) and node.line >= 0:
            self.lines[node.line] = ""

    def ensure_block(self, path: tuple) -> bool:
        """``path`` 缺失时建空映射（祖先一起建）。

        已存在则返回 True。往用户可能从没写过的文件里加段时调用它，而不是自己猜排版。
        """
        noderaw = self._lookup(path)
        if noderaw is None and path:
            # 路径可能用 id 而不是序号指向序列项，先解析再断定这段不存在。
            hit = self.entry(path)
            if hit is not None:
                path, noderaw = hit
        if noderaw is None and path:
            # 段名不是完整路径：加载器把每个插件的设置嵌在它自己的 `config:` 下，所以
            # `llm-pi-ai` 的 providers 还深一层。容忍这种简写。
            for depth in (1, 2):
                deeper = tuple(path) + ("config",) * depth
                hit = self._lookup(deeper)
                if hit is not None:
                    noderaw = hit
                    path = deeper
                    break
        node = noderaw
        if isinstance(node, YScalar) and node.empty and 0 <= node.line < len(self.lines):
            # `providers: {}` 虽是映射，但写成 `x: {}` 的段在这一行就结束了，追加的
            # 第一个子行会悬空。先把括号折成普通表头。
            body = self.lines[node.line].rstrip("\r\n")
            if body.rstrip().endswith(("{}", "[]")):
                cut = body.rstrip()[:-2].rstrip()
                self.lines[node.line] = cut + self.newline
                self._resync()
                node = self._lookup(path)
        if node is not None and isinstance(node, YMap) and not node.items:
            body = self.lines[node.line].rstrip("\r\n") if 0 <= node.line < len(self.lines) else ""
            if body.rstrip().endswith("{}"):
                self.lines[node.line] = body.rstrip()[:-2].rstrip() + self.newline
                self._resync()
                node = self._lookup(path)
        if node is not None and not (isinstance(node, YScalar) and not node.empty):
            return True
        if not path:
            return self.root is not None
        empty_file = not any(line.strip() for line in self.lines)
        if not empty_file:
            if path[:-1] and self._lookup(tuple(path[:-1])) is None:
                # 父段也不在：先建父段再回来。
                self.ensure_block(tuple(path[:-1]))
            if path[:-1] and self._lookup(tuple(path[:-1])) is None:
                return False
        if path[:-1]:
            indent = self.child_indent(tuple(path[:-1]))
        else:
            # 顶层：用根映射自己的列，不是子节点的列。
            indent = self.root.indent if isinstance(self.root, YMap) else 0
        new_line = " " * indent + str(path[-1]) + ":"
        if isinstance(self.root, YSeq) and path and isinstance(path[0], dict):
            # 根是列表（DSH 的 profile 就是插件列表）：新插件只能以 `- id: x` 的形式
            # 出现，否则整个文件会被读成映射，加载器直接失败。
            parts = [str(step.get("id", step)) if isinstance(step, dict) else str(step)
                     for step in path]
            inputs = [raw.rstrip("\r\n") for raw in self.lines]
            while inputs and not inputs[-1].strip():
                inputs.pop()
            body = ["- id: " + parts[0]]
            body += [" " * (2 * index) + part + ":" for index, part in enumerate(parts[1:], start=1)]
            inputs.extend(body)
            text = self.newline.join(inputs) + self.newline
        elif not self.lines or not any(line.strip() for line in self.lines):
            # 空文件没有根可挂，把整条目完整拼出来。
            parts = [str(step.get("id", step)) if isinstance(step, dict) else str(step)
                     for step in path]
            text = self.newline.join(
                ["- id: " + parts[0]]
                + [" " * (2 * index) + part + ":" for index, part in enumerate(parts[1:], start=1)]
            ) + self.newline
        elif not path[:-1]:
            inputs = [raw.rstrip("\r\n") for raw in self.lines]
            while inputs and not inputs[-1].strip():
                inputs.pop()
            inputs.append(new_line)
            text = self.newline.join(inputs) + self.newline
        else:
            parent = tuple(path[:-1])
            inputs = [raw.rstrip("\r\n") for raw in self.lines]
            if node is not None and _is_empty_body(inputs, node):
                # 独占一行的 `providers:` 下面什么都没有：保留这一行，新子项紧随其后。
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
        """把一段行插成 ``parent`` 的子项，按子项列重新缩进。

        ``block`` 可以是字符串（按行切、丢空行）或行列表。永远按行迭代，绝不按字符
        —— 单字符列表会被拼回行，而不是被当成列。
        """
        block = _flatten(block)
        # id 步骤只在开头解析一次：下面的展开、定位、缩进全用真实下标，而其中一步会
        # 重新解析。
        parent = self._real(parent) if any(isinstance(s, dict) for s in parent) else tuple(parent)
        if _needs_unfold(self, parent):
            # 旁边有兄弟键的空 `providers:` 会吞掉插入内容：新行会追加到兄弟后面被读成
            # 它的键。
            self.ensure_block(parent)
        # ensure_block 之后要重新取节点：它重解析并替换了节点，之前的句柄还指向旧布局。
        pnode = self._lookup(parent)
        if pnode is None:
            raise KeyError("no such path: " + ".".join(map(str, parent)))
        if isinstance(pnode, YScalar) and not pnode.empty:
            raise KeyError("not a block: " + ".".join(map(str, parent)))
        pos = _insert_position(self, parent, where)
        shifted = _reindent(block, self.child_indent(parent))
        self.lines[pos:pos] = [ln if ln.endswith("\n") else ln + self.newline for ln in shifted]

    def child_indent(self, path: tuple) -> int:
        """``path`` 的新子项该放在哪一列。

        只有映射适合当父节点；序列的「子项」是列表项，列由 dash 决定。
        """
        node = self._lookup(path)
        if node is None and path:
            hit = self.entry(path)
            if hit is not None:
                node = hit[1]
        if node is None:
            return 0
        children = list(node.children())
        if isinstance(node, YSeq):
            # 序列的子项是 `- id: x` 那类映射；它们的键列由 dash 位置决定。
            maps = [c for c in children if isinstance(c, YMap)]
            if maps:
                return min(self._key_col(c) for c in maps)
            return node.indent + 2
        kids = [c for c in children if isinstance(c, YMap)]
        if kids:
            # 子节点的 ``indent`` 是它值所在的列，嵌套映射时比键深一层。新兄弟属于键列，
            # 所以从子节点自己的源码行读。
            return min(self._key_col(k) for k in kids)
        return node.indent + 2
    def _key_col(self, node: YNode) -> int:
        """``node`` 的键在源码里的列，用于对齐兄弟。"""
        if 0 <= node.line < len(self.lines):
            body = self.lines[node.line].rstrip("\r\n")
            lead = len(body) - len(body.lstrip(" "))
            if body.lstrip().startswith("- "):
                return lead
            return lead
        return node.indent

    def text_of(self, path: tuple) -> list[str]:
        """属于 ``path`` 的原始源码行（按它的列去缩进）。"""
        node = self._lookup(path)
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
        """往 ``parent`` 里塞一个子条目，不重排它。

        ``child_lines`` 是被搬动/复制的节点的（已去缩进）源码，``prefix`` 是首行要补的
        ``key:``。整体排到 ``child_indent(parent)``，搬过去的子树只有统一平移。
        """
        parent = self._real(parent)
        pnode = self._lookup(parent)
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
            # 只有空值的父节点（`providers:`）没有子项可参考列，记下来让后续编辑一致。
            pnode.indent = max(0, indent - 2)
        return pos

    def end_of(self, path: tuple) -> int:
        """``path`` 最后一行之后的下标。"""
        node = self._lookup(path)
        if node is None:
            raise KeyError("no such path: " + ".".join(map(str, path)))
        if isinstance(node, YScalar):
            return node.line + 1
        last = node.line
        for k in node.children():
            last = max(last, _end_line(k))
        return last + 1

    def start_of_children(self, path: tuple) -> int:
        node = self._lookup(path)
        if node is None:
            raise KeyError("no such path: " + ".".join(map(str, path)))
        kids = list(node.children())
        if not kids:
            return node.line + 1
        return min(k.line if k.line >= 0 else node.line + 1 for k in kids)

    def delete_block(self, path: tuple) -> bool:
        """删掉整个键/条目及其子树，连同我们造成的空行。"""
        if not path:
            return False
        node = self._lookup(path)
        if isinstance(node, YScalar) and not node.empty:
            return False
        first = _last_key_line(self, self._real(path))
        if first < 0:
            return False
        # 子项已删空的段（光秃秃的 `providers:`）也得删：解析器把它当空标量，于是退回
        # 键行并带走尾随的更深行。
        last = _end_line(node) if node is not None and not isinstance(node, YScalar) else first
        last = max(last, first)
        if last < first:
            last = first
        del self.lines[first:last + 1]
        fresh = YamlDoc(list(self.lines), self.newline)
        self.lines, self.nodes, self.root = fresh.lines, fresh.nodes, fresh.root
        return True

    # -- 解析 ---------------------------------------------------------
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
            # `- key: value` -> 这一行开始、列在 indent+2 的映射
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
    """解析键都在 ``indent`` 列的块映射。

    ``dash_line`` 标记起于列表项 dash 行的映射：第一个键的文本从 ``indent`` 开始，
    尽管那一行本身的缩进更浅。
    """
    first_line = dash_line if dash_line is not None else rows[pos].line
    node = YMap(indent=indent, line=first_line)
    while pos < len(rows):
        row = rows[pos]
        on_dash = dash_line is not None and row.line == dash_line
        if on_dash:
            offset = indent
        else:
            # 块映射只在其所有键都同一列时成立：一次减缩进（或任何不属于已消费值的更深
            # 行）就结束它。
            if row.indent != indent:
                break
            offset = indent
        text = row.text[offset:]
        if _is_item(text) or not text.strip() or text.lstrip().startswith("#"):
            # dash 行、空行、注释行都不含键：当成键读曾让追加的块错半列
            # （`di: magpiee`）。
            break
        m = _MAP_ENTRY.match(text)
        if not m:
            break
        dash_line = None  # 只有第一个键与 dash 同行
        key = _unquote(m.group(1))
        child_path = path + (key,)
        after = text[m.end():]
        value_col = offset + m.end() + (len(after) - len(after.lstrip(" ")))
        block_scalar = after.lstrip().startswith(("|", ">"))
        if not after.strip():
            key_line = row.line
            pos, entry = _nested_or_empty(doc, rows, pos, offset, child_path)
            if not isinstance(entry, YScalar):
                # 块自己的行是 `key:` 那一行，不是它第一个子行 —— 区间（以及整块删除）
                # 才从该起的地方起。
                entry.line = key_line
        else:
            entry, pos = _parse_scalar(rows, pos, value_col, child_path, offset)
        if block_scalar and isinstance(entry, YScalar):
            # 块标量占有所有更深行，直到一次减缩进才结束；直接跳过，别当键解析。
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
        # `providers: {}` 是可以长子的空映射；当标量处理会让第一次插入在正常文件上失败。
        return YMap(indent=indent + 2, line=row.line), pos + 1
    if value.strip() == "[]":
        return YSeq(indent=indent, line=row.line), pos + 1
    lead = len(value) - len(value.lstrip(" "))
    start = value_col + lead
    end = start + len(value.rstrip(" "))
    return YScalar(text=value.strip(), line=row.line, indent=indent, start=start, end=end), pos + 1


# --------------------------------------------------------------------------- 工具函数

_QUOTES = ('"', "'")


def _unquote(key: str) -> str:
    key = key.strip()
    if len(key) >= 2 and key[0] in _QUOTES and key[-1] == key[0]:
        return key[1:-1]
    return key


def _split_comment(raw: str) -> tuple[str, str]:
    """按引号规则切开 ``value   # comment``，返回 (值, 注释)。"""
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
    """接受行列表或原始文本；绝不按字符迭代字符串。"""
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
    """``parent`` 的新子项可以插在哪里。

    ``end_of`` 停在最后一个子项结束处，这里有两种情况会错：那里的空行属于父与兄弟之间
    的间隔；那里若是兄弟的键行更糟 —— 把它顶下去就变成父的子项。两种都退回「真正在的
    最后一行之后」，让兄弟和它的空行原地不动。
    """
    if where != "end":
        return doc.start_of_children(parent)
    pos = doc.end_of(parent)
    if pos >= len(doc.lines):
        return pos
    here = doc.lines[pos].rstrip("\r\n")
    if not here.strip():
        # 空行在 YAML 里就结束了这个块；插在它后面会把子项送给后面的映射而不是父节点。
        return _last_line_index(doc, parent) + 1
    # 只有这一行属于父节点时，插在它前面才安全。缩进不大于父列的键是兄弟：在它上面加行
    # 会把它顶下去，从而被读成子项。
    pnode = doc._lookup(tuple(parent))
    pcol = pnode.indent if pnode is not None and not isinstance(pnode, YScalar) else -1
    if len(here) - len(here.lstrip(" ")) <= pcol:
        return _last_line_index(doc, parent) + 1
    return pos


def _last_key_line(doc: "YamlDoc", path: tuple) -> int:
    """``path`` 的 ``key:`` 所在的物理行（没有则 -1）。

    ``YNode.line`` 对映射与序列就是键行，但对光秃秃 ``key:`` 产生的空标量，它可能指向
    表头而 ``start``/``end`` 还停在值列，所以删块的调用方不能盲信它。这里把两者对齐。
    """
    node = doc._lookup(tuple(path))
    if node is None:
        return -1
    if isinstance(node, YScalar) and not node.empty:
        return node.line
    want = str(path[-1])
    ln = node.line
    if 0 <= ln < len(doc.lines) and doc.lines[ln].lstrip().startswith(want + ":"):
        return ln
    # 节点行号漂了（编辑后块被重解析过）：向上扫最近一个拼着该键的行。
    for i in range(ln, max(-1, ln - 64), -1):
        if 0 <= i < len(doc.lines):
            body = doc.lines[i]
            stripped = body.lstrip()
            if stripped.startswith(want + ":") or stripped.startswith(want + " :"):
                return i
    return ln


def _last_line_index(doc: "YamlDoc", path: tuple) -> int:
    """属于 ``path`` 的最后一个物理行的下标。"""
    node = doc._lookup(tuple(path))
    if node is None:
        raise KeyError("no such path: " + ".".join(map(str, path)))
    if isinstance(node, YScalar):
        return node.line
    last = node.line
    for k in node.children():
        last = max(last, _end_line(k))
    return last


def _is_empty_body(lines: list[str], node: "YNode") -> bool:
    """``node.line`` 那一行是否是个下面没内容的表头？"""
    if node.line < 0 or node.line >= len(lines):
        return False
    for line in lines[node.line + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        return len(line) - len(line.lstrip(" ")) <= node.indent
    return True


def _needs_unfold(doc: "YamlDoc", parent: tuple) -> bool:
    """``parent`` 是否没有自己的块体？

    行内 ``{}``/``[]`` 和「下一行属于兄弟」的段都算。两种都要先补出块体，才能往
    「父节点末尾」插子项。
    """
    if parent and doc._lookup(tuple(parent)) is None:
        return False  # 完全不存在；祖先交给 ensure_block 建
    node = doc._lookup(tuple(parent))
    if node is None or isinstance(node, YScalar):
        return False
    if node.line >= 0 and node.line < len(doc.lines):
        body = doc.lines[node.line].rstrip("\r\n")
        if body.rstrip().endswith(("{}", "[]")):
            return True
    kids = list(node.children()) if isinstance(node, YMap) else []
    return not kids
