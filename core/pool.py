# -*- coding: utf-8 -*-
"""账号池：粘性 + 加权轮询。

规则（已拍板）：
  · 粘性键 = (client_key, session_id)；没有 session_id（生图）时退化成按 client_key 粘
  · 权重：401 → 归零 + 冷却 60s；429/5xx → -20（下限 20）；成功 → +5（上限 100）
  · 只有 401（4100403 登录态过期）算账号故障；模型不存在 4130001、生图 TASK_STATE_FAILED 都不扣分
"""
import random
import threading
import time

from core.store import now


class Pool(object):
    def __init__(self, store, config):
        self.store = store
        self.config = config
        self._lock = threading.Lock()          # 保护 refresh 单飞
        self._refreshing = set()                # openid
        self._inflight = {}                     # account_id -> 计数
        self._throttle_streak = {}              # account_id -> 连续 429 次数（指数退避用）

    # ---------- 增删查 ----------
    def add_account(self, info, note=""):
        """按 openid upsert。info 至少要有 openid/access_token。返回 account id。"""
        openid = info.get("openid")
        token = info.get("access_token") or info.get("accessToken")
        if not openid or not token:
            raise ValueError("缺少 openid 或 access_token")
        ts = now()
        exists = self.store.q1("SELECT id FROM accounts WHERE openid=?", (openid,))
        if exists:
            self.store.x(
                "UPDATE accounts SET access_token=?, refresh_token=COALESCE(NULLIF(?,''),refresh_token),"
                " guid=COALESCE(NULLIF(?,''),guid), login_type=COALESCE(NULLIF(?,''),login_type),"
                " appid=COALESCE(NULLIF(?,''),appid), scope=COALESCE(NULLIF(?,''),scope),"
                " status='active', weight=100, cooldown_until=0, refreshed_at=?, expires_in=?,"
                " note=COALESCE(NULLIF(?,''),note) WHERE id=?",
                (token, info.get("refresh_token", ""), info.get("guid", ""), info.get("login_type", ""),
                 info.get("appid", ""), info.get("scope", ""), ts, int(info.get("expires_in") or 0),
                 note or "", exists["id"]))
            return exists["id"]
        cur = self.store.conn().execute(
            "INSERT INTO accounts(openid,access_token,refresh_token,guid,login_type,appid,scope,"
            "created_at,refreshed_at,expires_in,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (openid, token, info.get("refresh_token", ""), info.get("guid", ""),
             info.get("login_type", "QC"), info.get("appid", ""), info.get("scope", ""),
             ts, ts, int(info.get("expires_in") or 0), note))
        return cur.lastrowid

    def get(self, account_id):
        return self.store.q1("SELECT * FROM accounts WHERE id=?", (account_id,))

    def all(self):
        return self.store.q("SELECT * FROM accounts ORDER BY id")

    def masked(self):
        out = []
        for r in self.all():
            exp_in = int(r["expires_in"] or 0)
            base = r["refreshed_at"] or r["created_at"] or 0
            expires_at = (base + exp_in) if (exp_in > 0 and base) else 0
            days_left = round((expires_at - now()) / 86400.0, 1) if expires_at else None
            warn = None
            if days_left is not None:
                if days_left <= 0:
                    warn = "已过期，下次请求会触发续期（失败则需重新加号）"
                elif days_left <= max(3.0, exp_in / 86400.0 * 0.1):
                    warn = "剩余 %.1f 天，建议提前重新加号" % days_left
            out.append({
                "id": r["id"],
                "openid": (r["openid"] or "")[:6] + "…",
                "status": r["status"],
                "weight": r["weight"],
                "cooldown": max(0, int(r["cooldown_until"] - now())),
                "ok_count": r["ok_count"],
                "fail_count": r["fail_count"],
                "last_used": int(r["last_used_at"]) or 0,
                "token_len": len(r["access_token"] or ""),
                "has_refresh": bool(r["refresh_token"]),
                "expires_at": int(expires_at) or 0,
                "days_left": days_left,
                "expiry_warn": warn,
                "note": r["note"],
            })
        return out

    def counts(self):
        """expired = 401 且续期也失败、必须人工重新加号的号（活着但干不了活，不能算 active）。"""
        rows = self.all()
        active = len([r for r in rows if r["status"] == "active" and r["cooldown_until"] <= now()])
        return {"total": len(rows), "active": active,
                "cooling": len([r for r in rows if r["cooldown_until"] > now()]),
                "expired": len([r for r in rows if r["status"] == "expired"])}

    # ---------- 调度 ----------
    def _usable(self, row):
        return row is not None and row["status"] == "active" and row["cooldown_until"] <= now() and row["weight"] > 0

    def _revive_if_due(self, row):
        """半开恢复：401 会把权重打成 0 并给 60s 冷却。冷却到期后如果不管它，
        weight>0 的门槛会让这个号永远出不来（实测发现：60s 冷却形同虚设）。
        所以到期第一次被扫到时抬到 cooldown_probe_weight（默认 20）试一把：
        再成 → 每次 +5 慢慢回血；再 401 → 归零重新冷却。"""
        if row is None or row["status"] != "active" or row["weight"] > 0 or row["cooldown_until"] > now():
            return row
        floor = int(self.config.get("cooldown_probe_weight") or 20)
        self.store.x("UPDATE accounts SET weight=? WHERE id=? AND weight<=0", (floor, row["id"]))
        return self.get(row["id"])

    def _candidates(self, respect_limit=False):
        """候选号：先把到期的 0 权重号复活，再过滤可用性与并发余量。"""
        for r in self.all():
            self._revive_if_due(r)
        rows = [r for r in self.all() if self._usable(r)]
        if respect_limit:
            rows = [r for r in rows if not self._over_limit(r["id"])]
        return rows

    def _choose(self, rows):
        """最小负载优先，同分按权重加权随机。

        · 只有一号 → 直接给（没得选，权重不参与）
        · 唯一最闲 → 直接给它（哪怕它权重低：别人都更忙）
        · 多个并列最闲 → P(i) = w_i / Σw，把可靠性折进去，但不把降权号踢出去
        """
        if len(rows) == 1:
            return rows[0]
        least = min(self.inflight(r["id"]) for r in rows)
        tied = [r for r in rows if self.inflight(r["id"]) == least]
        if len(tied) == 1:
            return tied[0]
        return random.choices(tied, weights=[max(1, r["weight"]) for r in tied], k=1)[0]

    # ---- 并发计数：单号最多 account_max_concurrency 个在途请求 ----
    def inflight(self, acc_id):
        with self._lock:
            return self._inflight.get(int(acc_id), 0)

    def acquire(self, acc_id):
        with self._lock:
            self._inflight[int(acc_id)] = self._inflight.get(int(acc_id), 0) + 1

    def release(self, acc_id):
        with self._lock:
            k = int(acc_id)
            n = self._inflight.get(k, 0) - 1
            if n <= 0:
                self._inflight.pop(k, None)
            else:
                self._inflight[k] = n

    def total_inflight(self):
        with self._lock:
            return sum(self._inflight.values())

    def _over_limit(self, acc_id):
        cap = int(self.config.get("account_max_concurrency") or 0)
        return cap > 0 and self.inflight(acc_id) >= cap

    def _wait_free(self, acc, wait_sec):
        """粘性号打满时，先等它腾出名额（等到了就继续用同一个号，上游 prompt 缓存不失效）。

        返回可用行；等不到 / 该号彻底不可用 → None（交给普通调度）。
        """
        poll = max(0.02, float(self.config.get("sticky_wait_poll_ms") or 100) / 1000.0)
        deadline = time.time() + max(0.0, wait_sec)
        while time.time() < deadline:
            time.sleep(min(poll, max(0.0, deadline - time.time())))
            row = self._revive_if_due(self.get(acc["id"]))
            if not self._usable(row):
                return None
            if not self._over_limit(row["id"]):
                return row
        return None

    def pick(self, client_key="", session_id=None, respect_limit=False, exclude=None):
        """返回一条 accounts 行，或 None。respect_limit=True 时跳过已打满并发的号。

        粘性规则（用户拍板）：会话锁定的号可用但打满时，**先排队等它**（默认 1.2s），
        等不到才换号；换号时不改写粘性绑定，下次仍回原来的号，避免缓存来回抖。
        """
        keep_binding = False
        if session_id:
            # 会话绑定有寿命：超过 session_ttl_days 的绑定当作不存在，重新分配（否则老会话永远粘死同一个号）
            ttl_days = float(self.config.get("session_ttl_days") or 0)
            if ttl_days > 0:
                s = self.store.q1(
                    "SELECT account_id FROM sessions WHERE key=? AND session_id=? AND at>?",
                    (client_key or "", session_id, now() - ttl_days * 86400))
            else:
                s = self.store.q1("SELECT account_id FROM sessions WHERE key=? AND session_id=?",
                                  (client_key or "", session_id))
            if s and (not exclude or s["account_id"] != exclude):
                acc = self._revive_if_due(self.get(s["account_id"]))
                if self._usable(acc):
                    if not (respect_limit and self._over_limit(acc["id"])):
                        self._touch(acc["id"])
                        return acc
                    wait = float(self.config.get("sticky_wait_ms") or 0) / 1000.0
                    if wait > 0:
                        got = self._wait_free(acc, wait)
                        if got is not None:
                            self._touch(got["id"])
                            return got
                    keep_binding = True      # 没等到：临时借别的号，但不改绑定
        rows = self._candidates(respect_limit)
        if exclude:
            # 401/质检失败后换号：把坏号排除掉，别原地打转
            rows = [r for r in rows if r["id"] != exclude]
        if not rows:
            return None
        acc = self._choose(rows)
        if session_id and not keep_binding:
            self.store.x(
                "INSERT INTO sessions(key,session_id,account_id,at) VALUES(?,?,?,?) "
                "ON CONFLICT(key,session_id) DO UPDATE SET account_id=excluded.account_id, at=excluded.at",
                (client_key or "", session_id, acc["id"], now()))
        self._touch(acc["id"])
        return acc

    def _touch(self, account_id):
        self.store.x("UPDATE accounts SET last_used_at=? WHERE id=?", (now(), account_id))

    # ---------- 反馈 ----------
    def on_success(self, account_id):
        self.store.x("UPDATE accounts SET weight=MIN(100, weight+5), ok_count=ok_count+1, fail_count=0 WHERE id=?",
                     (account_id,))
        self._throttle_streak.pop(account_id, None)

    def on_unauthorized(self, account_id):
        cd = float(self.config.get("cooldown_seconds") or 60)
        self.store.x("UPDATE accounts SET weight=0, cooldown_until=?, fail_count=fail_count+1 WHERE id=?",
                     (now() + cd, account_id))

    def on_throttled(self, account_id):
        """429：不是号的错，是打太急。指数退避 30s→60s→…→1800s 封顶，并扣权重。

        固定 60s 冷却在真·高并发下会「冷却到期立刻又被 429」，反而把权重磨到 20 出不来；
        指数退避让连续被限流的号慢慢退到后面，同时不永久踢出池子。
        """
        base = float(self.config.get("throttle_cooldown_seconds") or 30)
        cap = float(self.config.get("throttle_cooldown_max_seconds") or 1800)
        n = int(self._throttle_streak.get(account_id, 0)) + 1
        self._throttle_streak[account_id] = n
        cd = min(base * (2 ** (n - 1)), cap)
        self.store.x("UPDATE accounts SET weight=MAX(20, weight-20), cooldown_until=?,"
                     " fail_count=fail_count+1 WHERE id=?",
                     (now() + cd, account_id))
        return cd

    def throttle_streak(self, account_id):
        return int(self._throttle_streak.get(account_id, 0))

    def on_server_error(self, account_id):
        self.store.x("UPDATE accounts SET weight=MAX(20, weight-20), fail_count=fail_count+1 WHERE id=?",
                     (account_id,))

    def set_status(self, account_id, status, note=None):
        """status: active / disabled / expired。note 给了就一起改（用于「需重新加号」这类说明）。"""
        if note is None:
            self.store.x("UPDATE accounts SET status=?, weight=CASE WHEN ?='active' THEN 100 ELSE weight END,"
                         " cooldown_until=0 WHERE id=?", (status, status, account_id))
        else:
            self.store.x("UPDATE accounts SET status=?, weight=CASE WHEN ?='active' THEN 100 ELSE weight END,"
                         " cooldown_until=0, note=? WHERE id=?", (status, status, note, account_id))

    def delete(self, account_id):
        self.store.x("DELETE FROM sessions WHERE account_id=?", (account_id,))
        self.store.x("DELETE FROM accounts WHERE id=?", (account_id,))

    # ---------- 续期（单飞锁） ----------
    def lock_refresh(self, openid):
        """拿到锁返回 True；别人正在续这个号返回 False。"""
        with self._lock:
            if openid in self._refreshing:
                return False
            self._refreshing.add(openid)
            return True

    def unlock_refresh(self, openid):
        with self._lock:
            self._refreshing.discard(openid)

    def save_refreshed(self, account_id, info):
        self.store.x(
            "UPDATE accounts SET access_token=?, refresh_token=COALESCE(NULLIF(?,''),refresh_token),"
            " refreshed_at=?, expires_in=?, weight=MAX(weight,80), cooldown_until=0 WHERE id=?",
            (info["access_token"], info.get("refresh_token", ""), now(),
             int(info.get("expires_in") or 0), account_id))

    # ---------- 提前主动续期 ----------
    def refresh_due(self, ratio=None):
        """返回「token 年龄已超过寿命 ratio，该主动刷一次」的号。

        为什么不等 401：实测 refresh 接口在 token 有效时会把同一个 token 送回来，
        等它真的失效再刷，很可能刷不活（见 05 报告 10.3.1）。所以提前打点。
        """
        ratio = float(ratio if ratio is not None else (self.config.get("refresh_ahead_ratio") or 0.5))
        t = now()
        out = []
        for r in self.all():
            if r["status"] != "active":
                continue
            life = float(r["expires_in"] or 0)
            if life <= 0:
                continue
            born = float(r["refreshed_at"] or r["created_at"] or 0)
            if born <= 0:
                continue
            if (t - born) / life >= ratio:
                out.append(r)
        return out
