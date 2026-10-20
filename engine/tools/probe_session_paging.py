"""探测 `chat_session/fetch_page` 的分页能力（**只读**，零新增）。

## 要回答的问题

`fetch_page` 只有 `count` 一个已知参数。会话多了以后（账号里可能上百条），
要么把 `count` 调到很大（一次拉全部，服务端可能截断且响应体巨大），
要么找到真正的分页游标。网页版常见做法是用 `updated_at` 当「往下翻」的游标：
**返回 `updated_at` 严格小于给定值的会话**。

## 判定标准（三条都要成立才算分页可用）

1. **游标真的生效**：带 `updated_at` 与不带返回的**不是同一批**；
2. **严格递减**：返回的条目 `updated_at` 全部 `<` 游标值；
3. **可穷尽**：连续翻页不重复、不漏（用 id 集合验证）。

三条里任何一条不成立，就说明这个参数是「被服务端静默忽略」——
那是本类接口最典型的陷阱：参数不报错、返回照旧，调用方以为翻页了。

## 安全性

全程只有 GET `chat_session/fetch_page` 与 GET `chat/history_messages`，
**不建会话、不发消息、不删任何东西**。跑它对账号零新增痕迹。

用法：
    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/probe_session_paging.py
    ./.venv/Scripts/python.exe -X utf8 tools/probe_session_paging.py --count 10 --pages 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.identity import DEFAULT_TOKEN_FILE, load_token  # noqa: E402
from gad_engine.transport import DeepSeekRpcTransport  # noqa: E402
from gad_engine.transport.deepseek_rpc import BASE  # noqa: E402

LIST_PATH = "/api/v0/chat_session/fetch_page"


def to_seconds(ts: float) -> float:
    """统一成秒（接口实测返回秒级浮点，但按量级兜底毫秒）。"""
    if not ts:
        return 0.0
    return ts / 1000.0 if ts > 1e11 else float(ts)


def summarize(items: list[dict]) -> tuple[list[str], list[float]]:
    ids = [str(s.get("id") or "") for s in items if isinstance(s, dict)]
    ts = [to_seconds(float(s.get("updated_at") or 0)) for s in items if isinstance(s, dict)]
    return ids, ts


async def main() -> int:
    ap = argparse.ArgumentParser(description="探测 fetch_page 分页游标（只读）")
    ap.add_argument("--count", type=int, default=10, help="每页条数，默认 10")
    ap.add_argument("--pages", type=int, default=4, help="最多翻几页，默认 4")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON 便于归档")
    args = ap.parse_args()

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

        def page(extra: dict | None = None) -> tuple[list[dict], int, str]:
            params = {"count": args.count}
            if extra:
                params.update(extra)
            resp = s.get(
                BASE + LIST_PATH,
                headers=tp._headers(token),
                params=params,
                timeout=tp._timeout,
            )
            body = resp.json()
            code = (body.get("code") if isinstance(body, dict) else None)
            biz = ((body.get("data") or {}).get("biz_data") or {}) if isinstance(body, dict) else {}
            return biz.get("chat_sessions") or [], code, str(body)[:200]

        # ---- 基线：不带游标 ----
        base, code0, raw0 = page()
        base_ids, base_ts = summarize(base)
        print("=" * 78)
        print(f"  基线（count={args.count}）· 返回 {len(base)} 条 · code={code0}")
        print("=" * 78)
        if not base:
            print(f"  返回为空，原始响应片段：{raw0}")
            return 1
        print(f"  最新 {base_ts[0]:.3f}  最旧 {base_ts[-1]:.3f}")
        print(f"  首条 id {base_ids[0][:8]}   末条 id {base_ids[-1][:8]}")

        # ---- 游标探测：拿基线最旧那条的 updated_at 往下翻 ----
        cursor = base_ts[-1]
        print("\n" + "-" * 78)
        print(f"  用 updated_at={cursor:.3f} 作游标翻页")
        print("-" * 78)

        seen = set(base_ids)
        prev_cursor = cursor
        ok_strict = True
        ok_diff = True
        overlaps: list[str] = []

        for p in range(1, args.pages):
            items, code, _raw = page({"updated_at": cursor})
            ids, ts = summarize(items)
            if not items:
                print(f"  第 {p + 1} 页：空（翻到头了）· code={code}")
                break

            dup = [i for i in ids if i in seen]
            if dup:
                overlaps.extend(dup)
                ok_diff = False
            # 严格递减：全部 < 游标
            bad = [t for t in ts if t >= cursor]
            if bad:
                ok_strict = False

            print(f"  第 {p + 1} 页：{len(items)} 条 · 最新 {ts[0]:.3f} · 最旧 {ts[-1]:.3f}"
                  f" · 与已见重复 {len(dup)} 条 · 越过游标的 {len(bad)} 条 · code={code}")
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=2)[:2000])

            seen.update(ids)
            new_cursor = min(ts)
            if new_cursor >= prev_cursor:
                print("  [警告] 游标没有前进（新的最旧时间 >= 旧游标）—— 分页可能失效")
                ok_diff = False
                break
            prev_cursor = cursor = new_cursor

        print("\n" + "-" * 78)
        print("  判定")
        print("-" * 78)
        print(f"  [{'通过' if ok_diff else '失败'}] 游标生效（返回与已见不重复）"
              + (f" · 重复 {len(overlaps)} 条" if overlaps else ""))
        print(f"  [{'通过' if ok_strict else '失败'}] 严格递减（返回条目 updated_at < 游标）")
        print(f"  累计去重后共 {len(seen)} 条")

        # 若基线就拿到了全部会话，则「游标生效」无从验证 —— 必须诚实标注。
        total_probe, _, _ = page({"count": 500})
        total_ids, _ = summarize(total_probe)
        print(f"  （对照：count=500 一次拉到 {len(total_ids)} 条）")
        if len(total_ids) <= len(base):
            print("  [说明] 账号会话数 ≤ 每页条数，**本次无法验证分页** —— 需要更多会话")
        print("=" * 78)

        return 0 if (ok_diff and ok_strict) else 1
    finally:
        await tp.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
