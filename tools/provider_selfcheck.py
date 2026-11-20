# -*- coding: utf-8 -*-
r"""离线自检：不启服务，直接把 provider 自我描述打出来看看对不对。

用法： py -3 tools\provider_selfcheck.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import serve  # noqa: E402


def main():
    ctx = serve.build_ctx()
    blk = serve.provider_block(ctx, full=True)
    text = json.dumps(blk, ensure_ascii=False, indent=2)
    print(text)
    problems = []
    if not blk.get("name"):
        problems.append("name 为空")
    if not blk.get("base_url"):
        problems.append("base_url 为空")
    if not blk.get("model_ids"):
        problems.append("model_ids 为空")
    if not blk.get("models"):
        problems.append("models 明细为空")
    print("\n--- 检查 ---")
    print("provider 名字：", blk.get("name"))
    print("模型 ID 数：", len(blk.get("model_ids") or []))
    if problems:
        print("FAIL:", "；".join(problems))
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
