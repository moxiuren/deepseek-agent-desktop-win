"""PoW 验收基准（n≥100，零上游请求）。

对应待办 **T-17**：`docs/handover-20261016-PoW加速.md` 要求 n ≥ 100 的真实
分布证据，此前只有 n=3–6，p90/max 不可当结论用。

**为什么用合成挑战而不是线上真实挑战**：本基准要回答的是
「求解耗时的分布」与「多进程是否降级」，这两件事只取决于
`difficulty` 与答案在区间里的位置，与挑战来自服务端还是本地无关。
合成挑战的答案位置用固定种子随机取，等价于服务端行为，
而且**一次上游请求都不发** —— 既不给账号增加风险，也不会建测试会话。

**为什么不能连续 `solve()`**：常驻池收尾只 `cancel()` 未开始的分片，
已在跑的 worker 要跑完当前 chunk（`chunk_size × 单哈希耗时`）才空闲。
实测（2026-10-18）：连跑时 nonce=39772 反而比 nonce=62445 慢 2.4 倍。
所以每轮之间必须等 worker 真正空闲，否则量到的是 CPU 超订后的数字。

用法：
    cd engine && ./.venv/Scripts/python.exe -X utf8 tools/bench_pow_accept.py --n 100
    cd engine && ./.venv/Scripts/python.exe -X utf8 tools/bench_pow_accept.py --n 100 --gap 2.5

产出：控制台表格 + `%TEMP%/gad-pow-accept.json`（供归档进 docs/evidence/）。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import pow as P

SALT = "accept_bench_salt"
EXPIRE = 1789148689858
DIFFICULTY = 144000


def make_challenge(nonce: int) -> P.Challenge:
    """造一个答案固定在 `nonce` 的挑战 —— 与 `tests/test_pow.py` 同法。"""
    target = P.ds_hash_hex(f"{SALT}_{EXPIRE}_{nonce}".encode())
    return P.Challenge(
        algorithm="DeepSeekHashV1",
        challenge=target,
        salt=SALT,
        difficulty=DIFFICULTY,
        signature="",
        expire_at=EXPIRE,
    )


def pct(xs: list[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认一致）。样本少时比「取第 k 个」稳。"""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="样本数（默认 100）")
    ap.add_argument("--gap", type=float, default=2.0,
                    help="每轮之间的等待秒数，让上一轮 worker 跑完当前 chunk（默认 2.0）")
    ap.add_argument("--seed", type=int, default=20261018)
    ap.add_argument("--json-out", default="", help="结果 JSON 路径，留空写 %%TEMP%%")
    args = ap.parse_args()

    import random

    rng = random.Random(args.seed)
    # 答案位置在 [0, difficulty) 内均匀取 —— 与真实服务端行为一致。
    nonces = [rng.randrange(DIFFICULTY) for _ in range(args.n)]

    rows: list[dict] = []
    print(f"PoW 验收基准 · n={args.n} · difficulty={DIFFICULTY} · gap={args.gap}s")
    print("-" * 74)
    print(f"{'#':>4}  {'nonce':>7}  {'耗时 ms':>9}  {'mode':>8}  {'workers':>7}  {'建池':>4}")

    for i, nonce in enumerate(nonces):
        if i:
            time.sleep(args.gap)
        ch = make_challenge(nonce)
        t0 = time.monotonic()
        try:
            r = P.solve(ch, "/api/v0/chat/completion")
        except Exception as exc:  # noqa: BLE001 - 基准要记录失败而不是中断
            rows.append({"i": i, "nonce": nonce, "error": repr(exc)})
            print(f"{i:>4}  {nonce:>7}  {'ERR':>9}  {repr(exc)[:30]}")
            continue
        dt = (time.monotonic() - t0) * 1000.0
        rows.append({
            "i": i, "nonce": nonce, "ms": round(dt, 1),
            "mode": r.get("_mode"), "workers": r.get("_workers"),
            "created": r.get("_pool_created"), "trials": r.get("_trials"),
            "pool_startup_ms": r.get("_pool_startup_ms"),
            "answer_ok": r.get("answer") == nonce,
        })
        print(f"{i:>4}  {nonce:>7}  {dt:>9.0f}  {r.get('_mode', '?'):>8}  "
              f"{r.get('_workers', 0):>7}  {r.get('_pool_created')!s:>4}")

    ok = [r for r in rows if "ms" in r]
    if not ok:
        print("\n[错误] 没有任何成功样本")
        return 1

    ms = [r["ms"] for r in ok]
    modes: dict[str, int] = {}
    for r in ok:
        modes[r["mode"]] = modes.get(r["mode"], 0) + 1
    wrong = [r for r in ok if not r["answer_ok"]]
    # 只统计**真正新建**的轮次（`created=True`）。复用轮的 `pool_startup_ms`
    # 只剩「首个分片的计算时间」，把它和 spawn 成本混在一起算分位数是错的。
    startups = [r["pool_startup_ms"] for r in ok
                if r.get("created") and r.get("pool_startup_ms") is not None]

    summary = {
        "n": len(ok),
        "difficulty": DIFFICULTY,
        "gap_s": args.gap,
        "seed": args.seed,
        "modes": modes,
        "answer_mismatch": len(wrong),
        "ms": {
            "min": round(min(ms), 1),
            "p50": round(pct(ms, 0.50), 1),
            "p90": round(pct(ms, 0.90), 1),
            "p99": round(pct(ms, 0.99), 1),
            "max": round(max(ms), 1),
            "mean": round(statistics.fmean(ms), 1),
            "stdev": round(statistics.pstdev(ms), 1),
        },
        "pool_startup_ms": {
            "n": len(startups),
            "p50": round(pct(startups, 0.50), 1) if startups else None,
            "max": round(max(startups), 1) if startups else None,
        },
        "cpu_count": os.cpu_count(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S +0800"),
    }

    print("-" * 74)
    print(f"成功 {len(ok)} / {args.n}   答案不符 {len(wrong)}   mode 分布 {modes}")
    print(f"耗时 ms   min {summary['ms']['min']:.0f} · p50 {summary['ms']['p50']:.0f}"
          f" · p90 {summary['ms']['p90']:.0f} · p99 {summary['ms']['p99']:.0f}"
          f" · max {summary['ms']['max']:.0f}")
    created = sum(1 for r in ok if r.get("created"))
    if startups:
        print(f"建池段 ms p50 {summary['pool_startup_ms']['p50']:.0f}"
              f" · max {summary['pool_startup_ms']['max']:.0f}"
              f"（{created} 次新建 / {len(ok)} 轮，其余复用）")

    out = args.json_out or os.path.join(tempfile.gettempdir(), "gad-pow-accept.json")
    Path(out).write_text(json.dumps({"summary": summary, "rows": rows},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写 {out}")

    # 判据：mode 恒为 multi 且答案全对，才算通过。
    bad = (modes.get("degraded", 0) > 0) or bool(wrong)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
