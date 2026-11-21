# -*- coding: utf-8 -*-
"""网关事实：wire 只认这一份，不认命令行参数、不认硬编码。

正在跑的反代比任何静态文档都清楚三件事：自己监听的地址、自己有哪些模型及各自
的上下文与输出上限、Anthropic 档位该落到哪个槽位。把它们当输入而非常量，用户改
了 models.json 里的 context_window，下次 apply 写进 Claude Code 的数字就自动跟上。

``provider_block`` 由 serve.py 传入（它要 ctx 里的账号池），所以这里不 import serve。
"""
from __future__ import annotations

ANTHROPIC = "anthropic"
OPENAI = "openai-chat-completions"


class Model(object):
    """``models.json`` 里一条 alias 的只读视图。"""

    __slots__ = ("id", "display", "context_window", "max_output_tokens", "caps", "alts", "slot")

    def __init__(self, raw):
        self.id = str(raw.get("name") or "")
        self.display = str(raw.get("display") or self.id)
        self.context_window = int(raw.get("context_window") or 0)
        self.max_output_tokens = int(raw.get("max_output_tokens") or 0)
        self.caps = dict(raw.get("caps") or {})
        self.alts = [str(a) for a in (raw.get("alts") or [])]
        self.slot = str(raw.get("slot") or "")

    def names(self):
        """可直接当 ``model`` 传的名字，稳定 id 在前。"""
        return [self.id] + [a for a in self.alts if a != self.id]

    def __repr__(self):
        return "<Model %s>" % self.id


class Facts(object):
    """一次 apply 用的全部网关事实。构造时快照，期间不变。"""

    def __init__(self, models, provider=None):
        self.provider = dict(provider or {})
        self._raw = models or {}
        self.models = [Model(a) for a in (self._raw.get("aliases") or []) if a.get("name")]
        self.claude_aliases = list(self._raw.get("claude_aliases") or [])
        self._by_slot = {}
        for m in self.models:
            self._by_slot.setdefault(m.slot, m)

    # ---- 地址 ---------------------------------------------------------
    @property
    def base(self):
        """不带 /v1 的根地址。"""
        url = str(self.provider.get("base_url") or "").rstrip("/")
        return url[:-3] if url.endswith("/v1") else url

    @property
    def openai_url(self):
        """OpenAI 形状：``http://127.0.0.1:8088/v1``。"""
        return self.base + "/v1"

    @property
    def anthropic_url(self):
        """Anthropic 形状：不带 /v1，Claude Code 自己会拼 /v1/messages。"""
        return self.base

    @property
    def name(self):
        return str(self.provider.get("name") or self.provider.get("display_name") or "Marvis Proxy")

    @property
    def provider_id(self):
        return str(self.provider.get("id") or "marvis-proxy")

    @property
    def key_env(self):
        env = ((self.provider.get("auth") or {}).get("env") or "")
        return env or "MARVIS_PROXY_API_KEY"

    @property
    def features(self):
        return dict(self.provider.get("features") or {})

    # ---- 模型 ---------------------------------------------------------
    def find(self, name):
        """按 id 或别名找模型，大小写不敏感。"""
        needle = str(name or "").strip().lower()
        if not needle:
            return None
        for m in self.models:
            if m.id.lower() == needle:
                return m
        for m in self.models:
            if needle in (a.lower() for a in m.alts):
                return m
        return None

    def require(self, name):
        model = self.find(name)
        if model is None:
            known = ", ".join(m.id for m in self.models) or "(空)"
            raise LookupError("未知模型 %r；可选：%s" % (name, known))
        return model

    def tier_slot(self, tier):
        """档位 -> 槽位名。

        用 ``claude_aliases`` 里真实的 Claude 型号前缀推（那是上游已承认的映射，
        Claude Code 请求体的 model 字段就落在这张表上）；查不到返回 ``None``，
        调用方保留原值并告警 —— 绝不猜。
        """
        probes = {
            "opus": ("claude-opus",),
            "sonnet": ("claude-sonnet", "claude-3-5-sonnet", "claude-3-7-sonnet"),
            "haiku": ("claude-haiku", "claude-3-5-haiku", "claude-3-haiku"),
            "fable": ("claude-fable",),
        }.get(tier, ())
        for probe in probes:
            for row in self.claude_aliases:
                if str(row.get("name") or "").startswith(probe):
                    return str(row.get("slot") or "") or None
        return None

    def tier_models(self):
        """档位 -> (真实模型 id, 警告)，推不出的档位不出现在结果里。"""
        out = {}
        warnings = []
        for tier in ("opus", "sonnet", "haiku"):
            slot = self.tier_slot(tier)
            if not slot:
                warnings.append("claude_aliases 里没有 %s 档，保留原值不写" % tier)
                continue
            model = self._by_slot.get(slot)
            if model is None:
                warnings.append("%s 档的槽位 %s 在 models.json 里没有对应模型，保留原值" % (tier, slot))
                continue
            out[tier] = model.id
        return out, warnings

    def describe(self):
        return [
            {"id": m.id, "display": m.display, "context_window": m.context_window,
             "max_output_tokens": m.max_output_tokens, "slot": m.slot, "alts": m.alts,
             "caps": m.caps}
            for m in self.models
        ]
