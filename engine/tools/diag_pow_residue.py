"""诊断：常驻池的「残留污染」—— 命中后 worker 还在跑多久（**纯本地**）。

## 要回答的问题

`n=100` 验收基准里出现 2 次 `degraded`（单进程降级），耗时 25–58 秒。
两次都**紧跟在「极快命中」之后**：

| 前一轮 | 下一轮 |
|---|---|
| #14 `nonce=12871` 626ms | #15 `degraded` **57688ms** |
| #26 `nonce=90` 48ms（首个分片就命中） | #27 `degraded` **25305ms** |

假设：`ProcessPoolExecutor.cancel()` **取消不了已经投进队列的分片**。
命中后 36 个分片里约 35 个「看起来被 cancel 了」，实际 worker 仍会逐个
取出来执行完（每个 `chunk_size=4096` × ~218µs ≈ 0.9 秒）。
8 个 worker 跑完 35 个残留分片要 3.9 秒；这段时间里下一轮的
首个分片排在队尾 → 10 秒 `startup_timeout` 等不到 → 降级单进程
→ 单进程又要和 8 个残留 worker 抢 CPU → 25–58 秒。

## 判据

1. **命中很快的轮次之后，紧接着的下一轮显著变慢** —— 且 gap 越短越慢；
2. **gap 足够长（残留跑完）后恢复正常**；
3. 残留期间 `os.cpu_count()` 个核应接近满载 —— 用 `time.process_time`
   或 wall/CPU 比值侧面佐证（本脚本用 wall 耗时对比即可）。

## 安全性

**零上游请求**。合成挑战、本地求解，不碰网络、不碰账号。

用法：
    cd engine && ./.venv/Scripts/python.exe -X utf8 tools/diag_pow_residue.py
    cd engine && ./.venv/Scripts/python.exe -X utf8 tools/diag_pow_residue.py --gaps 0.5 2 5
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import pow as P

SALT = "residue_diag_salt"
EXPIRE = 1789148689858
DIFFICULTY = 144000


def make_challenge(nonce: int) -> P.Challenge:
    target = P.ds_hash_hex(f"{SALT}_{EXPIRE}_{nonce}".encode())
    return P.Challenge(
        algorithm="DeepSeekHashV1",
        challenge=target,
        salt=SALT,
        difficulty=DIFFICULTY,
        signature="",
        expire_at=EXPIRE,
    )


def run_one(nonce: int) -> dict:
    ch = make_challenge(nonce)
    t0 = time.monotonic()
    r = P.solve(ch, "/api/v0/chat/completion")
    return {
        "nonce": nonce,
        "ms": round((time.monotonic() - t0) * 1000, 1),
        "mode": r.get("_mode"),
        "workers": r.get("_workers"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gaps", type=float, nargs="+", default=[0.0, 1.0, 3.0, 8.0],
                    help="快速命中之后等多少秒再做下一轮（默认 0/1/3/8）")
    ap.add_argument("--fast", type=int, default=90,
                    help="「快速命中」用的 nonce（默认 90，落在第一个分片）")
    ap.add_argument("--slow", type=int, default=20258,
                    help="「慢轮」用的 nonce（默认 20258，与基准 #27 一致）")
    args = ap.parse_args()

    print("=" * 78)
    print("常驻池残留污染诊断")
    print("=" * 78)
    print(f"  difficulty={DIFFICULTY}  cpu={P.os.cpu_count()}")
    print(f"  快速命中 nonce={args.fast}   慢轮 nonce={args.slow}")
    print()

    # ---- 基线：池刚建好、没有任何残留时跑一次慢轮 ----
    print("-" * 78)
    print("  基线：全新池（无残留）跑慢轮")
    print("-" * 78)
    base = run_one(args.slow)
    print(f"  nonce={base['nonce']:>6}  {base['ms']:>8.0f} ms  mode={base['mode']}"
          f"  workers={base['workers']}")
    base_ms = base["ms"]
    print()

    results: list[dict] = []
    for gap in args.gaps:
        print("-" * 78)
        print(f"  gap={gap}s：先跑快速命中，等 {gap}s，再跑慢轮")
        print("-" * 78)
        fast = run_one(args.fast)
        print(f"    快速轮 nonce={fast['nonce']:>6}  {fast['ms']:>8.0f} ms  mode={fast['mode']}")
        if gap > 0:
            time.sleep(gap)
        slow = run_one(args.slow)
        ratio = slow["ms"] / base_ms if base_ms else 0.0
        print(f"    慢轮   nonce={slow['nonce']:>6}  {slow['ms']:>8.0f} ms  mode={slow['mode']}"
              f"  （基线 {base_ms:.0f} ms 的 {ratio:.2f}x）")
        results.append({"gap": gap, "fast_ms": fast["ms"], "slow_ms": slow["ms"],
                        "mode": slow["mode"], "ratio": round(ratio, 2)})
        print()

    print("=" * 78)
    print("  判定")
    print("=" * 78)
    print(f"  {'gap':>6}  {'快速轮':>9}  {'慢轮':>9}  {'vs 基线':>9}  mode")
    for r in results:
        print(f"  {r['gap']:>6.1f}  {r['fast_ms']:>9.0f}  {r['slow_ms']:>9.0f}"
              f"  {r['ratio']:>8.2f}x  {r['mode']}")

    ratios = [r["ratio"] for r in results]
    degraded = any(r["mode"] == "degraded" for r in results)
    short_gap = results[0]["ratio"] if results else 1.0
    long_gap = results[-1]["ratio"] if results else 1.0
    print()
    print(f"  最短 gap {results[0]['gap']}s → {short_gap:.2f}x")
    print(f"  最长 gap {results[-1]['gap']}s → {long_gap:.2f}x")
    print(f"  ratio 中位 {statistics.median(ratios):.2f}x")

    # ---------- 判据（两档，对应两个修复阶段） ----------
    #
    # 这个脚本被用了两次，目的不同，所以判据也分两档：
    #
    #   **A. 残留是否存在**（修复前）—— 短 gap 显著更慢且随 gap 收敛。
    #      2026-10-18 首次运行的结果：0s→2.74x / 1s→2.17x / 3s→1.00x / 8s→0.91x。
    #
    #   **B. 修复是否生效**（滚动提交 + 协作式中断之后）—— 短 gap 不再显著更慢。
    #      这才是现在的期望形态。判据 A 在修复后**必须不成立**，
    #      否则说明残留还在。
    #
    # 阈值取 1.5x：实测噪声下同一挑战连续跑两次的波动约 ±20%，
    # 1.5x 留了足够余量，同时能抓住修复前的 2.74x。
    RATIO_THRESHOLD = 1.5
    if degraded:
        print("  [警告] 出现 degraded —— 残留仍能压垮下一轮，修复未生效")
        verdict = 1
    elif short_gap > RATIO_THRESHOLD:
        print(f"  [未通过] 短 gap 仍 >{RATIO_THRESHOLD}x —— 残留依然存在")
        verdict = 1
    elif short_gap > long_gap * 1.3:
        print("  [未通过] 短 gap 明显慢于长 gap —— 残留随 gap 收敛的特征还在")
        verdict = 1
    else:
        print(f"  [通过] 短 gap {short_gap:.2f}x ≤ {RATIO_THRESHOLD}x 且不随 gap 收敛"
              " —— 残留已消除")
        verdict = 0
    print("=" * 78)
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
