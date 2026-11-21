# -*- coding: utf-8 -*-
"""DeepSeek Harness（DSH）。

DSH 有两个互不相干的缝，我们只能动其中一个：

* ``$DSH_HOME/profiles/<profile>/cordis.patch.yml`` —— patch 层，加载器的静态配置。
  这是一个 YAML 数组，每个元素按 ``id`` 覆盖一条插件挂载；``llm-pi-ai`` 的
  ``config.providers`` 就是路由表，我们加一条叫 ``marvis-proxy`` 的路由，再把
  ``agent-default-model`` 指过去。
* ``$DSH_HOME/settings.yaml`` —— settings 层，用户在 UI 里改的东西，由
  ``dsh-settings-file`` 热发布。它在 patch 层之上，所以「用户选了哪个模型」只看这里。

两个事实决定写哪里、怎么写：

1. 真实机器上 ``settings.yaml`` 可能根本不存在，而路由表一定在 patch 文件里。所以
   配置写 patch 层，**并在有 settings.yaml 时同步改它的 ``agent-default-model``**。
2. DSH 读到非法 YAML 会直接启动失败。所以只做行级编辑，绝不重新序列化 —— patch 里
   常混着 GBK 乱码注释和上百行手调过的模型表。

优先级坑：DSH 先看继承来的进程环境，再看到配置文件，最后看 ``.env``。同名 shell 变量
会静默压过凭据库，所以 ``shadows_env_keys`` 要把这事报出来。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .atomic import newline_of, to_newline
from .base import Adapter, Entry
from .yamlutil import block_indent, cred_drop, cred_get, cred_set, inline_value
from .yamldoc import YMap, YScalar, YamlDoc


class ProfileError(RuntimeError):
    """判断不出该改哪个 profile —— 宁可报错，也不静默改错文件。"""

    code = "profile_ambiguous"


def _mtime(path):
    """文件修改时间，读不到就 0。用来在多个 profile 里挑「最近在用的那个」。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _when(stamp):
    """把 epoch 时间说成人话：``2026-11-21 04:16``。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
    except (OSError, ValueError):
        return "未知时间"

PROFILES = "profiles"
PATCH = "cordis.patch.yml"
SETTINGS = "settings.yaml"
CREDENTIALS = ".credentials.yaml"

#: patch 文件是插件列表，按 id 定位条目而不是按序号：用户插一个插件，序号就全错。
LLM = {"id": "llm-pi-ai"}
DEFAULT = {"id": "agent-default-model"}
LLM_ID = "llm-pi-ai"
DEFAULT_ID = "agent-default-model"
ROUTE = "marvis-proxy"
KEY_REF = "MARVIS_PROXY_API_KEY"

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
    def _dsh_home(self):
        """``$DSH_HOME``：真实机器上它是 ``~/.dsh``，测试里常直接给临时家目录。
        先看 ``<home>/.dsh``，没有再退回 ``<home>``。"""
        nested = self.home / ".dsh"
        return nested if nested.is_dir() else self.home

    def _profile(self):
        """要写的 profile 目录，以及**我们怎么选中它的**。

        一台机器上常有多个 profile（``desktop`` 和 ``web`` 是桌面版与 Web 端各一份），
        按字母序取第一个等于赌：赌错了改的是另一份配置，当前会话毫无变化，而且不会报错
        —— 用户只会觉得「明明 apply 成功了却没生效」。

        所以按「能确定的程度」依次取，最后一步宁可报错也不猜：
        ``--profile`` 显式指定 > ``DSH_PROFILE_DIR`` > ``DSH_PROFILE`` > 只有一个 profile。
        多个又判断不出来时抛 ``ProfileError``，把候选列给用户选。

        返回 ``(Path, 来源说明)``；来源说明会被打印出来，用户不用自己猜改的是哪份。
        """
        root = self._dsh_home() / PROFILES
        if not root.is_dir():
            return self._dsh_home(), "profiles/ 不存在，直接用 %s" % self._dsh_home()

        names = sorted(p.name for p in root.iterdir() if p.is_dir())
        if not names:
            return self._dsh_home(), "profiles/ 是空的，直接用 %s" % self._dsh_home()

        # 1) 显式指定（由 wire 的 --profile 传进来，存在 DSH_PROFILE_HINT 上）
        hint = getattr(self, "profile_hint", None)
        if hint:
            if hint not in names:
                raise ProfileError(
                    "profile %r 不存在；可选的：%s" % (hint, ", ".join(names)))
            return root / hint, "显式指定"

        # 2) DSH_PROFILE_DIR：这是 DSH 自己导出的绝对路径，最贴实际运行的那个
        env_dir = os.environ.get("DSH_PROFILE_DIR")
        if env_dir:
            cand = Path(env_dir).name if not isinstance(env_dir, Path) else env_dir.name
            if cand in names:
                return root / cand, "DSH_PROFILE_DIR=%s" % env_dir

        # 3) DSH_PROFILE
        env_name = os.environ.get("DSH_PROFILE")
        if env_name and env_name in names:
            return root / env_name, "DSH_PROFILE=%s" % env_name

        # 4) 只有一个：没什么可犹豫的
        if len(names) == 1:
            return root / names[0], "profiles/ 下只有这一个"

        # 5) 多个且没有环境变量：服务进程里常常就是这种情况（run.bat 起的不带
        #    DSH_PROFILE_DIR）。按 patch 文件的修改时间挑最近的 —— 正在被用的那份
        #    才会天天动，闲置的那份停在装上那天。这是启发式，所以来源说明里写清楚
        #    「按修改时间推断」，用户看见不对可以显式指定。
        ranked = sorted(
            ((root / n, _mtime(root / n / PATCH)) for n in names),
            key=lambda pair: pair[1] or 0, reverse=True)
        best, stamp = ranked[0]
        if stamp and len(ranked) > 1 and stamp > (ranked[1][1] or 0):
            return best, "多个 profile，按 patch 最近修改时间推断（%s 改过）" % _when(stamp)
        raise ProfileError(
            "机器上有 %d 个 DSH profile（%s），且都是老文件，无法判断该改哪一个。"
            "请显式指定：在 apply 请求里加 \"profile\": \"<name>\"，"
            "或设环境变量 DSH_PROFILE_DIR=%s。"
            % (len(names), ", ".join(names), root / names[0]))


    def _profile_path(self):
        """只要路径的调用点用这个；来源说明由 ``_profile`` 一并给出。"""
        return self._profile()[0]

    def path(self, rel):
        return self._dsh_home() / rel

    def all_paths(self):
        return [self._profile_path() / PATCH, self.path(CREDENTIALS)]

    def extra_warnings(self):
        out = []
        profile, source = self._profile()
        patch = profile / PATCH
        # 机器上有多个 profile 时，用户必须知道我们选了哪个、凭什么选的 —— 选错就会
        # 改到另一份配置而当前会话毫无变化，这种「成功但没生效」最难查。
        others = []
        root = self._dsh_home() / PROFILES
        if root.is_dir():
            others = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != profile.name)
        if others:
            out.append(
                "DSH profile：改的是 %s（依据：%s）；机器上还有 %s，那一份不会动。"
                "改错了就在 apply 请求里加 \"profile\": \"<name>\"。"
                % (profile.name, source, ", ".join(others))
            )
        if not patch.exists():
            out.append("%s 不存在，DSH 还没有任何 patch 层；我们会在里面建一个" % patch)
        if os.environ.get(KEY_REF):
            out.append(
                "%s 出现在继承环境里，它压过 %s；DSH 会用环境里那个值，而不是这里写的网关 key"
                % (KEY_REF, CREDENTIALS)
            )
        return out

    # ---- 钩子 -----------------------------------------------------------
    def plan(self, model, effort):
        patch, _source = self._profile()
        patch = patch / PATCH
        text, _, _ = self.load_text(patch)
        entries = []
        doc = YamlDoc.parse(text, newline_of(text) or "\n")

        route = (LLM, "config", "providers", ROUTE)
        present = doc.node(route) is not None
        entries.append(Entry(file=str(patch), path=list(route), present=present,
                             value=doc.text_of(route) if present else None,
                             kind="yaml-block"))
        for key in MODEL_KEYS:
            found = doc.scalar((DEFAULT, key))
            entries.append(Entry(file=str(patch), path=[DEFAULT_ID, key],
                                 present=found is not None, value=found, kind="yaml"))
        for section in ((LLM,), (LLM, "config"), (LLM, "config", "providers"), (DEFAULT,)):
            entries.append(Entry(
                file=str(patch), path=list(section),
                present=doc.node(section) is not None,
                value=inline_value(text, section) if section[-1] == "providers" else None,
                kind="yaml-section"))

        settings = self._dsh_home() / SETTINGS
        if settings.exists():
            s_text, _, _ = self.load_text(settings)
            s_doc = YamlDoc.parse(s_text, newline_of(s_text) or "\n")
            for key in MODEL_KEYS:
                found = s_doc.scalar((DEFAULT_ID, key))
                entries.append(Entry(file=str(settings), path=[DEFAULT_ID, key],
                                     present=found is not None, value=found,
                                     kind="yaml-settings"))
            entries.append(Entry(file=str(settings), path=[DEFAULT_ID],
                                 present=s_doc.node((DEFAULT_ID,)) is not None,
                                 kind="yaml-settings-section"))

        creds = self.path(CREDENTIALS)
        cred_text = self._read(creds)
        entries.append(Entry(file=str(creds), path=[KEY_REF],
                             present=cred_get(cred_text, KEY_REF) is not None,
                             value=cred_get(cred_text, KEY_REF), kind="flat"))
        return entries

    def render(self, path, text, newline, entries, model, effort):
        if path.name == CREDENTIALS:
            return cred_set(text, KEY_REF, self.gateway_key)

        facts = self.facts
        target = facts.require(model) if facts is not None else None
        model_id = target.id if target else model
        base = facts.openai_url if facts is not None else self.gateway_url + "/v1"
        display = facts.name if facts is not None else "Marvis Proxy"

        doc = YamlDoc.parse(text, newline)
        if path.name == SETTINGS:
            # settings 层只放用户可改的那一项，不重复整个路由表。
            doc.ensure_block((DEFAULT,))
            doc.put((DEFAULT, "provider"), ROUTE)
            doc.put((DEFAULT, "model"), model_id)
            if effort:
                doc.put((DEFAULT, "reasoningEffort"), effort)
            return to_newline(doc.render(), newline)

        # 路由表在 llm-pi-ai.config.providers；ensure_block 会把行内 `providers: {}`
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
            return cred_drop(text, KEY_REF)

        doc = YamlDoc.parse(text, newline)
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
                    # 用户本来就有同名路由：删掉我们的，把他们的字节原样放回。
                    if doc.node(key) is not None:
                        doc.delete_block(key)
                    doc.graft((LLM, "config", "providers"), entry.value or [],
                              block_indent(entry.value))
                else:
                    doc.delete_block(key)
                continue
            if entry.kind != "yaml" or not entry.present:
                continue
            doc.set(key, str(entry.value))

        if doc.node((DEFAULT,)) is not None:
            for key in MODEL_KEYS:
                marked = next((e for e in own
                               if list(e.path) == [DEFAULT_ID, key]), None)
                if marked is not None and not marked.present:
                    doc.delete((DEFAULT, key))
            section = next((e for e in own if e.kind == "yaml-section"
                            and list(e.path) == [DEFAULT_ID]), None)
            if section is not None and not section.present:
                doc.delete_block((DEFAULT,))

        # 真实文件里通常已有 config:/providers:，但空文件上是我们建的，回滚要把这几层
        # 空壳一起拿掉。
        providers = next((e for e in own if e.kind in ("yaml-section",)
                          and list(e.path) == [LLM_ID, "config", "providers"]), None)
        if providers is not None and providers.present and providers.value == "{}":
            # 原来是 `providers: {}`，被 ensure_block 展开过，这里折回去。
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

def _entry_block(model_id):
    """空 patch 文件里要补的那一整条 ``- id: llm-pi-ai`` 插件条目。"""
    return "\n".join([
        "- id: %s" % LLM_ID,
        "  config:",
        "    providers:",
    ])


def _route_block(model, effort, base, display, target):
    """``marvis-proxy:`` 路由，缩进相对 ``llm-pi-ai.config.providers``。"""
    context = (target.context_window if target else 0) or FALLBACK_CONTEXT
    lines = [
        "%s:" % ROUTE,
        "  displayName: %s" % display,
        "  apiKeyEnv: %s" % KEY_REF,
        "  api: openai-completions",
        "  baseURL: %s" % base,
        "  compat:",
        "    thinkingFormat: openai",
        "    supportsReasoningEffort: true",
        "  models:",
        "    - id: %s" % model,
        "      name: %s" % (target.display if target else model),
        "      contextWindow: %d" % context,
        # maxTokens 故意不写：pi-ai 只在模型条目显式给了上限时才限流，继承默认值
        # 会悄悄截断长回答。
        "      reasoningEfforts:",
        '        off: ""',
        "        minimal: minimal",
        "        low: low",
        "        medium: medium",
        "        high: high",
        "        xhigh: xhigh",
        "        max: max",
    ]
    return "\n".join(lines) + "\n"
