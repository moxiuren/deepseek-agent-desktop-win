"""PoW 多进程池可用性诊断（不联网、不碰 token）。

目的：查清 `mode=degraded` 的根因 —— 是 spawn 池在本环境根本不可用，
还是只在某种启动形态下不可用（比如被 stdio 管道子进程拉起时）。

用固定挑战（答案已知 80804）测三条路径，逐条打印：
  1. 直接在当前进程里跑多进程 solve
  2. 通过 subprocess + 管道拉起一个子进程跑多进程 solve（复现引擎的启动形态）
  3. 跑完再跑一次，看 `_parallel_disabled` 是否被置位、是否永久降级

用法：
    cd engine && ./.venv/Scripts/python.exe -X utf8 tools/diag_pow_pool.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine import pow as P  # noqa: E402

#: `pow.py::selftest()` 里那个 2026-09-12 的线上真实挑战，答案 80804。
CH = P.Challenge(
    algorithm="DeepSeekHashV1",
    challenge="a8f7edfeb5c161a8cada44aaf10163272931267ebbd5ac5a085da51c084961d2",
    salt="94ca9d35e51f15b7677a",
    difficulty=144000,
    signature="68440d49b655ed5b500474e79a84f9d8a07e5cda67f8114e75433b87cb84adf8",
    expire_at=1789148689858,
)
WANT = 80804


def run_one(tag: str) -> dict:
    """跑一次多进程 solve，打印全部诊断字段。"""
    t0 = time.monotonic()
    try:
        got = P.solve(CH, "/api/v0/chat/completion")
    except Exception as exc:  # noqa: BLE001 - 诊断脚本，任何异常都是结果
        return {"tag": tag, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    wall = (time.monotonic() - t0) * 1000.0
    return {
        "tag": tag,
        "ok": got["answer"] == WANT,
        "answer": got["answer"],
        "mode": got.get("_mode"),
        "workers": got.get("_workers"),
        "elapsed_ms": got.get("_elapsed_ms"),
        "wall_ms": round(wall, 1),
        "pool_startup_ms": got.get("_pool_startup_ms"),
        "pool_created": got.get("_pool_created"),
        "parallel_disabled": P._parallel_disabled,
    }


def child_entry() -> int:
    """子进程入口：跑一次多进程 solve，把结果以 JSON 打到 stdout。

    这个函数存在的唯一理由：复现「引擎被另一个进程用管道拉起」的形态。
    `pow.py` 的注释说 spawn 池在那种形态下会静默卡死 —— 要验证它。
    """
    P._parallel_disabled = False  # 子进程里是全新模块状态，显式清一次
    res = run_one("child")
    print("RESULT:" + json.dumps(res, ensure_ascii=False))
    return 0


def main() -> int:
    print("=" * 72)
    print("  PoW 多进程池诊断")
    print("=" * 72)
    print(f"  Python     : {sys.version.split()[0]}")
    print(f"  可执行文件 : {sys.executable}")
    print(f"  CPU 核数   : {os.cpu_count()}")
    print(f"  spawn 池开关 GAD_POW_POOL = {os.environ.get('GAD_POW_POOL', '(未设, 默认开)')}")
    print(f"  workers 覆盖 GAD_POW_WORKERS = {os.environ.get('GAD_POW_WORKERS', '(未设, 自动)')}")
    print(f"  挑战 difficulty = {CH.difficulty}（答案 {WANT}）")
    print("=" * 72)

    # ---------- 1. 当前进程直接跑 ----------
    print("\n[1] 当前进程直接跑多进程")
    print(f"    起始 _parallel_disabled = {P._parallel_disabled}")
    r1 = run_one("direct")
    print(f"    {json.dumps(r1, ensure_ascii=False)}")
    if not r1.get("ok"):
        print("    [失败] 直接跑都没成功 —— 继续看子进程形态，但先记下这一条")

    # ---------- 2. 子进程 + 管道 ----------
    print("\n[2] 子进程 + 管道（复现引擎启动形态）")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", __file__, "--child"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        print("    [严重] 子进程 180 秒未返回 —— 这就是「静默卡死」")
        return 1
    wall = (time.monotonic() - t0) * 1000.0
    line = next((ln for ln in (proc.stdout or "").splitlines()
                 if ln.startswith("RESULT:")), "")
    if not line:
        print(f"    [失败] 子进程没吐结果。returncode={proc.returncode}")
        print(f"    stdout 尾部：{(proc.stdout or '')[-500:]}")
        print(f"    stderr 尾部：{(proc.stderr or '')[-800:]}")
    else:
        r2 = json.loads(line[len("RESULT:"):])
        r2["subprocess_wall_ms"] = round(wall, 1)
        print(f"    {json.dumps(r2, ensure_ascii=False)}")

    # ---------- 3. 再跑一次，看是否永久降级 ----------
    print("\n[3] 同进程再跑一次（看 _parallel_disabled 是否被永久置位）")
    print(f"    当前 _parallel_disabled = {P._parallel_disabled}")
    r3 = run_one("again")
    print(f"    {json.dumps(r3, ensure_ascii=False)}")
    if r3.get("mode") == "degraded":
        print("    [确认] 本进程已永久降级 —— 后续所有轮次都走单进程")

    print("\n" + "=" * 72)
    print("  判读")
    print("=" * 72)
    direct_ok = r1.get("ok") and r1.get("mode") == "multi"
    print(f"  直接多进程 : {'可用' if direct_ok else '不可用'}")
    print(f"  最终降级   : {P._parallel_disabled}")
    return 0


if __name__ == "__main__":
    if "--child" in sys.argv:
        raise SystemExit(child_entry())
    raise SystemExit(main())
