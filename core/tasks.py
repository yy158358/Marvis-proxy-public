# -*- coding: utf-8 -*-
"""生图任务机：线程池跑长任务，请求线程只拿 id / 查状态。

状态：queued → submitted（拿到上游 task id）→ working（轮询中）→ completed / failed
产物：先写 *.part 再 os.replace 成正式文件名，避免客户端拿到半个文件。
"""
import os
import threading
import time
import traceback
import uuid

from cap import image as image_cap
from core.store import iso, now


class TaskManager(object):
    def __init__(self, store, pool, config, data_dir, models=None):
        from concurrent.futures import ThreadPoolExecutor
        self.store = store
        self.pool = pool
        self.config = config
        self.models = models or {}
        self.files_dir = os.path.join(data_dir, "files")
        if not os.path.isdir(self.files_dir):
            os.makedirs(self.files_dir)
        workers = int(config.get("image_workers") or 3)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="img")
        self._lock = threading.Lock()
        self._stopping = False   # 优雅退出：置位后正在轮询的任务立刻中止并如实标 interrupted
        self._recover()

    def _recover(self):
        """重启恢复：queued 重新排队；submitted/working 已丢上游关联，只能如实标失败。"""
        try:
            for row in self.store.q("SELECT id FROM tasks WHERE state='queued'"):
                self.executor.submit(self._run, row["id"])
            self.store.x("UPDATE tasks SET state='failed', error=?, updated_at=? "
                         "WHERE state IN ('submitted','working')",
                         ("interrupted: 服务重启导致任务中断，请重新提交", now()))
        except Exception:
            traceback.print_exc()

    # ---------- 对外 ----------
    def submit(self, prompt, size, client_key="", model=""):
        task_id = "img_" + uuid.uuid4().hex[:20]
        ts = now()
        self.store.x(
            "INSERT INTO tasks(id,client_key,model,size,prompt,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'queued',?,?)",
            (task_id, client_key, model, size, prompt, ts, ts))
        self.executor.submit(self._run, task_id)
        return self.get(task_id)

    def get(self, task_id):
        row = self.store.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            return None
        return self.public(row)

    def public(self, row):
        url = ""
        if row["file"]:
            url = "/files/" + row["file"]
        return {
            "id": row["id"],
            "object": "image.generation.task",
            "created": int(row["created_at"]),
            "status": row["state"],
            "model": row["model"],
            "size": row["size"],
            "prompt": row["prompt"],
            "error": row["error"] or None,
            "file": row["file"] or None,
            "data": [{"url": url}] if url else [],
            "upstream_id": row["upstream_id"] or None,
            "retries": int(row["retries"] or 0) if "retries" in row.keys() else 0,
            "error_kind": (row["error_kind"] or None) if "error_kind" in row.keys() else None,
            "updated": iso(row["updated_at"]),
        }

    # ---------- 执行 ----------
    def _set(self, task_id, **fields):
        fields["updated_at"] = now()
        keys = ", ".join([k + "=?" for k in fields])
        self.store.x("UPDATE tasks SET %s WHERE id=?" % keys,
                     tuple(fields.values()) + (task_id,))

    def stop(self):
        """进程收到 SIGTERM 时调用：让在跑的任务别死等 image_timeout。"""
        self._stopping = True

    def _run(self, task_id):
        if self._stopping:
            self._set(task_id, state="failed", error="interrupted: 服务正在退出，任务未开始执行")
            return
        row = self.store.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            return
        # 云端沙箱失败是偶发：默认重提一次。重试仍失败才判死。
        max_retry = int(self.config.get("image_retries") or 0)
        err = ""
        for attempt in range(max_retry + 1):
            outcome, err = self._attempt(task_id, row)
            if outcome == "done":
                return
            if outcome == "fatal":
                suffix = "（重试 %d 次后仍失败）" % attempt if attempt else ""
                self._set(task_id, state="failed", error="%s%s" % (err, suffix))
                return
            # outcome == "retry"：还有额度才再提一次（最后一轮不再排队，直接落到下面的 failed）
            if self._stopping:
                self._set(task_id, state="failed", error="interrupted: 服务正在退出，任务中断")
                return
            if attempt < max_retry:
                self._set(task_id, state="queued", retries=attempt + 1, error="")
                time.sleep(float(self.config.get("image_retry_delay") or 2))
        self._set(task_id, state="failed",
                  error="%s（已重试 %d 次）" % (err or "failed", max_retry))

    def _attempt(self, task_id, row):
        """返回 ('done'|'retry'|'fatal', 错误串)。done=已成功落盘。"""
        acc = None
        try:
            acc = self.pool.pick(row["client_key"], None)
            if acc is None:
                return "fatal", "no_account: 账号池里没有可用账号"
            conv_id = image_cap.new_conv()
            self._set(task_id, account_id=acc["id"], conv_id=conv_id, state="submitted")

            def on_event(kind, data):
                if kind == "stage" and data.get("title"):
                    self._set(task_id, state="working")

            r = image_cap.submit(acc, self.config, row["prompt"] or "", int(row["size"] or 1024),
                                 conv_id, on_event=on_event)
            if not r.get("ok"):
                if r.get("status") == 401:
                    self.pool.on_unauthorized(acc["id"])
                    return "fatal", "submit_failed: 401 登录态过期"
                return "retry", "submit_failed: %s" % (r.get("error") or r.get("status"))
            self._set(task_id, upstream_id=r["task_id"], state="working")

            deadline = time.time() + float(self.config.get("image_timeout") or 480)
            interval = float(self.config.get("image_poll_interval") or 3)
            while True:
                if self._stopping:
                    # 退出时不再等：如实记成中断，而不是让用户在 480s 后看到 timeout
                    return "fatal", "interrupted: 服务正在退出，任务中断（请重新提交）"
                left = deadline - time.time()
                if left <= 0:
                    break
                time.sleep(min(interval, max(0.2, left)))
                left = deadline - time.time()
                if left <= 0:
                    break
                # 单次轮询的超时不能超过剩余预算，否则 deadline 形同虚设
                t = image_cap.get_task(acc, self.config, r["task_id"], conv_id,
                                       timeout=max(1.0, min(30.0, left)))
                if not t.get("ok"):
                    continue
                if t.get("stage"):
                    self._set(task_id, state="working")
                arts = t.get("artifacts") or []
                if arts:
                    self._save(task_id, acc, conv_id, arts[0])
                    return "done", ""
                state = t.get("state") or ""
                if state in ("TASK_STATE_FAILED", "TASK_STATE_CANCELED"):
                    # 云端沙箱内部失败：任务照常受理，属于正常分支，不扣账号分
                    self.pool.on_success(acc["id"])
                    return "retry", "image_task_failed: %s" % (t.get("error") or state)
            return "retry", "timeout: 超过 %ss 未完成" % self.config.get("image_timeout")
        except Exception as e:
            traceback.print_exc()
            return "retry", "exception: %s" % e

    def _save(self, task_id, acc, conv_id, art):
        url = image_cap.artifact_url(art)
        if not url:
            self._set(task_id, state="failed", error="artifact 里没有 url")
            return
        data, _ctype = image_cap.download(acc, self.config, url, conv_id)
        mime, ext = image_cap.sniff_mime(data[:16])
        name = task_id + ext
        tmp = os.path.join(self.files_dir, name + ".part")
        final = os.path.join(self.files_dir, name)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
        self._set(task_id, state="completed", file=name, error="")
        self.pool.on_success(acc["id"])
