# -*- coding: utf-8 -*-
"""wire 自测：临时家目录下，四条接法的 apply -> rollback 必须逐字节还原。

跑法：

    python tools\\wire_selftest.py

它不碰真实家目录：全程在 ``paths.redirect()`` 里跑，快照落在临时 sqlite。
每个场景都是「造一个像真的配置文件 → 记下原始字节 → apply → 检查写入内容 →
rollback → 逐字节比对」。差一个字节就算失败，这是这套东西唯一有意义的验收标准。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import core.agents as A  # noqa: E402
from core.agents import paths  # noqa: E402
from core.agents.facts import Facts  # noqa: E402
from core.agents.snapshot import SnapshotStore  # noqa: E402
from core.store import Store  # noqa: E402

OK = []
FAILED = []


def check(name, ok, detail=""):
    (OK if ok else FAILED).append((name, detail))
    print("%-4s %s%s" % ("ok" if ok else "FAIL", name, ("  " + detail) if detail and not ok else ""))


def _models():
    with open(os.path.join(HERE, "models.json"), "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def facts():
    provider = {"id": "marvis-proxy", "name": "Marvis Proxy",
                "base_url": "http://127.0.0.1:8088/v1",
                "auth": {"env": "MARVIS_PROXY_API_KEY"}}
    return Facts(_models(), provider)


def settings_for(tmp):
    return SnapshotStore(Store(os.path.join(tmp, "wire.db")), os.path.join(tmp, "wire-backups"))


CASES = {}


def case(name):
    def wrap(fn):
        CASES[name] = fn
        return fn
    return wrap


def plant(home, rel, text):
    path = os.path.join(home, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))


@case("claude-code")
def _(home):
    plant(home, ".claude/settings.json", json.dumps({
        "model": "claude-sonnet-4-5",
        "env": {"ANTHROPIC_MODEL": "claude-sonnet-4-5", "FOO": "bar"},
        "permissions": {"allow": ["Bash"]},
    }, indent=2) + "\n")


@case("codex")
def _(home):
    plant(home, ".codex/config.toml",
          'model = "gpt-5"\n\n# 我的注释\n[profiles.slow]\nmodel = "o3"\n')


@case("dsh")
def _(home):
    plant(home, ".dsh/profiles/desktop/cordis.patch.yml",
          "# patch 层\n- id: ui-chat\n  config:\n    a: 1\n- id: llm-pi-ai\n"
          "  name: '@deepseek-ai/dsh-llm-pi-ai'\n  config:\n    providers:\n"
          "      wb-proxy:\n        displayName: WB Proxy\n        baseURL: http://127.0.0.1:8788/v1\n"
          "        models:\n          - id: hy3\n            contextWindow: 960000\n"
          "- id: agent-default-model\n  config:\n    provider: wb-proxy\n    model: hy3\n")
    plant(home, ".dsh/.credentials.yaml",
          "version: 1\nrecords:\n  a:\n    kind: grant\nrefs:\n  WB_PROXY_API_KEY: abc\n")


@case("zcode")
def _(home):
    plant(home, ".zcode/v2/provider_config.json", json.dumps({
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {"providerRules": [
                {"providerId": "wb-proxy",
                 "config": {"api": {"baseUrl": "http://127.0.0.1:8788/v1"}}}]},
            "providerOrder": ["wb-proxy"],
        },
    }, indent=2, ensure_ascii=False) + "\n")


def snapshot_bytes(home):
    out = {}
    for root, _dirs, files in os.walk(home):
        for name in files:
            full = os.path.join(root, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, home).replace("\\", "/")] = fh.read()
    return out


def _scratch(tag):
    """沙箱里 %TEMP% 常常可建不可写（ACL 继承），工作区自己的 tmp/ 才可靠。

    用自增目录名而不是 ``mkdtemp``：本机沙箱下 mkdtemp 建的目录带着一条 DENY
    ACE，建完就写不进去。目录名带 pid，同一进程重跑不会撞。
    """
    root = os.path.join(os.path.dirname(HERE), "tmp", "wire-selftest")
    os.makedirs(root, exist_ok=True)
    index = 0
    while True:
        path = os.path.join(root, "%s-%d-%d" % (tag, os.getpid(), index))
        if not os.path.exists(path):
            os.makedirs(path)
            return path
        index += 1


def main():
    model = "deepseek-v4-1-flash-261017"
    for agent_id, plant_fn in sorted(CASES.items()):
        tmp = _scratch(agent_id)
        try:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            plant_fn(home)
            before = snapshot_bytes(home)
            with paths.redirect(home):
                store = settings_for(tmp)
                adapter = A.build(agent_id, home, "http://127.0.0.1:8088", "mvk_test", store,
                                  facts=facts())
                applied = adapter.apply(model, "high")
                after = snapshot_bytes(home)
                check("%s apply changed something" % agent_id, bool(applied.changed),
                      str(applied.changed))
                check("%s no warnings" % agent_id, not applied.warnings, str(applied.warnings))
                rolled = adapter.rollback()
                final = snapshot_bytes(home)
                same = before == final
                detail = ""
                if not same:
                    detail = "missing=%s extra=%s unequal=%s" % (
                        sorted(set(before) - set(final)),
                        sorted(set(final) - set(before)),
                        [k for k in before if k in final and before[k] != final[k]],
                    )
                check("%s rollback is byte-identical" % agent_id, same, detail)
                check("%s rollback touched files" % agent_id, bool(rolled.restored or rolled.removed))
                # 写进去的东西得真能被解析回去
                _assert_contents(agent_id, after, model)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    _key_passthrough_checks(model)
    _mixed_format_checks(model)
    _dsh_profile_checks()

    print()
    print("%d ok, %d failed" % (len(OK), len(FAILED)))
    return 1 if FAILED else 0


# ---- DSH profile 选择（回护 MP-04）------------------------------------------
#
# 一台机器上常有 desktop 和 web 两份 profile。早年按字母序取第一个，等于赌：赌错了
# 改的是另一份配置，当前会话毫无变化，还不报错。这一组守「选得对，选不了就报错」。

def _dsh_profile_checks():
    from core.agents.dsh import DshAdapter, ProfileError

    def adapter(home, hint=None):
        # 不走 A.build：那会把 fact/store 一起装配，这里只想测选目录。
        a = object.__new__(DshAdapter)
        a.home = Path(home)
        if hint is not None:
            a.profile_hint = hint
        return a

    saved = {k: os.environ.pop(k, None) for k in ("DSH_PROFILE", "DSH_PROFILE_DIR")}
    try:
        # 两个 profile，没有任何依据 -> 必须报错而不是猜一个
        tmp = _scratch("dshprof")
        try:
            home = os.path.join(tmp, "home")
            root = os.path.join(home, ".dsh", "profiles")
            for name in ("desktop", "web"):
                os.makedirs(os.path.join(root, name))
            try:
                adapter(home)._profile()
                check("dsh profile: ambiguous raises", False, "没抛异常")
            except ProfileError as exc:
                check("dsh profile: ambiguous raises", "desktop, web" in str(exc), str(exc))

            # 显式指定优先于一切
            chosen, source = adapter(home, "web")._profile()
            check("dsh profile: explicit wins", chosen.name == "web", chosen.name)

            # 指定了不存在的，要把可选项列出来
            try:
                adapter(home, "nope")._profile()
                check("dsh profile: bad hint raises", False, "没抛异常")
            except ProfileError as exc:
                check("dsh profile: bad hint raises", "desktop, web" in str(exc), str(exc))

            # 只有一个 profile 时不用问
            home2 = os.path.join(tmp, "home2")
            os.makedirs(os.path.join(home2, ".dsh", "profiles", "only"))
            chosen, source = adapter(home2)._profile()
            check("dsh profile: single auto", chosen.name == "only", chosen.name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ---- 混合排版（回护 MP-03）--------------------------------------------------
#
# 真人手写的 settings.json 常常是「外层多行、小对象挤一行」。只记缩进的话，重排会把
# 它们全展开，回滚还原不回原字节 —— 用户拿到一份没请我们改的 diff。这一组守的就是
# 这种文件 apply -> rollback 之后必须一个字节都不差。

MIXED_SETTINGS = (
    '{\n'
    '  "model": "claude-sonnet-4-5",\n'
    '  "env": {"ANTHROPIC_MODEL": "claude-sonnet-4-5", "FOO": "bar"},\n'
    '  "permissions": {"allow": ["Bash"]}\n'
    '}\n'
)


def _mixed_format_checks(model):
    tmp = _scratch("mixed")
    try:
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        plant(home, ".claude/settings.json", MIXED_SETTINGS)
        before = snapshot_bytes(home)
        with paths.redirect(home):
            store = settings_for(tmp)
            adapter = A.build("claude-code", home, "http://127.0.0.1:8088", "mvk_test", store,
                              facts=facts())
            applied = adapter.apply(model, "high")
            after = snapshot_bytes(home)
            check("mixed apply changed something", bool(applied.changed), str(applied.changed))
            # 接线后 env 仍应是一行 —— 我们只改值，不改排版。
            text = after[".claude/settings.json"].decode("utf-8")
            check("mixed env stays inline", '"env": {' in text, text[:200])
            check("mixed permissions stays inline", '"permissions": {' in text, text[:200])
            adapter.rollback()
            final = snapshot_bytes(home)
            check("mixed rollback is byte-identical", before == final,
                  repr(final.get(".claude/settings.json")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 接线 key 透传（回护 MP-01）---------------------------------------------
#
# 这一组守的是一个曾经的事故：``gateway_key_of`` 兜底成字面量 "marvis-proxy"，
# apply 还回报成功，结果接完线的 agent 拿一个不存在的 key 去请求，必然 401。
# 单测适配器测不出来——问题在「算 key」那一层，所以这里直接测那一层。

def _key_passthrough_checks(model):
    import api.wire as W

    class FakeCtx(dict):
        """够 wire 用的最小 ctx：只需要 provider_block 能算出来。"""

    ctx = FakeCtx(config={"host": "127.0.0.1", "port": 8088}, models=_models(),
                  data_dir=os.path.join(HERE, "data"))

    # 1) 请求体显式给了 key：必须原样用
    try:
        got = W.gateway_key_of(ctx, "mvk_from_request")
        check("key: explicit wins", got == "mvk_from_request", got)
    except Exception as exc:
        check("key: explicit wins", False, "%s: %s" % (type(exc).__name__, exc))

    # 2) 没给 key，但这次请求带着一把鉴权通过的 key（ctx 里回填）：用它
    ctx["wire_key"] = "mvk_from_auth_header"
    try:
        got = W.gateway_key_of(ctx)
        check("key: falls back to auth'd key", got == "mvk_from_auth_header", got)
    except Exception as exc:
        check("key: falls back to auth'd key", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        ctx.pop("wire_key", None)

    # 3) 什么 key 都没有：服务自己发一把 mkx_，绝不兜底成假 key
    #    （早年这里 `return "marvis-proxy"`，接完线的 agent 必然 401 却回报成功）
    from core.store import Store
    import api.keys as K
    wdb = os.path.join(_scratch("wirekey"), "w.db")
    wctx = FakeCtx(config={"host": "127.0.0.1", "port": 8088}, models=_models(),
                   data_dir=os.path.dirname(wdb), store=Store(wdb))
    try:
        got = W.gateway_key_of(wctx)
        check("key: no fallback literal", got.startswith(K.WIRE_PREFIX), got)
        check("key: auto key is a real usable key", K.lookup(wctx, got) is not None, got[:14])
        check("key: auto key is not a user key",
              not got.startswith(K.PREFIX) or got.startswith(K.WIRE_PREFIX), got[:14])
        # 再要一次必须还是同一把，不能每次接线发新的
        check("key: auto key is stable", W.gateway_key_of(wctx) == got, "两次不一致")
        # 它不出现在 key list 里
        check("key: auto key hidden from list",
              all(k.get("prefix", "") != got[:12] for k in K.list_keys(wctx)["keys"]),
              str(K.list_keys(wctx)["keys"])[:80])
        # 也不允许被 enable/disable/delete（key_action 按 sha256 前缀找）
        try:
            K.key_action(wctx, K._h(got)[:12], "delete")
            check("key: auto key is not manageable", False, "居然删掉了")
        except ValueError as exc:
            check("key: auto key is not manageable", "一键接入" in str(exc), str(exc)[:60])
    finally:
        shutil.rmtree(os.path.dirname(wdb), ignore_errors=True)

    # 4) 没有 key 也要能接线（刚装完 agent 的典型场景），且接完就能用：
    #    ① 配置确实写进去了 ② key 位是 mkx_ 真钥匙 ③ 带一句说明来路的警告
    tmp = _scratch("keyless")
    try:
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        plant(home, ".claude/settings.json", '{"model": "old"}\n')
        kctx = FakeCtx(config={"host": "127.0.0.1", "port": 8088}, models=_models(),
                       data_dir=os.path.join(tmp, "data"),
                       store=Store(os.path.join(tmp, "data", "k.db")))
        with paths.redirect(home):
            try:
                res = W.apply_model(kctx, model, home=home, agent_ids=["claude-code"])
                errs = [r.get("error") for r in res.get("results") or [] if r.get("error")]
                check("key: keyless apply succeeds", bool(res.get("ok")) and not errs,
                      str(errs)[:120] if errs else str(res.get("ok")))
                check("key: keyless apply explains the auto key",
                      any("mkx_" in w for w in res.get("warnings") or []),
                      str(res.get("warnings"))[:120])
            except Exception as exc:
                check("key: keyless apply succeeds", False,
                      "%s: %s" % (type(exc).__name__, exc))
                check("key: keyless apply explains the auto key", False, "上一步就抛了")
            # 写进去的必须是真钥匙，不是占位符
            text = open(os.path.join(home, ".claude/settings.json"), encoding="utf-8").read()
            check("key: keyless writes auto key",
                  K.WIRE_PREFIX in text and W.PENDING_KEY not in text,
                  text[:120].replace("\n", " "))
            # 而且这把钥匙拿去查得到 = agent 拿它说话不会被 401
            data = json.loads(text)
            written = data["env"]["ANTHROPIC_AUTH_TOKEN"]
            check("key: written key is usable", K.lookup(kctx, written) is not None,
                  written[:14])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 5) 写进配置的 key 不是自己库里的：给警告，但不拦
    #    （允许写别处的 key，但写错了要到第一次对话才 401，太晚）
    class StoreCtx(FakeCtx):
        """带一个真的 Store，用来验证 lookup 能不能查到 key。"""

    from core.store import Store
    import api.keys as K

    sdb = os.path.join(_scratch("keywarn"), "k.db")
    ctx2 = StoreCtx(config={"host": "127.0.0.1", "port": 8088}, models=_models(),
                    data_dir=os.path.dirname(sdb), store=Store(sdb))
    try:
        real = K.create_key(ctx2, {"note": "selftest"})["key"]
        check("key: known key has no warning", W._key_warning(ctx2, real) == "",
              W._key_warning(ctx2, real)[:60])
        warn = W._key_warning(ctx2, "mvk_typo_0000000000")
        check("key: unknown key warns", "不在本服务的库里" in warn, warn[:60])
        check("key: lookup finds real key", W.key_is_known(ctx2, real) is True)
        check("key: lookup misses typo", W.key_is_known(ctx2, "mvk_typo_0000000000") is False)
        # 空串不该当成「有效」
        check("key: empty is unknown", W.key_is_known(ctx2, "") is False)
    finally:
        shutil.rmtree(os.path.dirname(sdb), ignore_errors=True)
def _assert_contents(agent_id, files, model):
    if agent_id == "claude-code":
        data = json.loads(files[".claude/settings.json"].decode("utf-8"))
        check("cc base url has no /v1", data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8088",
              data["env"]["ANTHROPIC_BASE_URL"])
        check("cc model written", data["model"] == model, data["model"])
        check("cc ANTHROPIC_MODEL removed", "ANTHROPIC_MODEL" not in data["env"])
        check("cc ctx from models.json",
              data["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000",
              data["env"].get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
    elif agent_id == "codex":
        text = files[".codex/config.toml"].decode("utf-8")
        check("codex wire_api is chat", 'wire_api = "chat"' in text, text[:200])
        check("codex model", 'model = "%s"' % model in text)
        check("codex comment kept", "# 我的注释" in text)
    elif agent_id == "dsh":
        text = files[".dsh/profiles/desktop/cordis.patch.yml"].decode("utf-8")
        check("dsh route added", "marvis-proxy:" in text)
        check("dsh default points at route", "provider: marvis-proxy" in text)
        check("dsh key only in credentials",
              "mvk_test" not in text and b"mvk_test" in files[".dsh/.credentials.yaml"])
    elif agent_id == "zcode":
        data = json.loads(files[".zcode/v2/provider_config.json"].decode("utf-8"))
        rules = data["config"]["providerConfigRules"]["providerRules"]
        check("zcode rule added", any(r.get("providerId") == "marvis-proxy" for r in rules))
        check("zcode existing rule kept", any(r.get("providerId") == "wb-proxy" for r in rules))
        check("zcode base url ends at /v1",
              any(r.get("config", {}).get("api", {}).get("baseUrl") == "http://127.0.0.1:8088/v1"
                  for r in rules))


if __name__ == "__main__":
    raise SystemExit(main())
