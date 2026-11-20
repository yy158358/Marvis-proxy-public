# -*- coding: utf-8 -*-
"""DeepSeek Harness（DSH）。

DSH 有两个互不相干的缝，我们只能动其中一个：

* ``$DSH_HOME/profiles/<profile>/cordis.patch.yml`` —— **patch 层**，加载器的静态配置。
  这是一个 YAML 数组，每个元素按 ``id`` 覆盖一条插件挂载；``llm-pi-ai`` 的
  ``config.providers`` 就是模型路由表，我们加一条叫 ``marvis-proxy`` 的路由，
  再把 ``agent-default-model`` 指过去。
* ``$DSH_HOME/settings.yaml`` —— **settings 层**，用户在 UI 里改的东西（由
  ``dsh-settings-file`` 热发布）。它在 patch 层之上，所以「用户选了哪个模型」只能
  看这里，它压过 patch 里的 ``agent-default-model.config``。

两个事实决定了这个适配器写哪里、怎么写：

1. 真实机器上 ``settings.yaml`` 可能根本不存在（用户没动过 UI 设置），而路由表一定
   在 patch 文件里。所以配置写 patch 层，**并在有 settings.yaml 时同步改它的
   ``agent-default-model``**（那才是真正生效的那一份）。
2. DSH 读到非法的 YAML 会在启动时直接失败。所以这里只做行级编辑（``yamldoc``），
   绝不重新序列化——patch 文件里常混着 GBK 乱码注释和上百行用户手工调过的模型表，
   重排一次就可能把别人的字节改掉。

优先级坑：DSH 先看**继承来的进程环境**，再看到这个文件，最后看 ``.env``。同名的
shell 变量会静默压过凭据库，所以 ``shadows_env_keys`` 会把这件事报出来。
"""
from __future__ import annotations

import os

from .atomic import newline_of, to_newline
from .base import Adapter, Entry
from .yamldoc import YMap, YScalar, YamlDoc

PROFILES = "profiles"
PATCH = "cordis.patch.yml"
SETTINGS = "settings.yaml"
CREDENTIALS = ".credentials.yaml"

LLM = "llm-pi-ai"
ROUTE = "marvis-proxy"
KEY_REF = "MARVIS_PROXY_API_KEY"
DEFAULT = "agent-default-model"

MODEL_KEYS = ("provider", "model", "reasoningEffort")
DEFAULT_EFFORT = "high"
#: pi-ai 的 fallback：只在拿不到网关事实时用。
FALLBACK_CONTEXT = 200000


class DshAdapter(Adapter):

    agent_id = "dsh"
    display_name = "DeepSeek Harness"
    files = (PATCH, CREDENTIALS)
    needs_restart = False
    shadows_env_keys = (KEY_REF,)

    # ---- 环境 -----------------------------------------------------------
    def _profile(self):
        """本次要写的 profile 目录。

        ``MAGPIE_DSH_PROFILE`` 之类的显式指定优先；否则取 ``profiles/`` 下第一个
        （真实机器上通常只有 ``desktop``）。找不到就退回 home 本身。
        """
        root = self.path(PROFILES)
        if not root.is_dir():
            return self.home
        names = sorted(p.name for p in root.iterdir() if p.is_dir())
        if not names:
            return self.home
        return root / names[0]

    def path(self, rel):
        return super().path(rel)

    def all_paths(self):
        return [self._profile() / PATCH, self.path(CREDENTIALS)]

    def extra_warnings(self):
        out = []
        patch = self._profile() / PATCH
        if not patch.exists():
            out.append("%s 不存在，DSH 还没有任何 patch 层；我们会在里面建一个" % patch)
        if os.environ.get(KEY_REF):
            out.append(
                "%s 出现在继承环境里，它压过 %s；DSH 会用环境里那个值，而不是这里写的网关 key"
                % (KEY_REF, CREDENTIALS)
            )
        return out

    # ---- hooks -----------------------------------------------------------
    def plan(self, model, effort):
        patch = self._profile() / PATCH
        text, _, _ = self.load_text(patch)
        entries = []
        doc = YamlDoc.load(text, newline_of(text) or "\n")

        route = (LLM, "config", "providers", ROUTE)
        present = doc.node(route) is not None
        entries.append(Entry(file=str(patch), path=list(route), present=present,
                             value=doc.text_of(route) if present else None,
                             kind="yaml-block"))
        for key in MODEL_KEYS:
            found = doc.scalar((DEFAULT, key))
            entries.append(Entry(file=str(patch), path=[DEFAULT, key],
                                 present=found is not None, value=found, kind="yaml"))
        for section in ((LLM,), (LLM, "config"), (LLM, "config", "providers"), (DEFAULT,)):
            entries.append(Entry(
                file=str(patch), path=list(section),
                present=doc.node(section) is not None,
                value=_inline_value(text, section) if section[-1] == "providers" else None,
                kind="yaml-section"))

        settings = self.path(SETTINGS)
        if settings.exists():
            s_text, _, _ = self.load_text(settings)
            s_doc = YamlDoc.load(s_text, newline_of(s_text) or "\n")
            for key in MODEL_KEYS:
                found = s_doc.scalar((DEFAULT, key))
                entries.append(Entry(file=str(settings), path=[DEFAULT, key],
                                     present=found is not None, value=found,
                                     kind="yaml-settings"))
            entries.append(Entry(file=str(settings), path=[DEFAULT],
                                 present=s_doc.node((DEFAULT,)) is not None,
                                 kind="yaml-settings-section"))

        creds = self.path(CREDENTIALS)
        cred_text = self._read(creds)
        entries.append(Entry(file=str(creds), path=[KEY_REF],
                             present=_cred_get(cred_text, KEY_REF) is not None,
                             value=_cred_get(cred_text, KEY_REF), kind="flat"))
        return entries

    def render(self, path, text, newline, entries, model, effort):
        if path.name == CREDENTIALS:
            return _cred_set(text, KEY_REF, self.gateway_key)

        facts = self.facts
        target = facts.require(model) if facts is not None else None
        model_id = target.id if target else model
        base = facts.openai_url if facts is not None else self.gateway_url + "/v1"
        display = facts.name if facts is not None else "Marvis Proxy"

        doc = YamlDoc.load(text, newline)
        if path.name == SETTINGS:
            # settings 层只放用户可改的那一项，不重复整个路由表。
            doc.ensure_block((DEFAULT,))
            doc.put((DEFAULT, "provider"), ROUTE)
            doc.put((DEFAULT, "model"), model_id)
            if effort:
                doc.put((DEFAULT, "reasoningEffort"), effort)
            return to_newline(doc.render(), newline)

        # 加载器把每个插件的配置嵌在自己的 config: 下，所以路由表在
        # llm-pi-ai.config.providers。ensure_block 还会把行内的 `providers: {}`
        # 展开成真正的段，否则追加的子行会挂在这一行后面。
        doc.ensure_block((LLM, "config", "providers"))
        doc.insert((LLM, "config", "providers"),
                   _route_block(model_id, effort or DEFAULT_EFFORT, base, display, target))
        doc.ensure_block((DEFAULT,))
        doc.put((DEFAULT, "provider"), ROUTE)
        doc.put((DEFAULT, "model"), model_id)
        if effort:
            doc.put((DEFAULT, "reasoningEffort"), effort)
        return to_newline(doc.render(), newline)

    def unrender(self, path, text, newline, entries):
        if path.name == CREDENTIALS:
            return _cred_drop(text, KEY_REF)

        doc = YamlDoc.load(text, newline)
        own = [e for e in entries if e.file == str(path)]
        if path.name == SETTINGS:
            for entry in own:
                if entry.kind == "yaml-settings-section" and not entry.present:
                    doc.delete_block((DEFAULT,))
                    continue
                if entry.kind != "yaml-settings" or not entry.present:
                    continue
                doc.set(tuple(entry.path), str(entry.value))
            return to_newline(doc.render(), newline)

        for entry in own:
            key = tuple(entry.path)
            if entry.kind == "yaml-block":
                if entry.present:
                    # 用户本来就有同名路由：把他们的字节原样放回那个位置，删掉我们的。
                    if doc.node(key) is not None:
                        doc.delete_block(key)
                    doc.graft((LLM, "config", "providers"), entry.value or [],
                              _indent_of(entry.value))
                else:
                    doc.delete_block(key)
                continue
            if entry.kind != "yaml" or not entry.present:
                continue
            doc.set(key, str(entry.value))

        if doc.node((DEFAULT,)) is not None:
            for key in MODEL_KEYS:
                marked = next((e for e in own
                               if list(e.path) == [DEFAULT, key]), None)
                if marked is not None and not marked.present:
                    doc.delete((DEFAULT, key))
            section = next((e for e in own if e.kind == "yaml-section"
                            and list(e.path) == [DEFAULT]), None)
            if section is not None and not section.present:
                doc.delete_block((DEFAULT,))

        # 再往上一层同理：真实文件里通常已有 config:/providers:，但空文件上是我们
        # 建的，回滚要把这几层空壳一起拿掉。
        providers = next((e for e in own if e.kind in ("yaml-section",)
                          and list(e.path) == [LLM, "config", "providers"]), None)
        if providers is not None and providers.present and providers.value == "{}":
            # 原来写的是 `providers: {}`，ensure_block 展开过，这里折回去。
            node = doc.node((LLM, "config", "providers"))
            if isinstance(node, YScalar) and node.empty:
                line = doc.lines[node.line].rstrip("\r\n")
                doc.lines[node.line] = line.rstrip() + " {}" + newline
                doc._resync()
        for section in ((LLM, "config", "providers"), (LLM, "config"), (LLM,)):
            marker = next((e for e in own if e.kind == "yaml-section"
                           and list(e.path) == list(section)), None)
            if marker is None or marker.present:
                continue
            node = doc.node(section)
            if node is None:
                continue
            if isinstance(node, YMap) and node.items:
                continue
            doc.delete_block(section)
            if section == (LLM,):
                break
        return to_newline(doc.render(), newline)

    @staticmethod
    def _read(path):
        try:
            return path.read_bytes().decode("utf-8", "surrogateescape")
        except OSError:
            return ""
