# -*- coding: utf-8 -*-
"""马维斯反代入口：单进程 ThreadingHTTPServer，只监听 127.0.0.1。

路由：
  GET  /health
  GET  /v1/models
  POST /v1/chat/completions            （OpenAI 兼容，支持 stream）
  POST /v1/images/generations          （M3）
  GET  /v1/images/tasks/<id>           （M3）
  GET  /files/<name>                   （产物下载）
  POST /admin/login/start
  POST /admin/login/exchange
  GET  /admin/accounts
  POST /admin/accounts/<id>/<action>
  GET  /admin/usage
  POST /admin/cleanup
"""
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import traceback
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.admin as admin_api
import api.anthropic as anthropic_api
import api.keys as keys_api
import api.openai as openai_api
from cap import wallet as wallet_cap
from core.backup import make_backup, start_backup_thread
from core.pool import Pool
from core.store import Store

ROOT = os.path.dirname(os.path.abspath(__file__))
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".json": "application/json"}

# 控制台（人看的页面）挂在 console/static 下；/ 与 /console/* 都从这里取。
# 与手机端保持一致：绝不做 SPA 兜底（未知路径回 404 JSON，别回 HTML）。
CONSOLE_DIR = os.path.realpath(os.path.join(ROOT, "console", "static"))
CONSOLE_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


# ---------------------------------------------------------------- 日志落盘
# 不管前台还是后台，所有 stdout/stderr（含 BaseHTTPRequestHandler 的访问日志）都同时写进
# logs/serve.log。前台时屏幕上照样能看到；后台时屏幕上的窗口可以随便关掉不影响。
# 超过 log_max_bytes 就滚动成 serve.log.1（只留一份旧的），避免日志无限长。
LOG_DIR = os.path.join(ROOT, "logs")
LOG_PATH = os.path.join(LOG_DIR, "serve.log")
LOG_MAX = 5 * 1024 * 1024


class _Tee(object):
    """把写操作同时送往屏幕与文件；文件写失败（被杀毒锁住等）只丢文件那一份，不影响服务。"""

    def __init__(self, console, fh):
        self.console = console
        self.fh = fh

    def write(self, s):
        try:
            if self.console is not None:
                self.console.write(s)
                self.console.flush()
        except Exception:
            pass
        if self.fh is not None:
            try:
                self.fh.write(s)
                self.fh.flush()
            except Exception:
                pass
        return len(s) if s else 0

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        for x in (self.console, self.fh):
            try:
                if x is not None:
                    x.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self.console is not None and self.console.isatty())
        except Exception:
            return False

    def fileno(self):
        if self.console is not None:
            return self.console.fileno()
        raise OSError("no fileno")

    @property
    def encoding(self):
        return "utf-8"


def setup_logging():
    """在 main() 最开始调用；返回日志文件路径（失败返回 ""）。

    两件事：(1) 把进程的 stdout/stderr 强制成 UTF-8（否则中文 Windows 下按 cp936 写，
    日志文件会变成 GBK/UTF-8 混排，grep 和 tail 都会花）；(2) 之后所有输出分两路，
    一路屏幕一路 logs/serve.log。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX:
                prev = LOG_PATH + ".1"
                if os.path.exists(prev):
                    os.unlink(prev)
                os.replace(LOG_PATH, prev)
        except OSError:
            pass
        fh = open(LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        return ""
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    return LOG_PATH


# ------------------------------------------------------------ 服务端自我描述
# OpenAI 协议里没有「provider 名字」这个概念，客户端只能自己写死一个标签。
# 这里把名字做成服务端的一份自描述清单：新接进来的 agent 工具打一次 GET /v1/provider
# （或看任意响应里的 X-Marvis-Provider 头、/v1/models 顶层的 provider 块）就知道
# 这是谁、怎么说、有哪些模型，不用去读我们的 config.json，也不用在客户端重复配置。
PROVIDER_DEFAULT_NAME = "Marvis Proxy"
PROVIDER_DEFAULT_ID = "marvis-proxy"
PROVIDER_VERSION = "0.2.0"


def provider_name(ctx):
    cfg = (ctx or {}).get("config") or {}
    return cfg.get("provider_name") or PROVIDER_DEFAULT_NAME


def provider_block(ctx, full=False):
    """服务的自我描述。full=True 时多带模型明细、账号池概况、进程信息。"""
    cfg = (ctx or {}).get("config") or {}
    models = (ctx or {}).get("models") or {}
    aliases = models.get("aliases") or []
    ids = [a.get("name") for a in aliases if a.get("name")]
    base = "http://%s:%s" % (cfg.get("host"), cfg.get("port"))
    blk = {
        "object": "provider",
        "id": cfg.get("provider_id") or PROVIDER_DEFAULT_ID,
        "name": provider_name(ctx),
        "display_name": provider_name(ctx),
        "version": PROVIDER_VERSION,
        "api": "openai-completions",
        "base_url": base + "/v1",
        "auth": {"type": "bearer", "header": "Authorization",
                 "env": "MARVIS_PROXY_API_KEY", "note": "表里没有 enabled key 时不校验"},
        "features": {"chat": True, "stream": True, "tools": True, "vision": True,
                     "images": True, "anthropic": True, "count_tokens": True,
                     "reasoning": True, "aliases": True},
        "endpoints": {
            "health": base + "/health",
            "provider": base + "/v1/provider",
            "models": base + "/v1/models",
            "chat": base + "/v1/chat/completions",
            "messages": base + "/v1/messages",
            "count_tokens": base + "/v1/messages/count_tokens",
            "images": base + "/v1/images/generations",
            "image_task": base + "/v1/images/tasks/{id}",
            "files": base + "/files/{name}",
        },
        "model_ids": ids,
    }
    if full:
        pack = openai_api.models_payload(models)
        blk["models"] = pack.get("data") or []
        try:
            blk["accounts"] = ctx["pool"].counts()
        except Exception:
            pass
        blk["server"] = {"pid": os.getpid(), "now": int(time.time()),
                         "data_dir": cfg.get("data_dir"), "started": ctx.get("started")}
    return blk



def load_config():
    path = os.path.join(ROOT, "config.json")
    cfg = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    for k, env in (("host", "MARVIS_HOST"), ("port", "MARVIS_PORT"), ("data_dir", "MARVIS_DATA_DIR"),
                   ("admin_token", "MARVIS_ADMIN_TOKEN"), ("image_workers", "MARVIS_IMAGE_WORKERS")):
        v = os.environ.get(env)
        if v:
            cfg[k] = int(v) if k in ("port", "image_workers") else v
    cfg.setdefault("host", "127.0.0.1")
    cfg.setdefault("port", 8088)
    cfg.setdefault("data_dir", "data")
    data_dir = cfg["data_dir"]
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(ROOT, data_dir)
    cfg["data_dir"] = data_dir
    return cfg


_MCACHE = {"mtime": -1.0, "obj": None}


def load_models():
    """按 mtime 缓存：models.json 改了下次请求自动生效，不用重启。"""
    path = os.path.join(ROOT, "models.json")
    try:
        mt = os.stat(path).st_mtime
    except OSError:
        mt = -1.0
    if _MCACHE["obj"] is not None and mt == _MCACHE["mtime"]:
        return _MCACHE["obj"]
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    _MCACHE["mtime"], _MCACHE["obj"] = mt, obj
    return obj


def refresh_models(ctx):
    m = load_models()
    if m is not ctx.get("models"):
        ctx["models"] = m
        try:
            ctx["tasks"].models = m
        except Exception:
            pass
    return m


def build_ctx():
    config = load_config()
    data_dir = config["data_dir"]
    files_dir = os.path.join(data_dir, "files")
    if not os.path.isdir(files_dir):
        os.makedirs(files_dir)
    store = Store(os.path.join(data_dir, "marvis.db"))
    pool = Pool(store, config)
    models = load_models()
    from core.tasks import TaskManager
    tasks = TaskManager(store, pool, config, data_dir, models)
    return {"config": config, "store": store, "pool": pool, "models": models,
            "tasks": tasks, "data_dir": data_dir, "files_dir": files_dir, "root": ROOT,
            "started": time.time()}


def require_admin(ctx, handler):
    """管理接口的唯一鉴权入口。

    当前策略：config.admin_token 为空 = 本机无鉴权（只监听 127.0.0.1）。
    将来接 Web 控制台时，只在这里加一条「有效 session cookie 也放行」，
    七条 /admin/* 路由一行都不用动。
    """
    want = (ctx["config"].get("admin_token") or "").strip()
    if not want:
        return True
    got = (handler.headers.get("X-Admin-Token") or "").strip()
    if not got:
        auth = (handler.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    return got == want


def refresh_ahead(ctx):
    """提前主动续期：token 年龄过半就刷一次，不等 401。

    依据见 05 报告 10.3.1：refresh 在 token 仍有效时会把同一个 token 送回，
    真失效了再刷很可能刷不活，所以必须提前打点。失败会被 ensure_token 标成 expired。
    """
    pool = ctx["pool"]
    ratio = float(ctx["config"].get("refresh_ahead_ratio") or 0.5)
    due = pool.refresh_due(ratio)
    done = []
    for acc in due:
        row = openai_api.ensure_token(ctx, acc)
        if row is not None:
            sys.stderr.write("[refresh-ahead] 账号 %s 已提前续期（年龄比>=%.2f）\n" % (acc["id"], ratio))
            done.append(acc["id"])
        else:
            sys.stderr.write("[refresh-ahead] 账号 %s 提前续期失败（已由 ensure_token 记日志）\n" % acc["id"])
        sys.stderr.flush()
    return {"ok": True, "checked": len(due), "refreshed": done}


def start_cleaner(ctx):
    """后台清理线程：启动先跑一次，之后每 cleanup_interval_seconds 跑一次。

    Android 上没有 cron/systemd timer，所以只能挂在进程内；daemon 线程随主进程退出。
    清理本身复用 api.admin.cleanup()，与手动 POST /admin/cleanup 完全是同一段代码。
    """
    interval = float(ctx["config"].get("cleanup_interval_seconds") or 3600)
    tick = min(float(ctx["config"].get("maintenance_tick_seconds") or 300), interval)
    sweep = float(ctx["config"].get("refresh_sweep_seconds") or 3600)
    last = {"cleanup": 0.0, "refresh": 0.0}

    def _loop():
        while True:
            t = time.time()
            if t - last["cleanup"] >= interval:
                try:
                    r = admin_api.cleanup(ctx)
                    last["cleanup"] = t
                    sys.stderr.write("[cleanup] files=%s tasks=%s keep=%sd\n"
                                     % (r.get("removed_files"), r.get("removed_tasks"), r.get("keep_days")))
                except Exception as e:  # 清理失败绝不能让线程死掉
                    sys.stderr.write("[cleanup] failed: %r\n" % (e,))
            if t - last["refresh"] >= sweep:
                try:
                    refresh_ahead(ctx)
                    last["refresh"] = t
                except Exception as e:
                    sys.stderr.write("[refresh-ahead] failed: %r\n" % (e,))
            time.sleep(tick)

    t = threading.Thread(target=_loop, name="cleaner", daemon=True)
    t.start()
    return t


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "marvis-proxy/0.1"

    # ---------- 基础工具 ----------
    _SECRET_RX = re.compile(r"(?i)\b(key|api_key|api-key|token|access_token)=([^&\s\"']+)")

    def log_message(self, fmt, *args):
        line = fmt % args
        # 访问日志会把 URL 原样打出来，`?key=mvk_...` 这种必须打码，否则 key 进日志文件
        line = self._SECRET_RX.sub(lambda m: "%s=%s…" % (m.group(1), m.group(2)[:8]), line)
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), line))

    def _json(self, status, obj, extra_headers=None):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Marvis-Provider", provider_name(self.ctx))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        limit = int(self.ctx["config"].get("max_body_bytes") or 0)
        if limit > 0 and n > limit:
            # 不读、不解析，直接拒：否则一个 Content-Length 巨大的请求会把内存拖爆
            raise openai_api.ApiError(413, "请求体太大（%d 字节，上限 %d 字节）" % (n, limit),
                                      "payload_too_large")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            raise openai_api.ApiError(400, "请求体不是合法 JSON", "bad_json")

    def _client_key(self):
        ok, ident = keys_api.gate(self.ctx, self.headers)
        self.client_ok = ok
        return ident

    def _deny_key(self):
        return self._json(401, {"error": {
            "message": "缺少或无效的 API Key：请求头加 X-API-Key: <key>（或 Authorization: Bearer <key>）",
            "type": "invalid_request_error", "code": "invalid_api_key"}})

    def _key_from_query(self, query):
        """给 <img src>、status_url 这类带不了请求头的场景用：?key=<key>。

        返回 True/False/None（None = 没带 query key，交回头部判定）。
        """
        qs = parse_qs(query)
        for name in ("key", "api_key", "api-key", "token"):
            v = (qs.get(name) or [""])[0].strip()
            if v:
                ok, _ = keys_api.gate(self.ctx, {"X-API-Key": v})
                return ok
        return None

    def _admin_ok(self):
        return require_admin(self.ctx, self)

    @property
    def ctx(self):
        return self.server.ctx

    def handle_one_request(self):
        """客户端「连上就断」（浏览器预连接、探测脚本超时、ssh 隧道抖动）不该刷 Traceback。

        socketserver 认为这是 bug，会 print_exc；对反代来说只是噪音——按连接关闭处理即可。
        """
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    # ---------- 路由 ----------
    def do_GET(self):
        refresh_models(self.ctx)
        u = urlparse(self.path)
        path = unquote(u.path)
        try:
            if path == "/health":
                return self._json(200, admin_api.health(self.ctx))
            if path == "/metrics":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                qs = parse_qs(u.query)
                try:
                    win = float((qs.get("window") or ["60"])[0])
                except ValueError:
                    win = 60.0
                body = admin_api.metrics_text(self.ctx, win).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/admin/trace":
                # 给「另一个 agent / 另一个人」看链路用的观测口：最近 N 轮请求摘要。
                # 只要 admin_token（配了的话）。不含票据、不含正文、不含凭证明文。
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                qs = parse_qs(u.query)
                try:
                    lim = int((qs.get("limit") or ["20"])[0])
                except ValueError:
                    lim = 20
                try:
                    mins = float((qs.get("minutes") or ["60"])[0])
                except ValueError:
                    mins = 60.0
                return self._json(200, admin_api.trace(self.ctx, limit=lim, minutes=mins))
            if path == "/v1/models":
                payload = openai_api.models_payload(self.ctx["models"])
                payload["provider"] = provider_block(self.ctx)
                return self._json(200, payload)
            if path in ("/", "/index.html"):
                # 根路径给人：控制台首页（浏览器打开就是面板）
                return self._console_file("index.html")
            if path.startswith("/console"):
                # /console 下的一切都当静态资源（含 /console/ 与 /console/x.js）
                return self._console_file(path[len("/console"):])
            if path == "/favicon.ico":
                return self._console_file("favicon.ico")
            if path in ("/v1/provider", "/v1/info", "/provider"):
                # 给「新接进来的 agent 工具」一个自描述入口：不用读我们的配置文件，
                # 打一下就知道这是谁、支持什么、有哪些模型、key 怎么给。
                return self._json(200, provider_block(self.ctx, full=True))
            if path.startswith("/v1/images/tasks/"):
                # 建过 key 之后这里也要校验（可用 ?key= 传，方便直接贴 status_url）
                q_ok = self._key_from_query(u.query)
                if q_ok is None:
                    self._client_key()
                    q_ok = self.client_ok
                if not q_ok:
                    return self._deny_key()
                return self._json(200, openai_api.image_task(self.ctx, path[len("/v1/images/tasks/"):]))
            if path.startswith("/files/"):
                # 产物下载：<img src> 带不了头，所以额外支持 ?key=
                q_ok = self._key_from_query(u.query)
                if q_ok is None:
                    self._client_key()
                    q_ok = self.client_ok
                if not q_ok:
                    return self._deny_key()
                return self._file(path[len("/files/"):])
            if path == "/admin/sanitize":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, admin_api.sanitize_info(self.ctx))
            if path == "/admin/backups":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                import glob
                out_dir = os.path.abspath(os.path.join(self.ctx["data_dir"], "..", "backups"))
                files = sorted(glob.glob(os.path.join(out_dir, "marvis-*.tar.gz")))
                return self._json(200, {"ok": True, "dir": out_dir, "last": self.ctx.get("last_backup"),
                                        "files": [{"name": os.path.basename(f), "size": os.path.getsize(f)}
                                                  for f in files]})
            if path == "/admin/wallet":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                qs = parse_qs(u.query)
                return self._json(200, wallet_cap.snapshot(self.ctx, force=bool(qs.get("force"))))
            if path == "/admin/keys":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, keys_api.list_keys(self.ctx))
            if path == "/admin/accounts":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, admin_api.accounts(self.ctx))
            if path == "/admin/usage":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                qs = parse_qs(u.query)
                if qs.get("days") or qs.get("by"):
                    return self._json(200, admin_api.usage_report(
                        self.ctx, float((qs.get("days") or ["7"])[0]), (qs.get("by") or ["day"])[0]))
                limit = int((qs.get("limit") or ["50"])[0])
                return self._json(200, admin_api.usage(self.ctx, limit))
            return self._json(404, {"error": "not found: " + path})
        except openai_api.ApiError as e:
            return self._json(e.status, e.payload())
        except admin_api.AdminError as e:
            return self._json(e.status, {"error": e.message})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": "internal: %s" % e})

    def do_POST(self):
        refresh_models(self.ctx)
        u = urlparse(self.path)
        path = unquote(u.path)
        try:
            if path == "/v1/chat/completions":
                return self._chat()
            if path == "/v1/messages":
                return self._messages()
            if path == "/v1/messages/count_tokens":
                body = self._body()
                self._client_key()
                if not getattr(self, "client_ok", True):
                    return self._deny_key()
                return self._json(200, anthropic_api.count_tokens(body))
            if path == "/v1/images/generations":
                body = self._body()
                ck = self._client_key()
                if not getattr(self, "client_ok", True):
                    return self._deny_key()
                task = openai_api.images_generation(self.ctx, body, ck)
                return self._json(200 if body.get("wait") else 202, task)
            if path == "/admin/keys":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, keys_api.create_key(self.ctx, self._body()))
            if path.startswith("/admin/keys/"):
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                parts = path[len("/admin/keys/"):].split("/")
                kid = parts[0]
                action = parts[1] if len(parts) > 1 else ""
                try:
                    return self._json(200, keys_api.key_action(self.ctx, kid, action))
                except LookupError as e:
                    return self._json(404, {"error": str(e)})
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            if path == "/admin/login/start":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, admin_api.login_start(self.ctx, self._body()))
            if path == "/admin/login/exchange":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, admin_api.login_exchange(self.ctx, self._body()))
            if path.startswith("/admin/accounts/"):
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                parts = path[len("/admin/accounts/"):].split("/")
                acc_id = int(parts[0])
                action = parts[1] if len(parts) > 1 else ""
                return self._json(200, admin_api.account_action(self.ctx, acc_id, action, self._body()))
            if path == "/admin/backup":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, make_backup(self.ctx))
            if path == "/admin/refresh-ahead":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, refresh_ahead(self.ctx))
            if path == "/admin/sanitize/reset":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, admin_api.sanitize_reset(self.ctx))
            if path == "/admin/cleanup":
                if not self._admin_ok():
                    return self._json(401, {"error": "admin_token 不对"})
                return self._json(200, admin_api.cleanup(self.ctx))
            return self._json(404, {"error": "not found: " + path})
        except openai_api.ApiError as e:
            return self._json(e.status, e.payload())
        except admin_api.AdminError as e:
            return self._json(e.status, {"error": e.message})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": "internal: %s" % e})

    # ---------- 聊天 ----------
    def _chat(self):
        body = self._body()
        client_key = self._client_key()
        if not getattr(self, "client_ok", True):
            return self._deny_key()
        t_req = time.time()
        if self.ctx["config"].get("debug_requests"):
            # 排障用：把最近一次请求体落盘（覆盖写，避免堆文件）
            try:
                d = self.ctx["data_dir"]
                with open(os.path.join(d, "req-last.json"), "w", encoding="utf-8") as f:
                    json.dump({"at": time.time(), "path": "/v1/chat/completions", "body": body},
                              f, ensure_ascii=False)
            except Exception as e:
                sys.stderr.write("[debug] dump 失败: %s\n" % e)
        result = openai_api.chat(self.ctx, body, client_key)

        if result[0] == "json":
            _, name, slot, acc, raw, hdrs, ms, raised, retried, why = result
            obj = openai_api.build_json_response(self.ctx, name, slot, acc, raw, hdrs, ms,
                                                 raised, retried, why)
            return self._json(200, obj)

        _, name, slot, acc, resp, _meta = result
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Marvis-Provider", provider_name(self.ctx))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        gen = openai_api.stream_chunks(self.ctx, name, slot, acc, resp, client_key, t_req)
        try:
            for chunk in gen:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                      # 客户端走了：finally 里 close 生成器 → 关上游
        except Exception:
            traceback.print_exc()
        finally:
            try:
                gen.close()           # 显式收尾：取消上游读取 + 归还并发名额
            except Exception:
                pass

    # ---------- Anthropic Messages ----------
    def _messages(self):
        body = self._body()
        client_ident = self._client_key()
        if not getattr(self, "client_ok", True):
            return self._deny_key()
        t_req = time.time()
        result = anthropic_api.messages(self.ctx, body, client_ident, t_req)
        if result[0] == "json":
            return self._json(200, result[1])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Marvis-Provider", provider_name(self.ctx))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        gen = result[1]
        try:
            for chunk in gen:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                       # 客户端走了：close 生成器即取消上游
        except Exception:
            traceback.print_exc()
        finally:
            try:
                gen.close()
            except Exception:
                pass

    # ---------- 静态产物 ----------
    def _console_file(self, rel):
        """从 console/static 下取文件。只服务这个目录，绝不做 SPA 兜底。

        目录穿越用 realpath 前缀校验挡住；找不到就回 404 JSON（不要回 index.html，
        否则打错的 API 路径会变成一页 HTML，排错时极其误导）。
        """
        rel = unquote(rel or "").lstrip("/")
        if not rel or rel.endswith("/"):
            rel = rel + "index.html"
        full = os.path.realpath(os.path.join(CONSOLE_DIR, rel))
        if not (full == CONSOLE_DIR or full.startswith(CONSOLE_DIR + os.sep)):
            return self._json(404, {"error": "console: path outside root"})
        if not os.path.isfile(full):
            return self._json(404, {"error": "console: not found: " + rel})
        try:
            st = os.stat(full)
        except OSError:
            return self._json(404, {"error": "console: not found: " + rel})

        ext = os.path.splitext(full)[1].lower()
        etag = '"%x-%x"' % (st.st_size, int(st.st_mtime))
        last_mod = self.date_time_string(int(st.st_mtime))
        # html 每次协商（改了立刻生效）；静态资源给 1 小时，靠 ETag 兜底
        cache = "no-cache" if ext == ".html" else "public, max-age=3600"

        if self._not_modified(etag, int(st.st_mtime)):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_mod)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return

        try:
            with open(full, "rb") as f:
                raw = f.read()
        except OSError as e:
            return self._json(500, {"error": "console: read failed: %s" % e})

        self.send_response(200)
        self.send_header("Content-Type", CONSOLE_MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def _file(self, name):
        name = os.path.basename(name)
        path = os.path.join(self.ctx["files_dir"], name)
        if not os.path.isfile(path):
            return self._json(404, {"error": "file not found"})
        try:
            st = os.stat(path)
        except OSError:
            return self._json(404, {"error": "file not found"})
        size = st.st_size
        mtime = int(st.st_mtime)
        ext = os.path.splitext(name)[1].lower()
        start, end, partial = self._range(size)
        # 元数据指纹，零开销：文件被改写（os.replace）mtime 必变
        etag = '"%x-%x"' % (size, mtime)
        last_mod = self.date_time_string(mtime)

        # 有 Range 就先不管缓存协商：客户端要的是某一段，回 304 会让它拿不到数据
        if not partial and self._not_modified(etag, mtime):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_mod)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(end - start))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end - 1, size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start
            while left > 0:
                buf = f.read(min(65536, left))
                if not buf:
                    break
                left -= len(buf)
                try:
                    self.wfile.write(buf)
                except Exception:
                    return

    def _not_modified(self, etag, mtime):
        """条件请求判定：If-None-Match 优先，其次 If-Modified-Since。"""
        inm = (self.headers.get("If-None-Match") or "").strip()
        if inm:
            if inm == "*":
                return True
            for cand in inm.split(","):
                cand = cand.strip()
                if cand.startswith("W/"):
                    cand = cand[2:]
                if cand == etag:
                    return True
            return False                      # 有 INM 但不匹配 → 必须回 200 全量
        ims = (self.headers.get("If-Modified-Since") or "").strip()
        if ims:
            try:
                dt = parsedate_to_datetime(ims)
                if dt is not None:
                    return mtime <= int(dt.timestamp())
            except (TypeError, ValueError, OverflowError):
                return False
        return False

    def _range(self, size):
        """解析 Range: bytes=a-b。返回 (start, end_exclusive, is_partial)。

        只支持单段；不合法/不可满足一律退化成整文件 200（对客户端最安全）。
        """
        raw = (self.headers.get("Range") or "").strip()
        if not raw.lower().startswith("bytes=") or "," in raw:
            return 0, size, False
        spec = raw[6:].strip()
        a, _, b = spec.partition("-")
        try:
            if a == "":                      # bytes=-500 → 末尾 500 字节
                n = int(b)
                if n <= 0:
                    return 0, size, False
                return max(0, size - n), size, True
            start = int(a)
            end = int(b) + 1 if b != "" else size    # bytes=0-499 → 0..499
        except ValueError:
            return 0, size, False
        if start < 0 or start >= size:
            return 0, size, False
        end = min(end, size)
        if end <= start:
            return 0, size, False
        return start, end, True


class Server(ThreadingHTTPServer):
    """listen backlog 默认只有 5，30 个客户端同时连会直接被内核 reset（实测 32 并发丢 10 个）。
    这里抬到 512，让并发上限由 account_max_concurrency 决定，而不是内核队列。"""
    request_queue_size = 512
    daemon_threads = True
    # Windows 的 SO_REUSEADDR 语义是「允许别人抢占同一端口」，开着会让第二个实例静默绑上同一端口、
    # 和已经在跑的那个抢连接（表现为「一半请求正常、一半像没启动」）。Windows 关掉它，
    # 第二个实例就会 bind 失败并明确报「端口被占用」；Linux 仍保留（TIME_WAIT 快速重启需要）。
    allow_reuse_address = (os.name != "nt")


def _port_in_use(host, port):
    """先探一下端口：已经在跑就别再起一个（Windows 上抢端口很难查）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def main():
    log_path = setup_logging()
    ctx = build_ctx()
    cfg = ctx["config"]
    # 首次启动自动补齐设备指纹（qimei36）与 QIMEI 应用标识（appKey）：
    # 都从本机 Marvis 客户端提取，成功后写入 data/ 缓存并回写到 cfg。
    # 探测失败不中断启动，只是对应字段留空并在 stderr 给出指引。
    try:
        from cap import autoconfig
        _ac = autoconfig.ensure(cfg, ctx)
        if _ac["wrote"]:
            print("已自动写入 %s -> %s" % (", ".join(_ac["wrote"]), ctx["data_dir"]))
        if _ac["qimei36"]:
            print("设备指纹 qimei36 = %s" % _ac["qimei36"])
        else:
            print("设备指纹 qimei36 未取得（见上方提示）")
    except Exception as e:
        print("autoconfig 跳过：%r" % (e,))
    host, port = cfg["host"], int(cfg["port"])
    if _port_in_use(host, port):
        print("marvis-proxy 已经在 %s:%s 上跑了，不重复启动。" % (host, port))
        print("  查看状态： run.bat status      停止： run.bat stop")
        return 1
    try:
        httpd = Server((host, port), Handler)
    except OSError as e:
        print("端口 %s 绑定失败：%s" % (port, e))
        print("  换个端口：改 config.json 的 port，或设置环境变量 MARVIS_PORT")
        return 1
    httpd.ctx = ctx
    # pidfile：给 run.bat stop / run.sh stop 用（Windows 没有 /proc，只能靠它找进程）
    pid_path = os.path.join(ctx["data_dir"], "serve.pid")
    try:
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pid_path = None
    start_cleaner(ctx)
    start_backup_thread(ctx)
    print("marvis-proxy listening http://%s:%s  data=%s" % (cfg["host"], cfg["port"], cfg["data_dir"]))
    print("log -> %s（超过 %d MB 滚动成 serve.log.1）" % (log_path or "(落盘失败，只打到屏幕)", LOG_MAX // (1024 * 1024)))
    def _on_signal(signum, _frame):
        print("收到信号 %d，开始优雅退出（在跑的生图任务会标 interrupted）" % signum)
        try:
            ctx["tasks"].stop()          # 让轮询中的任务立刻收尾，别死等 image_timeout
        except Exception:
            pass
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if pid_path:
            try:
                os.unlink(pid_path)
            except OSError:
                pass
        print("marvis-proxy 已退出")
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main() or 0)
