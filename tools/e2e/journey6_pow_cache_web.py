#!/usr/bin/env python3
"""旅程 6：PoW 预取缓存到底省下了多少「按下回车后的空等」。

## 这条旅程测的不是功能，是**用户体感**

PoW（工作量证明）是 DeepSeek 网页端在每次发送前要求的本地计算。它发生在
「按下回车」与「请求真的发出去」之间，用户看到的只有一件事：

    点了没反应。

2026-10-16 的基线（184 轮真实发送，全部是「同步取 challenge + 同步求解」）：

    challenge_ms   中位  150.9 ms
    pow_ms         中位 8242.8 ms   ← 这才是用户干等的那段
    pool_startup   中位 4129.1 ms   ← 其中近一半花在重建进程池上

`pow_cache.py` 的思路是：**提前算**。后台 daemon 线程在空闲时取 challenge、
解好、入队；真正发送时直接 `take()` 一个现成的。这一条不改算法，就能把
绝大多数发送的 PoW 开销压到 0。

## 判据为什么必须分「命中」与「未命中」

只看「平均 pow_ms 下降了」是没有意义的 —— 命中率 10% 也能拉低平均值，
但用户 10 次里有 9 次仍在干等。所以判据拆成三条：

  1. **命中率** —— 缓存是否真的在工作（不是代码写了但没接线）
  2. **命中时 pow_ms / challenge_ms 必须严格为 0** —— 这是「用户真的没等」
     的唯一直接证据。0 必须是真实的 0，不是字段没测到
  3. **命中轮的 solve_ms 必须 > 0** —— 反证：缓存里装的是**真解出来的结果**，
     不是空壳。如果 solve_ms 也是 0，说明缓存把「没算」当成了「算好了」

## 一个容易误读的字段

`pow_ms` 与 `solve_ms` 的语义**刻意不同**：

  · `pow_ms`   = 这一轮**用户等了多久**（命中即 0）
  · `solve_ms` = 这个 challenge **当初算了多久**（后台线程的耗时，不影响用户）

所以「cached=True 但 solve_ms=10607」不是自相矛盾，恰恰是设计意图 ——
10607 ms 的计算被挪到了用户看不见的后台。日志注释里记着这个坑
（`deepseek_rpc.py:1546` 附近：早期版本误报成 `cached=True 但 pow_ms=6646`）。

用法：
    python tools/e2e/journey6_pow_cache_web.py            # 用当前 Web 壳的日志
    python tools/e2e/journey6_pow_cache_web.py --all      # 扫全部引擎日志（含历史基线）
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_common import LOG_DIR, engine_log_for, find_pid  # noqa: E402


def scan_pow(path: Path) -> list[dict]:
    """从一份引擎日志里取出全部 pow_stats 事件（按出现顺序）。"""
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        j = line.find('{"type": "event"')
        if j < 0:
            continue
        try:
            e = json.loads(line[j:])
        except Exception:
            continue
        if e.get("event") == "pow_stats":
            out.append({"ts": line[:8], **(e.get("data") or {})})
    return out


def med(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def describe(label: str, xs: list[float], unit: str = "ms") -> None:
    if not xs:
        print(f"  {label:24} n=0")
        return
    print(f"  {label:24} n={len(xs):3}  中位={med(xs):8.1f}  "
          f"均={statistics.mean(xs):8.1f}  min={min(xs):8.1f}  max={max(xs):8.1f} {unit}")


def report(title: str, rows: list[dict]) -> dict:
    """对一组 pow_stats 做分组统计，返回关键数字供断言使用。"""
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)
    if not rows:
        print("  （无数据）")
        return {}

    # `cached` 字段是 2026-10-18 才加进日志的 —— 更早的行没有它，
    # 那些行全部是「没有预取」的同步求解，正好当基线。
    legacy = [r for r in rows if "cached" not in r]
    modern = [r for r in rows if "cached" in r]
    hit = [r for r in modern if r.get("cached")]
    miss = [r for r in modern if not r.get("cached")]

    print(f"  事件总数: {len(rows)}   "
          f"旧格式(无 cached 字段): {len(legacy)}   "
          f"新格式: {len(modern)}（命中 {len(hit)} / 未命中 {len(miss)}）")

    if legacy:
        print("\n  --- 基线：无预取（同步求解）---")
        describe("challenge_ms", [r["challenge_ms"] for r in legacy])
        describe("pow_ms", [r["pow_ms"] for r in legacy])
        describe("pool_startup_ms", [r["pool_startup_ms"] for r in legacy])

    if modern:
        rate = 100.0 * len(hit) / len(modern)
        print(f"\n  --- 预取生效后 ---   命中率 {rate:.1f}%")
        if hit:
            describe("命中 · challenge_ms", [r["challenge_ms"] for r in hit])
            describe("命中 · pow_ms", [r["pow_ms"] for r in hit])
            describe("命中 · solve_ms(后台)", [r.get("solve_ms") or 0 for r in hit])
        if miss:
            describe("未命中 · pow_ms", [r["pow_ms"] for r in miss])

        # 连续命中尾段 —— 稳态判据。缓存刚上线时会有一两轮未命中（首个
        # challenge 还没算完用户就发了），那属于冷启动，不算缺陷。
        tail = 0
        for r in reversed(modern):
            if r.get("cached"):
                tail += 1
            else:
                break
        print(f"\n  末尾连续命中: {tail} 轮 / 共 {len(modern)} 轮")
    else:
        rate, tail = 0.0, 0

    return {
        "total": len(rows), "legacy": len(legacy), "modern": len(modern),
        "hit": len(hit), "miss": len(miss),
        "rate": 100.0 * len(hit) / len(modern) if modern else 0.0,
        "tail": tail,
        "base_pow_med": med([r["pow_ms"] for r in legacy]),
        "hit_pow_max": max([r["pow_ms"] for r in hit], default=0.0),
        "hit_ch_max": max([r["challenge_ms"] for r in hit], default=0.0),
        "hit_solve_min": min([r.get("solve_ms") or 0 for r in hit], default=0.0),
        "hit_rows": hit, "miss_rows": miss,
    }


def main() -> int:
    if "--all" in sys.argv:
        files = [Path(f) for f in sorted(glob.glob(str(LOG_DIR / "engine-*.log")))]
        label = "全部引擎日志"
    else:
        pid = find_pid()
        log, engine_pid = engine_log_for(pid)
        files = [log]
        label = f"当前 Web 壳（PID {pid}，引擎 {engine_pid}）的日志 {log.name}"

    print("统计范围:", label)
    print("日志文件数:", len(files))

    rows: list[dict] = []
    for f in files:
        rows.extend(scan_pow(f))

    s = report(label, rows)
    if not s:
        print("[失败] 一份日志里都没有 pow_stats —— 说明还没有真实发送过")
        return 1

    # 稳态尾段单独看：只统计「最近的连续命中」
    print()
    print("=" * 66)
    print("判据")
    print("=" * 66)

    ok_hit_exists = s["hit"] > 0
    ok_zero_pow = s["hit"] > 0 and s["hit_pow_max"] == 0.0
    ok_zero_ch = s["hit"] > 0 and s["hit_ch_max"] == 0.0
    ok_real_solve = s["hit"] > 0 and s["hit_solve_min"] > 0.0
    ok_rate = s["rate"] >= 50.0
    ok_tail = s["tail"] >= 2

    print(f"  缓存确实被命中                    : {ok_hit_exists}"
          f"（{s['hit']} 次）")
    print(f"  命中时 pow_ms 严格为 0            : {ok_zero_pow}"
          f"（最大 {s['hit_pow_max']:.1f}）")
    print(f"  命中时 challenge_ms 严格为 0      : {ok_zero_ch}"
          f"（最大 {s['hit_ch_max']:.1f}）")
    print(f"  命中轮 solve_ms > 0（装的是真解） : {ok_real_solve}"
          f"（最小 {s['hit_solve_min']:.1f}）")
    print(f"  整体命中率 >= 50%                 : {ok_rate}"
          f"（{s['rate']:.1f}%）")
    print(f"  末尾连续命中 >= 2 轮（稳态）      : {ok_tail}"
          f"（{s['tail']} 轮）")

    if s["base_pow_med"]:
        saved = s["base_pow_med"]
        print()
        print(f"  用户体感对比：基线每轮干等 {saved:.0f} ms → 命中时 0 ms"
              f"（单次省 {saved/1000:.1f} 秒）")

    passed = all((ok_hit_exists, ok_zero_pow, ok_zero_ch, ok_real_solve,
                  ok_rate, ok_tail))
    print()
    print(f"  判定: {'通过' if passed else '不通过'}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
