"""列出账号下的服务端会话（**只读**，不建会话、不发消息、不产生任何痕迹）。

用途：清点账号里累计了多少会话、哪些是测试留下的、最近活跃时间分布。
判定「测试会话堆积」是否构成账号风险时，先要看清**事实**：
数量、时间分布、标题形态。

只用 `chat_session/fetch_page` 一个 GET 接口。跑它对账号零新增。

用法：
    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/list_remote_sessions.py
    ./.venv/Scripts/python.exe -X utf8 tools/list_remote_sessions.py --count 200 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.identity import DEFAULT_TOKEN_FILE, load_token  # noqa: E402
from gad_engine.transport import DeepSeekRpcTransport  # noqa: E402


def to_seconds(ts: float) -> float:
    """把 `updated_at` 统一成**秒**。

    2026-10-18 实测：`chat_session/fetch_page` 返回的是**秒级浮点**
    （`1792306981.417`），不是毫秒。但接口文档/其他实现里见过毫秒形态，
    所以按量级判断：`> 1e11` 一定是毫秒（1e11 秒 ≈ 5138 年）。
    """
    if not ts:
        return 0.0
    return ts / 1000.0 if ts > 1e11 else float(ts)


def fmt_ts(ts: float) -> str:
    """格式化 `updated_at`（自动识别秒/毫秒）。"""
    sec = to_seconds(ts)
    if not sec:
        return "(无)"
    return datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M")


async def main() -> int:
    ap = argparse.ArgumentParser(description="只读列出服务端会话")
    # 上限 100 是**服务端硬边界**（2026-10-18 实测 T-20）：超了会静默返回空列表。
    # 所以这里不给「调到 200 试试」的机会 —— 默认就取满。
    ap.add_argument("--count", type=int, default=100,
                    help="拉取条数，2–100，默认 100（服务端上限，超了会返回空列表）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出原始条目")
    args = ap.parse_args()

    tp = DeepSeekRpcTransport(
        token_provider=lambda: load_token(DEFAULT_TOKEN_FILE),
        device_id_path=ROOT / ".device-id",
        model="default",
        search_enabled=False,
    )
    await tp.start()
    try:
        sessions = await tp.list_sessions(args.count)
    finally:
        await tp.stop()

    if args.json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print(f"  服务端会话清单（只读）· 共 {len(sessions)} 条")
    print("=" * 78)
    if not sessions:
        print("  （账号下没有会话）")
        return 0

    rows = []
    for i, s in enumerate(sessions, 1):
        if not isinstance(s, dict):
            continue
        rows.append(
            {
                "idx": i,
                "id": str(s.get("id") or ""),
                "title": str(s.get("title") or "(无标题)"),
                "updated": float(s.get("updated_at") or 0.0),
            }
        )

    # 按时间倒序展示
    rows.sort(key=lambda r: r["updated"], reverse=True)
    for r in rows:
        print(f"  {r['idx']:>3}. {fmt_ts(r['updated'])}  "
              f"{r['title'][:44]:<44}  {r['id'][:8]}")

    print("\n" + "-" * 78)
    print("  按天分布")
    print("-" * 78)
    days = Counter(fmt_ts(r["updated"])[:10] for r in rows if r["updated"])
    for day, n in sorted(days.items(), reverse=True):
        bar = "#" * min(n, 60)
        print(f"  {day}  {n:>3}  {bar}")

    # 空标题会话通常是「建了但没发消息」的测试残留 —— 这是最关键的一条信号。
    empty = [r for r in rows if not r["title"].strip() or r["title"] == "(无标题)"]
    print("\n" + "-" * 78)
    print(f"  无标题（建了但没聊）会话：{len(empty)} / {len(rows)}")
    if empty:
        for r in empty[:20]:
            print(f"    · {fmt_ts(r['updated'])}  {r['id'][:8]}")
        if len(empty) > 20:
            print(f"    … 另有 {len(empty) - 20} 条")

    now = time.time()
    recent_24h = sum(1 for r in rows if r["updated"] and now - to_seconds(r["updated"]) < 86400)
    recent_1h = sum(1 for r in rows if r["updated"] and now - to_seconds(r["updated"]) < 3600)
    print("\n" + "-" * 78)
    print(f"  最近 1 小时新增/活跃：{recent_1h} 条")
    print(f"  最近 24 小时新增/活跃：{recent_24h} 条")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
