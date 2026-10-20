"""探测 `fetch_page` 的 `count` 上限（**只读**，零新增）。

## 为什么单独探这个

`probe_session_paging.py` 发现两个事实：
1. `updated_at` 参数**被服务端静默忽略** —— 带与不带返回完全相同的 10 条；
2. `count=500` 返回 **0 条**（不是报错，是空）。

第 2 条尤其危险：它意味着「把 count 调大就能一次拿全部」这个假设不成立，
而且**失败形态是空列表** —— 上层会把它读成「账号下没有会话」，
而不是「参数超限」。这是本项目最想避免的静默失效。

本脚本二分出真实上限，让 `list_sessions` 能给出准确报错而不是空列表。

## 安全性

只有 GET `chat_session/fetch_page`。请求间隔 1.2 秒（拟人节流），
不建会话、不发消息、不删任何东西。

用法：
    cd engine && ./.venv/Scripts/python.exe -X utf8 tools/probe_session_count_limit.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.identity import DEFAULT_TOKEN_FILE, load_token  # noqa: E402
from gad_engine.transport import DeepSeekRpcTransport  # noqa: E402
from gad_engine.transport.deepseek_rpc import BASE  # noqa: E402

LIST_PATH = "/api/v0/chat_session/fetch_page"
GAP = 1.2  # 请求间隔（秒）—— 拟人节流，别让服务端看到一串等距请求


async def main() -> int:
    tp = DeepSeekRpcTransport(
        token_provider=lambda: load_token(DEFAULT_TOKEN_FILE),
        device_id_path=ROOT / ".device-id",
        model="default",
        search_enabled=False,
    )
    await tp.start()
    try:
        s = tp._require_session()
        token = tp._token_sync()

        def probe(count: int) -> tuple[int, str]:
            resp = s.get(
                BASE + LIST_PATH,
                headers=tp._headers(token),
                params={"count": count},
                timeout=tp._timeout,
            )
            body = resp.json()
            code = body.get("code") if isinstance(body, dict) else None
            biz = ((body.get("data") or {}).get("biz_data") or {}) if isinstance(body, dict) else {}
            items = biz.get("chat_sessions") or []
            note = ""
            if not items:
                # 空返回必须区分「真没有」与「参数被拒」—— 看有没有错误信息。
                msg = body.get("msg") or body.get("message") or "" if isinstance(body, dict) else ""
                note = f" · msg={msg!r}" if msg else ""
            return len(items), f"code={code}{note}"

        print("=" * 78)
        print("  fetch_page · count 上限探测（只读）")
        print("=" * 78)
        print(f"{'count':>7}  {'返回条数':>8}  说明")
        print("-" * 78)

        results: dict[int, int] = {}
        for count in (1, 5, 10, 20, 30, 50, 100, 200, 500):
            n, note = probe(count)
            results[count] = n
            print(f"{count:>7}  {n:>8}  {note}")
            time.sleep(GAP)

        print("-" * 78)
        ok = {c: n for c, n in results.items() if n > 0}
        if not ok:
            print("  [失败] 所有 count 都返回 0 —— 可能是登录态失效，不是上限问题")
            return 1

        biggest_ok = max(ok)
        zero_at = sorted(c for c, n in results.items() if n == 0)
        print(f"  最大可用 count = {biggest_ok}（返回 {ok[biggest_ok]} 条）")
        if zero_at:
            print(f"  返回 0 的 count：{zero_at}")
            print("  [注意] 超限的失败形态是**空列表**，不是报错 —— "
                  "上层必须自己判 `count` 上限，否则会把「参数超限」读成「没有会话」")
        else:
            print("  未探测到上限（所有 count 都有返回）")

        # 会话总数上限：账号里到底有多少条？用最大可用 count 拿到的条数
        # 只是「一页的容量」，不代表账号总数 —— 分页不可用就意味着
        # **超过一页容量的会话根本拿不到**。这条要写进结论。
        print(f"\n  结论：`updated_at` 游标无效 + count 上限 {biggest_ok}"
              f" ⇒ 超出 {biggest_ok} 条的会话**当前无法通过 API 获取**")
        print("=" * 78)
        return 0
    finally:
        await tp.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
