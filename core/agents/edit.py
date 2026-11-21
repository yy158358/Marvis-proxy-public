# -*- coding: utf-8 -*-
"""保格式文本编辑。两种策略，故意不同：

  * JSON —— 解析、改、按文件自己的缩进与换行写回。dict 保插入序且不排序，无关的键
    永远不会被挪位置。
  * TOML —— **不重新序列化**。定位我们负责的键的字节区间就地拼接，注释、空行、表序、
    以及解析器不认识的语法都原样留着。

这里全部是纯文本 -> 文本，可以脱离磁盘做单测。
"""
from __future__ import annotations

import json
import re
from typing import Any

# --------------------------------------------------------------------------- JSON


def detect_json_style(text: str) -> dict[str, Any]:
    """探测缩进、换行、结尾换行，让重写后的文件看起来还是用户的文件。

    除了「整份文件用几个空格」，还要记**哪些值原本写在一行上**：真人的 settings.json
    常常是外层多行、``"env": {"FOO": "bar"}`` 这种小对象挤在一行。只记缩进的话，重排
    会把它们全展开，回滚就还原不回原字节，用户拿到一份没请我们改的 diff。
    """
    m = re.search(r"\r?\n([ \t]+)\S", text)
    indent: int | str = 2
    if m:
        raw = m.group(1)
        indent = "\t" if raw.startswith("\t") else len(raw)
    compact = not m and "\n" not in text.strip()
    # 分隔符也是用户风格的一部分：`{"a":1}` 与 `{"a": 1}` 是同一份文档但不是同一个
    # 文件，回滚把它们互换就是错的。
    spaced = bool(re.search(r'":\s+[^\s]', text)) or bool(re.search(r",\s", text))
    empty = not text.strip()
    return {
        "indent": None if compact else indent,
        "newline_at_end": text.endswith("\n") or not text,
        "newline": "\r\n" if "\r\n" in text else "\n",
        "separator": (", ", ": ") if spaced else (",", ":"),
        #: 原本写在一行上的容器值所在路径，形如 ``[("env",), ("permissions", "allow")]``。
        #: 只有缩进输出才有意义 —— 全内联的文件本来就整体一行。
        "inline": [] if compact or empty else inline_paths(text),
        #: 文件本来的内容就是空的（零字节或纯空白）：写回时也要原样还原。
        "empty": empty,
        "empty_text": text if empty else "",
    }



def json_load(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    return json.loads(text)


def json_dump(obj: dict, style: dict[str, Any]) -> str:
    # 空文件保持空：`json_load("")` 给的是 `{}`，但把它写成 `{}\n` 就是凭空造内容，
    # 回滚时也还原不回零字节。
    if style.get("empty"):
        # 纯空白文件保留它原有的空白：零字节回零字节，一个换行回一个换行。
        return style.get("empty_text", "")
    indent = style.get("indent")
    separators = style.get("separator")
    if not (isinstance(separators, (list, tuple)) and len(separators) == 2):
        separators = None
    if indent is not None:
        # 带缩进时 json 会把项分隔符接在逗号后，传 ", " 会让每个非末键变成
        # `"a": 1, ` 留下尾随空格。缩进输出恒用 ","，只有紧凑输出才需要区分。
        separators = (",", ": ") if separators is None or separators[1] == ": " else (",", ":")
    out = json.dumps(obj, indent=indent, ensure_ascii=False, separators=separators)
    if indent is not None:
        # 把原本写在一行上的容器值折回去。缩进输出才有可折的东西。
        for path in _sorted_inline(style.get("inline") or ()):
            out = _collapse_value(out, tuple(path))
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





# ---- 行内值：哪些容器原本写在一行 ------------------------------------------
#
# 「外层多行、小对象挤一行」是真人手写 JSON 最常见的形态，也是最容易被 json.dumps
# 抹掉的东西。这里只做一件事：找出这些容器的路径，写回时把它们折回一行。
#
# 判断依据是**源文本里有没有换行**，所以得先知道每个值在原文里占哪一段。不用正则
# 扫文本，因为两处行内值可能字面完全一样（`{"a": 1}` 出现不止一次），文本匹配分不
# 清哪个属于哪条路径 —— 这里走一遍解析树，每个容器记住自己的源片段。

_WS = " \t\r\n"


def inline_paths(text: str) -> list[list]:
    """原本写在一行上的容器值所在路径（可 JSON 序列化，好存进快照）。

    路径元素是键名或数组下标，例如 ``[["env"], ["permissions", "allow"]]``。整份
    文档本身就是一行时返回空表 —— 那种情况由 ``indent=None`` 表达，不需要逐值折。
    """
    if not text.strip() or "\n" not in text:
        return []
    try:
        parsed = _parse_tree(text.strip())
    except Exception:
        return []  # 解析不了就别猜，宁可按缩进重排
    root = parsed[0]

    paths: list[list] = []

    def walk(node, path):
        # 只有容器带 src；标量是裸值，没有可折的东西。
        if not isinstance(node, _Inline):
            return
        data = node.data
        if isinstance(data, dict):
            children = list(data.items())
        elif isinstance(data, list):
            children = list(enumerate(data))
        else:
            return
        for key, value in children:
            walk(value, path + [key])
        # 根自己不用记：整份文件的换行由 indent 决定。
        if path and "\n" not in node.src:
            paths.append(path)

    walk(root, [])
    return paths


class _Inline(object):
    """解析出的值 + 它在源文本里的那一段。"""

    __slots__ = ("src", "data")

    def __init__(self, src, data):
        self.src = src
        self.data = data


def _parse_tree(body: str):
    """带源片段的极简 JSON 解析，返回 ``(_Inline, end_pos)``。

    只服务 ``inline_paths``：值本身用 ``json.loads`` 兜底，容器的边界由括号配对得出。
    """
    def skip(pos):
        while pos < len(body) and body[pos] in _WS + ",":
            pos += 1
        return pos

    def parse(pos):
        pos = skip(pos)
        if pos >= len(body):
            raise ValueError("unexpected end of input")
        start = pos
        if body[pos] == "{":
            out = {}
            pos += 1
            while True:
                pos = skip(pos)
                if pos >= len(body):
                    raise ValueError("unterminated object")
                if body[pos] == "}":
                    pos += 1
                    break
                key_node, pos = parse(pos)
                key = key_node.data if isinstance(key_node, _Inline) else key_node
                while pos < len(body) and body[pos] in _WS + ":":
                    pos += 1
                value, pos = parse(pos)
                out[key] = value
            return _Inline(body[start:pos], out), pos
        if body[pos] == "[":
            items = []
            pos += 1
            while True:
                pos = skip(pos)
                if pos >= len(body):
                    raise ValueError("unterminated array")
                if body[pos] == "]":
                    pos += 1
                    break
                value, pos = parse(pos)
                items.append(value)
            return _Inline(body[start:pos], items), pos
        if body[pos] == '"':
            pos += 1
            while pos < len(body) and body[pos] != '"':
                pos += 2 if body[pos] == "\\" else 1
            pos = min(pos + 1, len(body))
            return json.loads(body[start:pos]), pos
        end = pos
        while end < len(body) and body[end] not in ",]}\r\n \t":
            end += 1
        if end == pos:
            raise ValueError("empty token at %d" % pos)
        return json.loads(body[pos:end]), end

    return parse(0)


def _sorted_inline(paths) -> list[list]:
    """深的先折：先折内层再折外层，外层折叠时内层已经是一行了。"""
    cleaned = [list(p) for p in paths if p]
    return sorted(cleaned, key=len, reverse=True)


def _collapse_value(text: str, path: tuple) -> str:
    """把已排好版的内容里某个容器值折回一行。

    折叠是**纯排版**操作：只动空白，不碰任何值。括号层级对不上就原样返回，绝不硬折
    —— 折错了比不折更糟。
    """
    if not path:
        return text
    lines = text.split("\n")
    target = _find_container_line(lines, path)
    if target is None:
        return text
    depth = 0
    end = None
    for j in range(target, len(lines)):
        depth += lines[j].count("[") + lines[j].count("{")
        depth -= lines[j].count("]") + lines[j].count("}")
        if depth <= 0:
            end = j
            break
    if end is None or end <= target:
        return text
    head = lines[target]
    lead = head[:len(head) - len(head.lstrip())]
    # 把多行拼成一行：逐行去首尾空白后直接接上，行尾的逗号本来就还在，不用补。
    parts = [lines[target].strip()]
    for part in lines[target + 1:end + 1]:
        part = part.strip()
        if parts[-1].endswith((",", "{", "[")):
            parts.append(part)
        elif part.startswith((",", "}", "]")):
            parts.append(part)
        else:
            parts.append(" " + part)
    body = " ".join(parts)
    body = re.sub(r"\s+", " ", body)
    body = re.sub(r"\{\s+", "{", body)
    body = re.sub(r"\[\s+", "[", body)
    body = re.sub(r"\s+\]", "]", body)
    body = re.sub(r"\s+\}", "}", body)
    body = re.sub(r"\s*,\s*,", ",", body)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    folded = (lead + body).rstrip()
    nxt = lines[end + 1] if end + 1 < len(lines) else ""
    if not folded.endswith(",") and nxt.strip() and not nxt.strip().startswith(("}", "]")):
        folded += ","
    lines[target:end + 1] = [folded]
    return "\n".join(lines)


def _find_container_line(lines: list[str], path: tuple):
    """找到 ``path`` 指向的容器值在第几行开始（形如 ``"key": {``）。

    按缩进层级定位：路径深度 d 的键，其行首缩进正好是 d 个缩进单位。这样同名但不同
    层的键不会互相命中，也不需要维护括号栈。
    """
    key = path[-1]
    if not isinstance(key, str):
        return None
    # 根对象的子键缩进 1 个单位，孙键 2 个……所以深度就是路径长度。
    want = len(path)
    needle = json.dumps(key, ensure_ascii=False) + ":"
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(needle):
            continue
        tail = stripped[len(needle):].strip()
        if tail[:1] not in ("{", "["):
            continue
        if _indent_depth(line) == want:
            return i
    return None


def _indent_depth(line: str) -> int:
    """行首缩进相当于几个缩进单位（tab 算一个）。"""
    lead = line[:len(line) - len(line.lstrip())]
    if not lead:
        return 0
    if lead.startswith("\t"):
        return len(lead)
    return len(lead) // 2 if len(lead) % 2 == 0 else len(lead)



# --------------------------------------------------------------------------- TOML
#
# 最小的区间保留式 TOML 助手，不追求完整解析：只认 (a) 顶层 `key = value` 行和
# (b) `[table]` 段，config.toml 里我们要碰的键就这两类。
#
# 唯一的不变式：没打算改的地方一个字都不改。值没变的键是空操作；重写表只替换表自己
# 的行，不吞也不凭空造空行 —— 空行属于用户，不属于我们。

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
    """按文件顺序列出表名（``[a.b]`` -> ``"a.b"``）。"""
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            names.append(stripped.strip("[]").strip())
    return names


def toml_table_neighbours(text: str, table: str) -> tuple[str | None, str | None]:
    """``table`` 前后的邻居表；表不存在时返回 (None, None)。"""
    names = toml_tables(text)
    if table not in names:
        return None, None
    i = names.index(table)
    return (names[i - 1] if i > 0 else None), (names[i + 1] if i + 1 < len(names) else None)


def toml_find_key(text: str, key: str, table: str | None) -> tuple[int, int, str] | None:
    """返回 ``table`` 里 ``key`` 所在行的 (start, end, 值文本)。

    ``table=None`` 表示顶层（第一个 [header] 之前）。区间含行尾换行，替换成空串就能
    干净地删掉这一行。
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
    """``table`` 里 ``key`` 的**语义值**：引号、注释都剥掉。

    ``toml_find_key`` 给的是原始文本（``"gpt-5"`` 连引号一起），那是给
    ``toml_set_key`` 做字节替换用的。读值必须解一层引号，否则 ``model = "x"``
    读出来是 ``"x"``，回滚时会被写成 ``model = "\"x\""``。
    """
    hit = toml_find_key(text, key, table)
    return None if hit is None else _toml_unquote(hit[2])


def _toml_unquote(raw: str) -> str:
    """把一行里 ``=`` 右边的值解成裸值：去注释、去引号。

    只处理本服务会写的那些形态（字符串、裸值），TOML 的复杂类型原样返回 ——
    我们从不读它们，硬解反而会改坏。
    """
    val = _toml_strip_comment(raw).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        inner = val[1:-1]
        if val[0] == '"':
            # 基本字符串：还原 TOML 的转义。字面量字符串（单引号）不处理转义。
            inner = (inner.replace('\\\\', '\\').replace('\\"', '"')
                          .replace("\\n", "\n").replace("\\t", "\t"))
        return inner
    return val


def _toml_strip_comment(raw: str) -> str:
    """去掉行尾注释，但引号里的 ``#`` 不算。"""
    quote = None
    for i, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return raw[:i]
    return raw


def toml_has_table(text: str, table: str) -> bool:
    return bool(_table_header(table).search(text))


def toml_set_key(text: str, key: str, table: str | None, value: str) -> str:
    """就地写 ``key = value``，没有就追加到对应段。

    值本来就一样时原样返回，重复 apply 不会把注释挤到别的行。
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
        # 顶层：接在最后一个顶层键之后；还没有键时放在第一张表之前，绝不贴到表里。
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
            return _final_newline(head + new_line + tail, eol)
        head_end = 0
        for line in lines:
            if re.match(r"^[ \t]*\[", line):
                break
            head_end += len(line)
        head, tail = text[:head_end], text[head_end:]
        if not head:
            return new_line + (eol + tail if tail else "")
        # 还没有顶层键：新键放在第一张表上方，并保留文件原有的空行间隔，之后
        # delete_key 才能还原成原字节。
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
    # 追加为表的最后一行（贴着它最后一个非空行）。
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
    if table is not None:
        return text[:start] + text[end:]
    # 我们追加的顶层键可能就在第一张表上方，中间隔着一个不得不加的空行。删键时要把它
    # 一起删掉，否则回滚会多出一个换行。
    before, rest = text[:start], text[end:]
    eol = _eol(text)
    # 键被加在第一张表上方，原先隔开注释与表的那一行空行现在落在我们下面。连同键
    # 一起吃掉，否则回滚会多出一个换行。
    if rest.startswith(eol + "[") and not rest.startswith(eol + eol):
        return before + rest[len(eol):]
    if rest.startswith(eol + "[") and before.endswith(eol + eol):
        return before[:-len(eol)] + rest
    return before + rest

def toml_table_span(text: str, table: str) -> tuple[int, int] | None:
    """``[table]`` 表头加表体的 (start, end)，不含尾部空行。"""
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
    """补上文件原本就有的结尾换行。

    往没有结尾换行的文件里加键不能留下这个换行，否则回滚会差一个字节。
    """
    return text if text.endswith(eol) else text + eol


def toml_set_table(text: str, table: str, entries: dict[str, str]) -> str:
    """用给定的 key -> 原始值文本 创建或整段重写 ``[table]``。"""
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
    """把 ``[table]`` 插到 ``before`` 这张表之前（找不到就追加）。

    回滚时把表放回原位而不是追加到末尾 —— 这是一行 diff 和「整段搬家」的差别。
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
    # 连带吃掉一行分隔空行，避免删完留下双空行；有得选时优先吃表前那一行。
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
