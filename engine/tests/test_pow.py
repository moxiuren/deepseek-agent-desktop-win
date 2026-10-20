"""PoW 求解器自测。

守护两件事：

1. **正确性** —— 用 C# 侧的金标准向量（`ds_hash_hex` 的已知输入输出）。
2. **降级路径** —— 多进程池不可用时**必须**降级单进程，而不是静默卡死。

第 2 条是 2026-10-15 实测事故的回归守护：引擎以 `python -m gad_engine` 跑在
另一个进程的子进程里时（外壳与冒烟脚本都这么起它），`multiprocessing` 的
spawn 池会**静默卡死** —— 池对象建起来了、任务发出去了、管理线程都健在，
但结果永远不回来。从外面看就是「消息发出去了，一直没有回复」，
既不抛异常也不打日志。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_pow.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from multiprocessing import shared_memory as _shm_mod
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import pow as P

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def make_challenge_at(nonce: int, difficulty: int) -> P.Challenge:
    """造一个**答案固定在 `nonce`** 的挑战：拿该 nonce 的哈希当目标。

    这样任何搜索路径都能在 `nonce` 处命中，测试只花在「路径本身」上，
    而不是花在真算 14 万次哈希上。

    `nonce=0` → 第一个候选就命中，适合测路径本身；
    `nonce=5000` → 落在第 1 个分片（`chunk_size=4096`），适合测**跨分片**搜索。
    """
    salt = "test_salt_for_pow_selftest"
    expire = 1789148689858
    target_hex = P.ds_hash_hex(f"{salt}_{expire}_{nonce}".encode())
    return P.Challenge(
        algorithm="DeepSeekHashV1",
        challenge=target_hex,
        salt=salt,
        difficulty=difficulty,
        signature="",
        expire_at=expire,
    )


def make_easy_challenge(difficulty: int) -> P.Challenge:
    """答案为 0 的挑战（保持旧名，语义 = `make_challenge_at(0, difficulty)`）。"""
    return make_challenge_at(0, difficulty)


def main() -> int:
    print("=" * 70)
    print("PoW 求解器自测")
    print("=" * 70)

    # ---------- 1. 正确性 ----------
    print("\n[1] 正确性 · 金标准向量")
    want = "a77d69cad11ee53b776e0b5fb61ba36074e67eaaa1b6644ef5006deb6be55453"
    got = P.ds_hash_hex(b"3c1b67cba98a8235c00d_1788633061248_0")
    check("哈希向量吻合（C# 侧金标准）", got == want, f"期望 {want} 实得 {got}")

    # ---------- 1b. 热循环优化对拍（T-15/T-16） ----------
    #
    # 2026-10-18 为提速把 `_search_chunk` 改成了「复用 state + scratch、
    # 整数比较代替 squeeze」。这两处都是**结果必须逐字节不变**的改动，
    # 而它们一旦写错，表现是「PoW 永远解不出来」或「解出错误 nonce」——
    # 后者更糟：服务端会拒绝，但日志里看不出是本地算错了。
    #
    # 所以这里做三组对拍，任何一组不符都说明热循环改动破坏了语义。
    print("\n[1b] 热循环优化对拍 · 复用缓冲不得改变结果")
    import random as _rnd

    _rng = _rnd.Random(20261018)
    _same = True
    _detail = ""
    for _ in range(200):
        _raw = bytes(_rng.getrandbits(8) for _ in range(P.RATE))
        _fresh = P._absorb(_raw)
        _reused = [0] * 25
        P._absorb_into(_raw, _reused)
        if _fresh != _reused:
            _same = False
            _detail = "absorb 与 absorb_into 结果不同"
            break
    check("_absorb 与 _absorb_into 逐 lane 相等", _same, _detail)

    # 复用缓冲的**真正风险**：上一轮的残值不清会污染本轮。
    # 连续 absorb 同一个块，第二次必须与第一次完全相同 —— 若 `_absorb_into`
    # 漏清后 8 lane，第二轮会带着第一轮的垃圾。
    _a1 = [0] * 25
    P._absorb_into(b"\x01" * P.RATE, _a1)
    _first = _a1[:]
    P._absorb_into(b"\x01" * P.RATE, _a1)
    check("复用 state 不串味（连续 absorb 同块结果一致）", _a1 == _first)

    # scratch 复用同理：连续两次完整轮函数，结果必须一致。
    _s1 = [_rng.getrandbits(64) for _ in range(25)]
    _sc = [0] * 25
    _x = _s1[:]
    P._keccak_f1600_rounds_1_to_23(_x, _sc)
    _y = _s1[:]
    P._keccak_f1600_rounds_1_to_23(_y, _sc)
    check("复用 scratch 不串味（连续 23 轮结果一致）", _x == _y)

    # ---------- 2. 单进程求解 ----------
    print("\n[2] 单进程求解")
    ch = make_easy_challenge(50000)
    t0 = time.monotonic()
    solved = P.solve(ch, "/api/v0/chat/completion", workers=1)
    check("单进程解出答案 0", solved.get("answer") == 0, str(solved))
    check("单进程耗时可接受", time.monotonic() - t0 < 5.0)

    # ---------- 3. 降级路径（本次事故的回归守护） ----------
    print("\n[3] 多进程不可用 → 必须降级，不得静默卡死")
    ch_big = make_easy_challenge(50000)   # difficulty ≥ 20000 才会走多进程

    original = P._parallel_search

    def boom(*_args, **_kwargs):
        raise P.PowParallelUnavailable("测试注入：模拟 spawn 池不可用")

    P._parallel_search = boom  # type: ignore[assignment]
    try:
        t0 = time.monotonic()
        degraded = P.solve(ch_big, "/api/v0/chat/completion", workers=4)
        elapsed = time.monotonic() - t0
        check("多进程抛 PowParallelUnavailable 后仍解出答案",
              degraded.get("answer") == 0, str(degraded))
        check("降级是立即的（没有白等）", elapsed < 5.0, f"{elapsed:.2f}s")
    finally:
        P._parallel_search = original  # type: ignore[assignment]

    # ---------- 4. 阈值语义 ----------
    print("\n[4] 阈值语义")
    # limit < 20000 必须走单进程 —— 进程池的启动开销在小难度上得不偿失。
    called = {"parallel": False}

    def spy(*args, **kwargs):
        called["parallel"] = True
        return original(*args, **kwargs)

    P._parallel_search = spy  # type: ignore[assignment]
    try:
        small = P.solve(make_easy_challenge(15000), "/api/v0/chat/completion", workers=4)
        check("小难度不走多进程", called["parallel"] is False)
        check("小难度解出答案", small.get("answer") == 0, str(small))
    finally:
        P._parallel_search = original  # type: ignore[assignment]

    # ---------- 5. 真实多进程路径 ----------
    print("\n[5] 真实多进程路径（可用则快，不可用则降级）")
    # 第 3 项注入失败时会把「本进程多进程不可用」记进缓存（那是刻意的：
    # 后续轮次不再白等超时），这里要清掉才能测到真实路径。
    P._parallel_disabled = False
    t0 = time.monotonic()
    real = P.solve(make_easy_challenge(50000), "/api/v0/chat/completion", workers=4)
    elapsed = time.monotonic() - t0
    check("多进程路径最终给出答案", real.get("answer") == 0, str(real))
    print(f"     耗时 {elapsed:.1f}s（明显 >10s 说明本环境 spawn 池不可用、走了降级）")

    # ---------- 6. 失败后不再重试 ----------
    print("\n[6] 失败缓存 · 后续轮次不再白等")
    P._parallel_disabled = True
    t0 = time.monotonic()
    cached = P.solve(make_easy_challenge(50000), "/api/v0/chat/completion", workers=4)
    elapsed = time.monotonic() - t0
    check("缓存生效后仍解出答案", cached.get("answer") == 0, str(cached))
    check("缓存生效后不再等超时", elapsed < 3.0, f"{elapsed:.2f}s")
    P._parallel_disabled = False

    # ---------- 7. 计量字段 + FIRST_COMPLETED 回归守护 ----------
    print("\n[7] 计量字段 · 以及「不得等满启动超时」的回归守护")
    # 答案固定在 nonce=5000（分片 1）：既覆盖**跨分片**搜索，又便宜。
    ch_meas = make_challenge_at(5000, 144000)

    t0 = time.monotonic()
    s_meas = P.solve(ch_meas, "/api/v0/chat/completion", workers=1)
    single_ms = (time.monotonic() - t0) * 1000.0
    s_stats = {k: v for k, v in s_meas.items() if k.startswith("_")}
    check("单进程解出 5000", s_meas.get("answer") == 5000, str(s_meas.get("answer")))
    check("单进程计量字段正确",
          s_meas.get("_mode") == "single" and s_meas.get("_workers") == 1
          and s_meas.get("_trials") == 5001, str(s_stats))

    P._parallel_disabled = False
    t0 = time.monotonic()
    m_meas = P.solve(ch_meas, "/api/v0/chat/completion", workers=4)
    multi_ms = (time.monotonic() - t0) * 1000.0
    m_stats = {k: v for k, v in m_meas.items() if k.startswith("_")}
    check("多进程解出 5000", m_meas.get("answer") == 5000, str(m_meas.get("answer")))
    check("多进程计量字段正确",
          m_meas.get("_mode") == "multi" and m_meas.get("_workers") == 4
          and m_meas.get("_trials") == 5001
          and "_pool_startup_ms" in m_meas, str(m_stats))

    # ⚠️ 这条是 2026-10-16 那个 bug 的**回归守护**，别删。
    #
    # `concurrent.futures.wait` 的默认 `return_when` 是 **`ALL_COMPLETED`**
    # （不是 `FIRST_COMPLETED`）。`_parallel_search` 里漏写这个参数时，
    # 那一行就从「等第一个分片」变成「等**全部**分片跑完」，被
    # `startup_timeout=10` 截断 ⇒ **每一轮恒定等满 10 秒**，与答案藏在
    # 区间哪个位置**完全无关**（答案是 0、第一个候选就命中，照样等满）。
    #
    # 实测：修前 10056ms，修后 ~700ms。阈值 6000ms 留了约 3 倍余量。
    check("多进程没有等满启动超时（FIRST_COMPLETED 回归守护）",
          multi_ms < 6000.0,
          f"{multi_ms:.0f}ms —— 疑似 pow.py 的 wait() 退回了 ALL_COMPLETED")
    print(f"     单进程 {single_ms:.0f}ms · 多进程 {multi_ms:.0f}ms")

    # ---------- 8. 协作式中断（T-18 后半） ----------
    #
    # 要守的四件事：
    #   a) 令牌不匹配时，分片必须**立刻**退出，而不是跑完剩下的 nonce；
    #   b) 令牌匹配时，必须照常搜完 —— 不能因为「有 stop」就乱停；
    #   c) 段打不开时按「已停止」处理，**不能抛异常**；
    #   d) 段**不得泄漏** —— 曾经每轮新建 + unlink，实测每轮漏一个。
    #
    # 反例代价很高：若检查点没生效，命中后其余 7 个分片会继续跑满 4096 个
    # nonce（约 900ms），下一轮首分片和它们抢 CPU —— 这正是 degraded 的成因。
    print("\n[8] 协作式中断 · 令牌不匹配即退出")

    # 一个**不可能命中**的目标：随机 32 字节，整个区间都搜不到。
    _dead = os.urandom(32)
    _prefix = b"stop_test_salt_1789148689858_"
    _big_job = (0, 4096, _prefix, _dead)

    # 基线：不传 stop，跑完整个 4096 区间。
    t0 = time.monotonic()
    _none = P._search_chunk(_big_job)
    full_ms = (time.monotonic() - t0) * 1000.0
    check("无 stop 时跑完整个区间并返回 None", _none is None, str(_none))

    _name = f"gad-test-stop-{uuid.uuid4().hex}"
    _seg = _shm_mod.SharedMemory(create=True, size=4, name=_name)
    try:
        # (a) 段里是哨兵（父进程命中）—— 与任何代数都不等，必须立刻退出。
        #
        # ⚠️ 这里和 pow.py 里一样必须显式切片：Windows 上 `size=4` 实际映射
        # 4096 字节，`buf[:] = 4 字节` 会抛 ValueError。
        _seg.buf[: P._STOP_TOKEN_BYTES] = P._STOP_SENTINEL
        _tok = (5).to_bytes(P._STOP_TOKEN_BYTES, "little")
        t0 = time.monotonic()
        _stopped = P._search_chunk((*_big_job, (_name, _tok)))
        stop_ms = (time.monotonic() - t0) * 1000.0
        check("令牌不匹配时立刻返回 None", _stopped is None, str(_stopped))
        # 检查粒度 256 个 nonce ≈ 56ms，留足余量取 1/3 的完整耗时。
        check("令牌不匹配时没有跑完整个区间", stop_ms < full_ms / 3,
              f"停止路径 {stop_ms:.0f}ms vs 完整 {full_ms:.0f}ms")

        # (b) 段里正是自己的代数 —— 必须照常搜完。
        _seg.buf[: P._STOP_TOKEN_BYTES] = _tok
        _notyet = P._search_chunk((*_big_job, (_name, _tok)))
        check("令牌匹配时照常搜完并返回 None", _notyet is None, str(_notyet))
    finally:
        _seg.close()
        _seg.unlink()

    # (c) 段已消失（父进程退出）：等价于「已停止」，不是错误。
    _gone = f"gad-test-gone-{uuid.uuid4().hex}"
    try:
        _after = P._search_chunk((*_big_job, (_gone, (1).to_bytes(4, "little"))))
        check("段打不开时按已停止处理（返回 None 而非抛错）", _after is None, str(_after))
    except FileNotFoundError:
        check("段打不开时按已停止处理（返回 None 而非抛错）", False,
              "抛了 FileNotFoundError")

    # (d) 单例段：连续多轮**不得**新建第二个段。
    #
    # 这里包一层 `SharedMemory.__init__` 来数创建次数 —— Windows 上没有
    # /dev/shm 可数，`resource_tracker._registry` 在 3.13 也已移除。
    _created: list[str] = []
    _real_init = _shm_mod.SharedMemory.__init__

    def _spy_init(self, name=None, create=False, size=0, *, track=True):
        _real_init(self, name=name, create=create, size=size, track=track)
        if create and name and str(name).startswith("gad-pow-stop-"):
            _created.append(name)

    _shm_mod.SharedMemory.__init__ = _spy_init  # type: ignore[method-assign]
    try:
        P._stop_shm = None  # 从干净状态起算
        P._stop_gen = 0
        P._parallel_disabled = False
        for _ in range(3):
            P.solve(make_challenge_at(0, 144000), "/api/v0/chat/completion", workers=4)
    finally:
        _shm_mod.SharedMemory.__init__ = _real_init  # type: ignore[method-assign]

    check("停止段是进程内单例（3 轮只创建 1 个）", len(_created) == 1, str(_created))
    check("代数递增（3 轮后 _stop_gen == 3）", P._stop_gen == 3, str(P._stop_gen))

    # 端到端：答案落在第一个分片，其余分片应被提前掐掉。
    # 判据只看「结果正确」—— 速度差异在基准里量，这里不做脆弱的阈值断言。
    P._parallel_disabled = False
    _fast = P.solve(make_challenge_at(0, 144000), "/api/v0/chat/completion", workers=4)
    check("协作式中断下多进程仍给出正确答案",
          _fast.get("answer") == 0 and _fast.get("_mode") == "multi", str(_fast.get("_mode")))

    print("\n" + "-" * 70)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("  失败项：")
        for name in FAIL:
            print(f"    - {name}")
    print("-" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
