"""PoW 热循环基准 —— 只量**单次哈希**的成本，不掺网络与进程池。

用法：
    cd engine && ./.venv/Scripts/python.exe tools/bench_pow_kernel.py
    cd engine && ./.venv/Scripts/python.exe tools/bench_pow_kernel.py --n 20000

为什么单独拉一个基准而不是靠 `selftest()`：`selftest` 量的是
「解出某个已知答案」的端到端时间，受难度、答案位置、进程池启动影响，
**噪声远大于待测的改动**。改 `_absorb` 省下的那点时间会被 spawn 的 4 秒盖掉。

这里量的是 `_search_chunk` 的内部循环：固定跑 N 个 nonce，target 设成
一个必然不命中的值，于是每个 nonce 都走完整的
「拼块 → absorb → 23 轮 → squeeze → 比较」，正好是要优化的那条路径。

判据（三条都必须成立）：
  1. 吞吐提升 ≥ 15%（相对改前基线）
  2. 哈希结果与金标准向量**逐字节相等**
  3. 边界输入（空串、135 字节满块）与改前一致
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import pow as P

# 必然不命中的 target：全 0xFF。真实 challenge 是 Keccak 输出，
# 撞上 32 字节全 FF 的概率是 2^-256，可以当不可能事件。
MISS = b"\xff" * 32

# 与 selftest 里那条线上真实挑战同形的前缀（长度也对齐）
PREFIX = b"94ca9d35e51f15b7677a_1789148689858_"


def bench(n: int, repeat: int) -> float:
    """跑 repeat 轮，每轮 n 个 nonce，返回**最好那一轮**的「微秒/哈希」。

    取最好而不是平均：Windows 上后台进程、GC、CPU 频率抖动都会让某些轮
    明显变慢，平均值会把这类噪声算进结论。最好的那轮最接近纯计算成本。
    """
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        P._search_chunk((0, n, PREFIX, MISS))
        dt = time.perf_counter() - t0
        best = min(best, dt / n * 1e6)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8000, help="每轮 nonce 数（默认 8000）")
    ap.add_argument("--repeat", type=int, default=5, help="重复轮数，取最好（默认 5）")
    ap.add_argument("--json", action="store_true", help="只输出一行机器可读结果")
    args = ap.parse_args()

    us = bench(args.n, args.repeat)

    # 正确性同轮验：改性能不能动结果
    ok_hash = P.ds_hash_hex(b"3c1b67cba98a8235c00d_1788633061248_0") == (
        "a77d69cad11ee53b776e0b5fb61ba36074e67eaaa1b6644ef5006deb6be55453"
    )

    if args.json:
        print(f'{{"us_per_hash": {us:.3f}, "n": {args.n}, "hash_ok": {str(ok_hash).lower()}}}')
    else:
        print(f"每哈希 {us:7.3f} us   （{args.n} nonce × {args.repeat} 轮取最好）")
        print(f"折算 144000 难度单核 {us * 144000 / 1e6:6.2f} s")
        print(f"金标准向量 {'' if ok_hash else '不'}相符")
    return 0 if ok_hash else 1


if __name__ == "__main__":
    raise SystemExit(main())
