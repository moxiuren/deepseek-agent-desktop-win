"""DeepSeekHashV1 PoW 求解器（Python 移植 + 多进程加速）。

算法（对照 `deepseek-mac/windows/DeepSeekPowSolver.cs`，已实测通过）
----------------------------------------------------------------

  * Keccak-f[1600] 海绵结构
  * **只跑 round 1..23（跳过第 0 轮）** —— 这是与标准 SHA3-256 的**唯一区别**，
    也是通用库算不出正确答案的原因（没有任何现成库会实现这个变体）
  * rate = 136 字节（capacity = 64 字节 = 256 bit 输出）
  * padding：`0x06` 前缀 + `0x80` 收尾（标准 SHA3-256）
  * 输入：`{salt}_{expire_at}_{nonce}`（UTF-8，nonce 为十进制字符串）
  * 判据：`hash(输入) == challenge`（**32 字节完全相等**，非前缀匹配）
  * nonce 范围：`[0, difficulty)`

性能
----

纯 Python 单核每轮 hash 约 0.35ms，`difficulty=144000` 量级 ⇒ 单核约 50~80 秒。
本实现两条加速路径：

  1. **`struct` 批量解包/打包** —— 替代逐 lane 切片 `int.from_bytes`（C 层循环）
  2. **多进程分片搜索** —— 按核数切分 nonce 区间，命中即全局终止

实测（8 核，`difficulty=144000`）：单核 ~52s → 多进程 **~7s**。

> 注意：`workers>1` 走 `multiprocessing` 的 **spawn** 语义（Windows 默认）。
> 调用方**必须**把入口代码放在 `if __name__ == "__main__":` 之下，否则子进程
> 会重复导入并递归创建进程池。
>
> ★ **2026-10-15 修正**：这里原先还有一句「库内部调用不受影响」—— **那是错的**。
> 实测：当本模块跑在**另一个进程的子进程里**时（`python -m gad_engine` ——
> 外壳与冒烟脚本都是这么起引擎的），spawn 池会**静默卡死**：池建起来了、
> 任务发出去了、管理线程都健在，但结果永远不回来，既不抛异常也不打日志。
> 现在 `_parallel_search` 带启动时限并抛 `PowParallelUnavailable`，
> 由 `solve` 降级到单进程。**多进程是加速手段，不该变成单点故障。**

正确性由 `selftest()` 用 C# 侧的金标准向量守护（见文件末）。
"""

from __future__ import annotations

import contextlib
import os
import struct
import time
import uuid
from dataclasses import dataclass
from multiprocessing import shared_memory as _shm

RATE = 136
RATE_LANES = RATE // 8  # 17

# ---------- 协作式中断的检查粒度（T-18，2026-10-18） ----------
#
# `_search_chunk` 每 `_STOP_CHECK_EVERY` 个 nonce 查一次 `stop.is_set()`。
#
# 取 256 的依据：
#   * 一次哈希 ~218µs，一次跨进程 `Event.is_set()` 是共享内存读，便宜得多
#     但仍非零 —— 每个 nonce 都查会白白吃掉热循环。
#   * 256 个 nonce ≈ 56ms，是「命中后最多还要多跑多久」的上界。
#     对照要解决的问题（残留让下一轮慢 1.96x，绝对值 ~1.4 秒），
#     56ms 的延迟可以忽略；而检查频率从 4096 次降到 16 次。
#   * 必须是 **2 的幂** —— 热循环里用 `nonce & _STOP_CHECK_MASK` 判断，
#     位与比取模快，且不依赖 nonce 的绝对值（分片起点任意）。
_STOP_CHECK_EVERY = 256
_STOP_CHECK_MASK = _STOP_CHECK_EVERY - 1

# 停止令牌的字节宽度。**必须是显式切片宽度，不能靠 `buf[:]`** ——
# 见 `_stop_segment` 的注释：Windows 上 `SharedMemory(size=4)` 实际映射
# 4096 字节（页对齐），`buf[:]` 的长度是 4096 而不是 4。
_STOP_TOKEN_BYTES = 4

# 命中时写入共享内存的哨兵值。任何有效代数都不会等于它 ——
# 所以「哨兵」对**所有**在飞分片都意味着「别找了，已经有人给出答案」。
_STOP_SENTINEL = b"\xff" * _STOP_TOKEN_BYTES

# 进程内单例的停止段。**不每轮新建** —— 见 `_stop_segment` 的注释。
_stop_shm = None
_stop_gen = 0

# Keccak round constants（标准 24 轮表；本算法只用到 1..23）
RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# rho 旋转偏移，ROT[x][y]
ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

MASK64 = (1 << 64) - 1

_UNPACK17 = struct.Struct("<17Q").unpack
_UNPACK4 = struct.Struct("<4Q").unpack
_PACK4 = struct.Struct("<4Q").pack

# 预计算扁平索引表 —— 把嵌套循环里的 `(2*x+3*y)%5` 与取模算术搬到模块加载期。
# 实测这一步让 23 轮本体从 ~682µs 降到 ~425µs（1.6x）。
#   _PIR: (源索引, 旋转量, 目标索引)  —— rho + pi
#   _CHI: (目标索引, 源1, 源2)        —— chi
_PIR: tuple[tuple[int, int, int], ...] = tuple(
    (x + 5 * y, ROT[x][y], y + 5 * ((2 * x + 3 * y) % 5))
    for x in range(5)
    for y in range(5)
)
_CHI: tuple[tuple[int, int, int], ...] = tuple(
    (y + x, y + (x + 1) % 5, y + (x + 2) % 5)
    for y in range(0, 25, 5)
    for x in range(5)
)

# ---- 展开用的常量表（模块加载期算好，循环里直接用字面量语义） ----
# rho+pi：25 条 (src, rot, dst)。rot==0 的那条（src=dst=0）单独处理，
# 因为 `v >> 64` 在 Python 里是合法但无意义的（结果是 0），而 `v << 0 | v >> 64`
# 恰好等于 v —— 数学上没错，但多两次移位。展开时直接写成赋值。
_PIR_ROT = tuple((src, rot, dst) for src, rot, dst in _PIR if rot)
_PIR_SAME = tuple((src, dst) for src, rot, dst in _PIR if not rot)

# 展开后的源码由 `_build_round_body()` 生成，见下。
def _build_round_body() -> str:
    """生成 round 本体的展开源码 —— **只在模块加载期跑一次**。

    为什么用代码生成而不是手写 25+25 行：手写容易抄错一个下标，
    而错一个下标不会报错，只会算出错误的哈希 —— 这种 bug 靠读代码
    几乎发现不了。生成器从 `_PIR` / `_CHI` 推导，与查表版本**同源**，
    不存在「展开版和查表版对不上」的可能。

    生成出来的代码做两件事：
      1. rho+pi 25 条全部写成 `b[dst] = ((a[src] << rot) | (a[src] >> (64-rot))) & mask`
      2. chi 25 条全部写成 `a[dst] = b[dst] ^ ((~b[s1] & mask) & b[s2])`
    """
    lines = []
    for src, dst in _PIR_SAME:
        lines.append(f"        b[{dst}] = a[{src}]")
    for src, rot, dst in _PIR_ROT:
        lines.append(
            f"        _v = a[{src}]; b[{dst}] = ((_v << {rot}) | (_v >> {64 - rot})) & mask"
        )
    for dst, s1, s2 in _CHI:
        lines.append(f"        a[{dst}] = b[{dst}] ^ ((~b[{s1}] & mask) & b[{s2}])")
    return "\n".join(lines)


#: 展开后的 round 本体（rho+pi + chi）。`exec` 只在这里执行一次。
_ROUND_BODY = _build_round_body()


def _make_round_fn():
    """把生成的展开体编成一个真函数，模块加载期执行一次。

    为什么不直接在 `_keccak_f1600_rounds_1_to_23` 里 `exec`：那会让每次
    哈希都付一次编译成本。这里只付一次，之后是普通函数调用。

    签名 `(a, b, mask)` —— 三个参数都靠位置传，`mask` 传进来而不是
    闭包引用，是为了让函数体里 `mask` 是局部变量（LOAD_FAST 比 LOAD_GLOBAL 快）。
    """
    src = "def _round(a, b, mask):\n" + _ROUND_BODY + "\n"
    ns: dict = {}
    exec(compile(src, "<pow._round>", "exec"), ns)
    return ns["_round"]


_round = _make_round_fn()


def _keccak_f1600_rounds_1_to_23(a: list[int], scratch: list[int] | None = None) -> None:
    """就地跑 round 1..23。a 是 25 个 uint64 的 state。

    索引一律用 `x + 5*y`（与 C# 版一致）。

    **`scratch` 是 rho+pi 的临时缓冲**（长度 25，内容会被覆写）。
    单次调用时可以省略，函数会自己建一个；但热循环里必须传 ——
    原实现每轮 `b = [0] * 25`，23 轮就是 23 次分配，而每次求解要跑
    十几万个 nonce。2026-10-18 基准：这一项省掉约 8% 的单次哈希成本。

    theta 的 25 次 XOR 用显式展开而不是 `for y / for i` 双循环：
    后者每轮多 25 次索引 + 循环管理，23 轮累计 575 次，是纯开销。
    """
    mask = MASK64
    b = scratch if scratch is not None else [0] * 25
    for rnd in range(1, 24):
        # ---- theta ----
        c0 = a[0] ^ a[5] ^ a[10] ^ a[15] ^ a[20]
        c1 = a[1] ^ a[6] ^ a[11] ^ a[16] ^ a[21]
        c2 = a[2] ^ a[7] ^ a[12] ^ a[17] ^ a[22]
        c3 = a[3] ^ a[8] ^ a[13] ^ a[18] ^ a[23]
        c4 = a[4] ^ a[9] ^ a[14] ^ a[19] ^ a[24]

        d0 = c4 ^ (((c1 << 1) | (c1 >> 63)) & mask)
        d1 = c0 ^ (((c2 << 1) | (c2 >> 63)) & mask)
        d2 = c1 ^ (((c3 << 1) | (c3 >> 63)) & mask)
        d3 = c2 ^ (((c4 << 1) | (c4 >> 63)) & mask)
        d4 = c3 ^ (((c0 << 1) | (c0 >> 63)) & mask)

        # 状态按 a[x + 5y] 线性排列：每一行（固定 y）的 5 个元素依次 ^= d0..d4。
        a[0] ^= d0
        a[1] ^= d1
        a[2] ^= d2
        a[3] ^= d3
        a[4] ^= d4
        a[5] ^= d0
        a[6] ^= d1
        a[7] ^= d2
        a[8] ^= d3
        a[9] ^= d4
        a[10] ^= d0
        a[11] ^= d1
        a[12] ^= d2
        a[13] ^= d3
        a[14] ^= d4
        a[15] ^= d0
        a[16] ^= d1
        a[17] ^= d2
        a[18] ^= d3
        a[19] ^= d4
        a[20] ^= d0
        a[21] ^= d1
        a[22] ^= d2
        a[23] ^= d3
        a[24] ^= d4

        # ---- rho + pi + chi（展开体，见 `_build_round_body`） ----
        _round(a, b, mask)

        # ---- iota ----
        a[0] ^= RC[rnd]


def _absorb(block: bytes) -> list[int]:
    """把 136 字节块按 little-endian 载入 25 lane 的 state（后 8 lane 为 0）。

    用 `struct` 一次解出 17 个 uint64 —— 比逐 lane 切片 `int.from_bytes` 快数倍。

    **热循环不要用这个**（它每次分配一个新 list）；用 `_absorb_into`。
    保留它是为了 `ds_hash` 这种一次性路径的可读性，以及给测试当参照实现 ——
    两条路径必须给出逐字节相同的结果，这由 `tests/test_pow.py` 守着。
    """
    return list(_UNPACK17(block)) + [0] * 8


def _absorb_into(block, state: list[int]) -> None:
    """同 `_absorb`，但**写进调用方给的 state**，不分配。

    `block` 可以是 `bytes` / `bytearray` / `memoryview` —— `struct.unpack`
    走 buffer protocol，不需要先转成 `bytes`。原实现在热循环里写
    `_absorb(bytes(base))`，那个 `bytes(base)` 每个 nonce 都复制 136 字节，
    是纯粹的白费。

    后 8 lane 必须显式清零：state 是复用的，上一轮的残值不清就会污染本轮。
    """
    state[:17] = _UNPACK17(block)
    state[17] = state[18] = state[19] = state[20] = 0
    state[21] = state[22] = state[23] = state[24] = 0


def _squeeze(state: list[int]) -> bytes:
    """取 state 前 4 lane（32 字节）的 little-endian 字节。"""
    return _PACK4(state[0], state[1], state[2], state[3])


def ds_hash(data: bytes) -> bytes:
    """DeepSeekHashV1 单块哈希（输入须 < 136 字节）。"""
    if len(data) >= RATE:
        raise ValueError(f"输入过长（{len(data)} >= {RATE}），本实现只支持单块")
    block = bytearray(RATE)
    block[: len(data)] = data
    block[len(data)] |= 0x06
    block[RATE - 1] |= 0x80
    state = _absorb(bytes(block))
    _keccak_f1600_rounds_1_to_23(state)
    return _squeeze(state)


def ds_hash_hex(data: bytes) -> str:
    return ds_hash(data).hex()


@dataclass
class Challenge:
    algorithm: str
    challenge: str
    salt: str
    difficulty: int
    signature: str
    expire_at: int

    @classmethod
    def from_api(cls, biz: dict) -> Challenge:
        expire = biz.get("expire_at") or biz.get("expireAt")
        if expire is None:
            raise ValueError("challenge 缺少 expire_at")
        return cls(
            algorithm=biz.get("algorithm", ""),
            challenge=biz.get("challenge", ""),
            salt=biz.get("salt", ""),
            difficulty=int(biz.get("difficulty", 0)),
            signature=biz.get("signature", ""),
            expire_at=int(expire),
        )


# ---------- 搜索内核（模块级，供 multiprocessing spawn 复用） ----------


def _search_chunk(
    job: tuple[int, int, bytes, bytes]
    | tuple[int, int, bytes, bytes, tuple[str, bytes] | None],
) -> int | None:
    """在 `[start, end)` 内搜索 nonce，命中返回 nonce，否则 None。

    **必须是模块级函数** —— spawn 模式下子进程靠 pickle 引用它。

    ---- 协作式中断（2026-10-18，T-18 后半）----

    `job` 的第 5 项 `stop` 是可选的 `(段名, 代数令牌)`。子进程自己
    `SharedMemory(name=段名)` 打开它，每 `_STOP_CHECK_EVERY` 个 nonce 读一次
    那 4 字节：**不等于自己的代数令牌**就说明本轮已被叫停，立刻返回 None。

    父进程命中时写入 `_STOP_SENTINEL`（全 0xFF），它对**所有**代数都不相等，
    所以每一代分片都会退出。新一轮开始时把段写回自己的代数，分片又能照常搜。

    为什么挂在 `job` 里而不是当第二个参数：`executor.submit(fn, *args)` 的
    每个 `arg` 都要单独 pickle 一轮，而 `job` 本来就是一次调用里唯一的载荷。
    并进元组既少一次序列化，也让「4 元组（单进程）/ 5 元组（多进程）」的
    差异收在一个地方 —— 单进程路径天然没有协作对象，不需要传 None。

    ### 为什么不用 `multiprocessing.Event`

    这是实测否掉的第一直觉（2026-10-18，`/tmp/probe_evt.py`）：

        RuntimeError: Condition objects should only be shared between processes
                      through inheritance

    `Event` 底层是 `SemLock` + `Condition`，**只能靠进程继承共享**；而
    `ProcessPoolExecutor.submit()` 的参数是**经队列 pickle 过去的**，
    不是继承。spawn 下两者根本不是同一条路。共享内存只传一个名字字符串，
    子进程打开即可 —— 实测两个 worker 在 389ms 内都看到了置位。

    ### 为什么需要它

    命中后其余在飞分片（滚动提交下最多 `workers - 1` 个）本来要跑完整个
    chunk（4096 × 218µs ≈ 867ms）才退出。那段时间下一轮的首个分片要和它们
    抢 CPU。实测（`diag_pow_residue.py`）残留会让下一轮慢 1.96x。

    ### 检查粒度

    每 `_STOP_CHECK_EVERY` 个 nonce 查一次。那是一次共享内存读，
    比一次哈希（~218µs）便宜得多，但仍不是零。256 意味着**最多多跑
    256 个 nonce（约 56ms）**才停，这个延迟远小于它换来的收益。

    ### 段打不开时

    单例段活到父进程退出，正常情况下不会消失。但父进程若已退出
    （引擎被强杀后残留的 worker），打开会 `FileNotFoundError` ——
    那等价于「没人等这个结果了」，直接 `return None`，不当异常处理。

    ⚠️ 单进程路径（`workers=1` 或小 difficulty）传的是 **4 元组**，没有
    `stop` —— 那里没有可协作的对象，热循环必须**完全不受影响**。

    ---- 热循环优化（2026-10-18，T-15/T-16）----

    这个循环是 PoW 的全部成本所在：`difficulty=144000` 意味着每个分片要跑
    上万次，8 核并行下整轮仍有 ~20 万次哈希。原实现每个 nonce 分配
    **四个**临时对象（`tail` bytes、`bytes(base)` 的 136 字节拷贝、
    `_absorb` 的 state list、`_keccak` 里的 scratch list），其中三个可以完全消掉：

    | 原写法 | 现写法 | 省掉 |
    |---|---|---|
    | `_absorb(bytes(base))` | `_absorb_into(base, state)` | 每次 136 字节拷贝 + 25 元素 list |
    | `_keccak(...)` 内 `b = [0]*25` | 传入复用的 `scratch` | 每次 23 个 list |
    | `_squeeze(state) == target` | 逐 lane 整数比较 | 每次 32 字节 pack |

    `tail = str(nonce).encode()` 那一次分配**保留**：它是 nonce 十进制表示，
    长度随位数变化，没有不分配的写法。这是本循环剩下的唯一分配。

    正确性：逐 lane 整数比较与 `_squeeze(state) == target` **完全等价** ——
    little-endian 下 4 个 uint64 相等 ⟺ 32 字节相等。`_absorb_into` 与
    `_absorb` 同样等价，两条路径由 `tests/test_pow.py` 对拍守护。
    """
    # 4 元组 = 单进程（无协作对象）；5 元组 = 多进程（带 (段名, 代数令牌)）。
    # 用长度判断而不是 `job[4] if len(job) > 4 else None` —— 后者每次调用
    # 都要建一次切片/索引，长度解包是 C 层一次完成。
    if len(job) == 5:
        start, end, prefix, target, stop = job
    else:
        start, end, prefix, target = job
        stop = None
    plen = len(prefix)

    base = bytearray(RATE)
    base[:plen] = prefix
    base[RATE - 1] = 0x80
    prev_end = plen  # 上一轮的 0x06 pad 位

    # 目标预先解成 4 个 uint64 —— 循环里不再 pack/比较 bytes。
    t0, t1, t2, t3 = _UNPACK4(target)

    # 两个跨 nonce 复用的缓冲。state 的 25 个 lane 每轮由 `_absorb_into`
    # 整体覆写（含后 8 lane 清零），scratch 由 `_keccak` 每轮 rho+pi 整体覆写，
    # 都不需要在这里初始化内容 —— 只需要长度对。
    state = [0] * 25
    scratch = [0] * 25

    # 协作式中断：子进程自己打开父进程创建的共享内存段。
    #
    # `FileNotFoundError` 的含义不是「出错」—— 父进程可能已退出（引擎被强杀
    # 后残留的 worker），没人再等这个结果了，直接按「已停止」处理。
    stop_shm = None
    stop_buf = None
    token = None
    if stop is not None:
        stop_name, token = stop
        try:
            stop_shm = _shm.SharedMemory(name=stop_name)
            stop_buf = stop_shm.buf
        except FileNotFoundError:
            return None

    try:
        for nonce in range(start, end):
            # 检查点：`stop_buf is None` 时只是一次 LOAD_FAST + 条件跳转，
            # 相对 218µs 的一次哈希可忽略；非 None 时每 256 个 nonce
            # 才真的读一次共享内存。
            #
            # 判据是「**不等于自己的代数**」而不是「非零」：
            #   * 父进程命中 → 写入 `_STOP_SENTINEL`（全 0xFF），不等于任何代数；
            #   * 下一轮开始 → 写入新一代的代数，上一轮的令牌自然失效。
            # 用 4 字节整体比较，避免逐字节读再拼。
            # ⚠️ 切片宽度必须写死 `:_STOP_TOKEN_BYTES`。Windows 上段被页对齐到
            # 4096 字节，`stop_buf[:]` 拿到的是 4096 字节，永远不等于 4 字节的
            # 令牌 —— 那会让**每一个**分片在第一个检查点就退出，求解直接失败。
            if (
                stop_buf is not None
                and (nonce & _STOP_CHECK_MASK) == 0
                and stop_buf[:_STOP_TOKEN_BYTES] != token
            ):
                return None

            tail = str(nonce).encode("ascii")
            pos = plen + len(tail)
            # 位数变化时清掉上一轮遗留的 pad 位（否则会污染本轮的块）
            if prev_end != pos:
                base[prev_end] = 0x00
            base[plen:pos] = tail
            base[pos] = 0x06

            _absorb_into(base, state)
            _keccak_f1600_rounds_1_to_23(state, scratch)
            # 短路求值：绝大多数 nonce 在第一个比较就出局。
            if state[0] == t0 and state[1] == t1 and state[2] == t2 and state[3] == t3:
                return nonce

            prev_end = pos

        return None
    finally:
        # 只 close，**不 unlink** —— 段是进程内单例，生命周期归父进程。
        # 子进程若 unlink，下一次分片就再也打不开它了。
        if stop_shm is not None:
            stop_shm.close()


class PowParallelUnavailable(RuntimeError):
    """多进程池没能给出任何结果 —— 调用方应当降级到单进程。

    这个异常存在的理由是一次实测事故（2026-10-15）：引擎以
    `python -m gad_engine` 跑在**另一个进程的子进程里**（外壳 / 冒烟脚本都是
    这么起它的）时，`multiprocessing` 的 spawn 池会**静默卡死** ——
    池对象建起来了、任务也发出去了、管理线程（`_handle_workers` /
    `_handle_tasks` / `_handle_results`）都健在，但结果**永远不回来**。
    既不抛异常、也不写日志，从外面看就是「消息发出去了，一直没有回复」。

    这类故障比报错恶劣得多，所以这里宁可**主动放弃多进程**（10 秒没有
    任何分片返回就认定它不可用），降级到单进程慢跑。

    ★ **2026-10-18 修正：这个异常现在有两种成因，不能一概而论。**

    | 成因 | 判定 | 正确处置 |
    |---|---|---|
    | 池真的坏了（spawn 卡死、池被 shutdown） | `transient=False` | 永久降级，不再重试 |
    | 池是好的，只是被另一个 `solve()` 抢占了 | `transient=True` | **本次**降级，**不**置全局标志 |

    混为一谈的代价实测过：后台预取线程与主路径共用同一个 `ProcessPoolExecutor`，
    各自 submit 全量分片，主路径等「首个分片返回」超时 → 被当成「池坏了」→
    `_parallel_disabled=True` → **本进程后续所有轮次都单进程**，
    单次求解从 5 秒变成 40 秒。
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        #: True = 池是好的，只是这次没抢到。调用方**不应**置 `_parallel_disabled`。
        self.transient = transient


#: 多进程池在**本进程**里是否已被证明不可用。
#:
#: 一旦确认就**不再重试** —— 否则用户每一轮对话都要白等一次启动超时
#: （10 秒 × 每轮，比单进程慢跑还糟）。引擎的每一轮对话都跑在同一个进程里，
#: 所以这个代价只付一次。
#:
#: ⚠️ 只有 `PowParallelUnavailable.transient is False` 才允许置位。
#: 并发争用（`transient=True`）只是这一次没抢到，不代表池坏了。
_parallel_disabled = False

#: 串行化所有 `solve()` 调用 —— **这是并发争用的根治手段**。
#:
#: 为什么需要：`_get_executor()` 返回的是**单例池**。后台预取线程
#: （`PowCache._loop` → `_prefetch_one` → `solve`）与主路径
#: （`deepseek_rpc._stream_completion` → `solve`）会同时进来，
#: 两边各 submit 36 个分片，池的 8 个 worker 被瓜分，
#: 主路径的 `startup_timeout` 等不到自己的首个分片。
#:
#: 实测（`tools/diag_pow_concurrency.py`）：串行两次都 `multi`；
#: 两线程并发时，一个 `multi` 一个 `degraded`，且事后 `_parallel_disabled=True`。
#:
#: 用锁而不是给每个调用方独立池：独立池会**成倍放大进程数**
#: （8 worker × 2 调用方 = 16 进程），在 16 核机器上直接把 CPU 打满，
#: 反而是更强的机器特征。串行化后总耗时仍是 5–10 秒量级，
#: 远好于降级后的 40 秒。
#:
#: 注意：锁只包住**求解**这段纯本地计算，不包网络。预取线程持有它时
#: 主路径会等，但主路径本来也要等 PoW 解完才能发请求，语义上没有变差。
_solve_lock = None  # threading.Lock，惰性创建（模块导入期不建锁）


def _get_solve_lock():
    """惰性创建 `_solve_lock`。

    为什么不在模块级直接 `threading.Lock()`：`pow.py` 会被 `multiprocessing`
    的 spawn 子进程重新导入，模块级建锁在 fork/spawn 边界上有历史坑
    （`_executor_lock` 出于同样理由也是惰性的）。
    """
    global _solve_lock
    import threading

    if _solve_lock is None:
        _solve_lock = threading.Lock()
    return _solve_lock


#: 连续多少次「首轮超时」才认定为池真的坏了。
#:
#: 为什么要计数而不是一次就降级：`transient=True` 的超时有两种成因，
#: 单次无法区分。真坏的池**每次**都超时，争用的池只是偶尔撞上。
#: 取 3 —— 一次 10 秒超时已经足够痛，连撞三次（30 秒白等）还不通，
#: 那多半不是运气问题。
#:
#: 计数只在**超时**时累加；任何一次成功立刻清零（见 `_reset_timeout_streak`）。
#: 这保证了「偶发争用 + 偶发争用 + 成功」不会被误判成坏池。
_TIMEOUT_STREAK_LIMIT = 3

#: 当前连续超时次数。只在持有 `_solve_lock` 时读写，所以不需要额外加锁。
_timeout_streak = 0


def _bump_timeout_streak() -> int:
    """记录一次超时，返回累加后的连续超时次数。"""
    global _timeout_streak
    _timeout_streak += 1
    return _timeout_streak


def _reset_timeout_streak() -> None:
    """多进程成功一次 —— 池是好的，清空连续超时计数。"""
    global _timeout_streak
    _timeout_streak = 0


def _disable_parallel(reason: str) -> None:
    """确认池不可用：永久降级到单进程，并丢弃坏池。

    这是**唯一**允许置 `_parallel_disabled` 的入口 —— 集中到一处，
    免得日后又在某个分支里顺手写 `_parallel_disabled = True`，
    把「争用」重新混成「故障」。
    """
    global _parallel_disabled
    _parallel_disabled = True
    # 坏池不能留 —— 常驻池一旦僵死，留着它只会让下次 submit 继续挂。
    _drop_executor()


#: 常驻进程池（2026-10-17 新增）。
#:
#: 背景：`pow_stats` 实测（三轮真实发送）显示每轮 `pool_startup_ms` = **3.8–6.1 秒**，
#: 而整轮 `pow_ms` 只有 7.8–13.4 秒 —— **近一半时间花在重建进程池上**。
#: 这是纯工程浪费，与安全无关，却是最容易被区分的行为特征：真人按下回车，
#: 服务端毫秒级回字；我们是静默 8–13 秒才出第一个 token。
#:
#: 设计要点：
#:   * **单例复用**，跨轮次不销毁 ⇒ 省掉 spawn 成本
#:   * 命中后只 `cancel()` **未开始**的分片；已在跑的 worker 最多跑完当前
#:     chunk（`chunk_size=4096` × ~0.35ms ≈ 1.4s）后自然空闲
#:   * ⚠️ 常驻池**绝不能调 `shutdown()`** —— 一旦 shutdown 就不可再 submit，
#:     下一轮会直接抛 `RuntimeError: cannot schedule new futures after shutdown`
#:   * 池一旦被判定不可用（`_parallel_disabled`）或 submit 抛异常，**丢弃重建**，
#:     不做「坏池永久复用」
#:   * `GAD_POW_POOL=0` 退回「每轮新建」—— 逃生舱，思路与 `GAD_POW_WORKERS` 一致
_executor = None
_executor_workers = 0
_executor_lock = None  # threading.Lock，惰性创建（模块导入期不建锁）


def _pool_enabled() -> bool:
    """常驻池开关。默认开启；`GAD_POW_POOL=0` 退回每轮新建。"""
    return os.environ.get("GAD_POW_POOL", "1").strip() != "0"


def _drop_executor() -> None:
    """丢弃当前常驻池（不阻塞等待 worker 收尾）。"""
    global _executor, _executor_workers
    ex = _executor
    _executor = None
    _executor_workers = 0
    if ex is not None:
        with contextlib.suppress(Exception):
            ex.shutdown(wait=False, cancel_futures=True)


def _get_executor(workers: int):
    """取常驻池；worker 数变了就重建。

    返回 `(executor, created)` —— `created=True` 表示这一轮**新建**了池
    （所以这一轮的 `pool_startup_ms` 里含真实 spawn 成本），`False` 表示复用了
    上一轮的池（此时 `pool_startup_ms` 只剩「首个分片的计算时间」）。
    """
    global _executor, _executor_workers, _executor_lock
    import threading

    if _executor_lock is None:
        _executor_lock = threading.Lock()
    with _executor_lock:
        if _executor is not None and _executor_workers == workers:
            return _executor, False
        _drop_executor()
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        ctx = mp.get_context("spawn")
        _executor = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
        _executor_workers = workers
        return _executor, True


def _stop_segment() -> tuple[str, bytes]:
    """取进程内**单例**的停止段，并开一个新的「代数」。

    返回 `(段名, 本轮的代数令牌)`。每调用一次代数 +1。

    ## 为什么是单例，而不是每轮新建一个段

    最初实现是每轮 `SharedMemory(create=True, name=f"...{uuid}")`，命中后
    `unlink()`。**实测泄漏**（2026-10-18，`/tmp/probe_leak2.py`）：

        6 轮求解 → 6 个段仍可被重新打开（即从未真正释放）

    根因在 Windows 的语义：子进程的映射还挂着时 `unlink()` 抛
    `PermissionError`，而那段代码用 `contextlib.suppress(Exception)` 吞掉了它。
    换成「进程内单例」后，段只创建一次、活到进程退出，**根本没有 unlink 时机**，
    泄漏从结构上消失。

    ## 代数令牌解决「跨轮次污染」

    单例段只有 4 字节，内容是**本轮的代数**（1、2、3……）。分片带着自己
    那一轮的代数进来，读到的值 **不等于** 自己的代数就说明「我这一轮已经
    被叫停了」。命中时父进程写入 `_STOP_SENTINEL`。

    这样，上一轮命中留下的哨兵**不会**让下一轮的第一个分片立刻退出 ——
    新一轮会把代数写回去（从哨兵变成具体数字），分片读到自己的代数就照常搜。
    这正是「每轮新建段」想达到的效果，但不需要反复创建/删除。

    ⚠️ 换代必须在**提交任何分片之前**完成，否则分片会拿着上一轮的令牌。
    """
    global _stop_shm, _stop_gen
    if _stop_shm is None:
        name = f"gad-pow-stop-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        _stop_shm = _shm.SharedMemory(create=True, size=4, name=name)
    _stop_gen += 1
    token = _stop_gen.to_bytes(_STOP_TOKEN_BYTES, "little")
    # ⚠️ 必须显式切片：Windows 上 `size=4` 实际映射 4096 字节（页对齐），
    # `buf[:] = token` 会因为长度不符抛 `ValueError: memoryview assignment:
    # lvalue and rvalue have different structures`。
    _stop_shm.buf[:_STOP_TOKEN_BYTES] = token
    return _stop_shm.name, token


def _parallel_search(
    limit: int,
    prefix: bytes,
    target: bytes,
    workers: int,
    chunk_size: int,
    on_progress=None,
    startup_timeout: float = 10.0,
    stats: dict | None = None,
) -> int | None:
    """多进程分片搜索，命中即全局终止。

    第一个分片必须在 `startup_timeout` 秒内返回，否则抛
    `PowParallelUnavailable` 让上层降级 —— 见该异常的说明。

    `stats` 是**可选的出参**（只写不读）。成功时写入
    `pool_startup_ms` = 「建池 → 首个分片返回」的耗时。

    ⚠️ 它**不等于纯粹的进程启动开销**：里面还含**首个分片的计算时间**，
    上限是 `chunk_size × 单次哈希耗时`（默认 4096 × ~0.35ms ≈ 1.4s）。
    读这个数时要先把那一项减掉，剩下的才是 spawn 池真正的启动代价。

    之所以只在这里能记到它：这是整个流程里**唯一**能观测到「池到底要多久
    才开始出活」的位置 —— 出了 `_parallel_search` 就再也分不出来了。
    """
    import multiprocessing as mp
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

    if _parallel_disabled:
        raise PowParallelUnavailable("本进程此前已确认 spawn 池不可用，不再重试")

    ctx = mp.get_context("spawn")

    # ---------- 协作式中断的停止令牌（T-18 后半） ----------
    #
    # 取进程内**单例**段的当前代数。这一步必须在提交任何分片之前 ——
    # 分片带着令牌进来，读到段里的值不等于自己的令牌就退出。
    #
    # 为什么不用 `mp.Event`：见 `_search_chunk` 的 docstring，它只能靠继承共享，
    # 而 `submit()` 走的是 pickle 队列。
    # 为什么是单例而不是每轮新建：见 `_stop_segment` 的 docstring（每轮新建会泄漏）。
    stop_name, stop_token = _stop_segment()
    stop = (stop_name, stop_token)

    # ★ 分片用**迭代器**而不是列表 —— 滚动提交（T-18）按需取，不预先铺开。
    jobs = iter(
        (s, min(s + chunk_size, limit), prefix, target, stop)
        for s in range(0, limit, chunk_size)
    )
    done_chunks = 0
    started = time.monotonic()

    executor = None
    submitted: set = set()
    pending: set = set()
    try:
        persistent = _pool_enabled()
        if persistent:
            executor, pool_created = _get_executor(workers)
            if stats is not None:
                stats["pool_created"] = pool_created
        else:
            executor = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)

        # ---------- 滚动提交（T-18，2026-10-18） ----------
        #
        # ## 为什么不能一次 submit 全部分片
        #
        # `difficulty=144000` / `chunk_size=4096` ⇒ **36 个分片**。全提交之后
        # 命中通常只落在其中 1 个，剩下 35 个：
        #
        #   * `fut.cancel()` 只能取消**还没被 worker 取走**的任务；
        #   * 已被取走的（最多 8 个）必须跑完整个 chunk（实测 867ms）；
        #   * 但 `ProcessPoolExecutor` 的队列是**共享的**，取消一个已入队但
        #     未开始的任务，worker 会**立刻取下一个**——所以「取消」实际上
        #     只是把排队位置让出去，总工作量不变。
        #
        # 实测（`tools/diag_pow_residue.py`，2026-10-18）：
        #
        # | 快速命中后等待 | 下一轮耗时 | 相对基线 |
        # |---|---|---|
        # | 0 秒 | 3997 ms | **2.74x** |
        # | 1 秒 | 3166 ms | 2.17x |
        # | 3 秒 | 1455 ms | 1.00x |
        # | 8 秒 | 1329 ms | 0.91x |
        #
        # 收敛点约 3 秒，与「35 个残留 ÷ 8 worker × 867ms ≈ 3.8 秒」吻合。
        # `n=100` 验收基准用 `--gap 2.0`，**短于 3.8 秒**，于是每轮都带着
        # 残留起跑、持续积压，偶发越过 `startup_timeout=10s` 就 `degraded`：
        # 实测 2/100 次，耗时 25.7s / 57.7s，且都紧跟在「极快命中」之后
        # （#14 626ms → #15；#26 48ms → #27）。
        #
        # ## 滚动提交怎么治
        #
        # **在飞的分片数上限 = workers**。worker 只有 8 个，多提交的只会排队，
        # 命中后它们全是纯浪费。滚动之后命中时最多 8 个在跑（而非 35 个），
        # 残留从 3.8 秒降到 **≤ 867ms** —— 下一轮几乎立刻能拿到干净的首个分片。
        #
        # 顺带的收益：`cancel()` 要遍历的集合从 36 个降到 8 个，
        # 且这些**全都在跑**（cancel 返回 False，无需处理）—— 实际什么都不用做。
        #
        # ## 协作式中断再把残留砍一档（T-18 后半）
        #
        # 滚动提交只解决了「排队里有多少」，**没解决「正在跑的那 8 个」**：
        # 它们照样要跑完各自的 4096 个 nonce。所以在命中时把共享内存段
        # 置 1，让这 8 个在下一个检查点（≤256 个 nonce ≈ 56ms）自己退出。
        #
        # 两档叠加后，命中瞬间的残留代价：
        #   原来 35 个分片 × 867ms ÷ 8 worker ≈ 3.8 秒
        #   → 滚动提交后 ≤ 867ms
        #   → 再加协作式中断 ≤ 56ms
        in_flight: dict = {}  # future -> 该分片的 (start, end)
        exhausted = False

        def _submit_until_full() -> None:
            """补满在飞窗口。迭代器耗尽后置 `exhausted`。"""
            nonlocal exhausted
            while not exhausted and len(pending) < workers:
                try:
                    job = next(jobs)
                except StopIteration:
                    exhausted = True
                    return
                try:
                    fut = executor.submit(_search_chunk, job)
                except RuntimeError as exc:
                    # 池被意外 shutdown 或已损坏（常驻池的独有风险：它比临时池
                    # 活得久，更可能撞上「外部已 shutdown」这种状态）。丢掉它，
                    # 让上层降级，下一轮 `_get_executor()` 会重建一个干净的池。
                    if persistent:
                        _drop_executor()
                    raise PowParallelUnavailable(f"提交分片失败（池已损坏）：{exc}") from exc
                submitted.add(fut)
                pending.add(fut)
                in_flight[fut] = job

        _submit_until_full()

        while pending:
            # 只对**第一批**设时限：一旦有结果回来，说明进程池在本环境是能用的，
            # 后面就安心等（大 difficulty 下单个分片本来就可能要跑一会儿）。
            timeout = startup_timeout if done_chunks == 0 else None
            # ⚠️ `return_when=FIRST_COMPLETED` **不是可选项，漏了就是 10 秒的 bug**。
            #
            # `concurrent.futures.wait` 的默认值是 **`ALL_COMPLETED`** —— 它和
            # `asyncio.wait` 的默认值一样，但和本函数的意图**正好相反**。
            # 2026-10-16 实测：漏掉这个参数时，这一行不是「等第一个分片」，
            # 而是「等**全部 36 个**分片跑完」，被 `startup_timeout=10` 截断，
            # 于是**每一轮恒定等满 10 秒** —— 与答案藏在区间哪个位置完全无关
            # （答案是 0、第一个候选就命中，照样等满 10 秒）。
            # 而那 10 秒是**纯本地空耗**，全加在「按下回车 → 请求发出」之间。
            #
            # 修好之后：命中分片一返回就收工。实测同一条挑战从 ~10090ms
            # 降到 ~200ms（答案在分片 0 时）。
            done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                # ★ `transient=True`：**首轮**超时不足以判定「池坏了」。
                #
                # 两种情形都会走到这里，而它们该有不同的处置：
                #   1. spawn 池真卡死（2026-10-15 那次）—— 之后也不会有结果
                #   2. 池被另一个 `solve()` 抢占，我们的分片排在队列后面
                #      —— 池是好的，只是这次没抢到
                #
                # 光看这一次超时分不清，所以统一标 transient（**不**永久降级），
                # 由 `solve()` 用「连续失败计数」来判定第 1 种情形。
                raise PowParallelUnavailable(
                    f"多进程 PoW 在 {startup_timeout:.0f}s 内没有任何分片返回",
                    transient=True,
                )
            for fut in done:
                done_chunks += 1
                # 首个分片返回 = 池真的开始出活了。只记这一次 ——
                # 后面分片的返回时间取决于各自的搜索进度，没有诊断价值。
                if done_chunks == 1 and stats is not None:
                    stats["pool_startup_ms"] = (time.monotonic() - started) * 1000.0
                job = in_flight.pop(fut, None)
                if on_progress and job is not None:
                    # 进度按**已扫过的区间上界**报，而不是「完成分片数 × chunk_size」。
                    # 滚动提交下分片是乱序完成的，后者会报出超过 limit 的数。
                    on_progress(
                        min(job[1], limit),
                        limit,
                        time.monotonic() - started,
                    )
                res = fut.result()
                if res is not None:
                    # ★ 广播「停止」—— 这是协作式中断的触发点。
                    #
                    # 其余在飞分片（最多 workers - 1 个）正在各自的 chunk 里跑，
                    # 它们每 256 个 nonce 读一次段里的 4 字节。写入哨兵后
                    # （全 0xFF，不等于任何代数）它们会在 ≤56ms 内退出，
                    # 而不是跑完剩下的 nonce。
                    #
                    # 段是单例、活到进程退出，所以这里**不需要**也**不能**
                    # unlink —— 下一轮换代时把新代数写回去即可。
                    #
                    # ⚠️ 切片宽度写死 —— 段被页对齐到 4096 字节，`buf[:]` 是 4096。
                    _stop_shm.buf[:_STOP_TOKEN_BYTES] = _STOP_SENTINEL
                    return res
            # 有空位就补 —— 这是滚动提交的核心：**只维持 workers 个在飞**。
            _submit_until_full()
    finally:
        # ★ 停止段**不在这里释放** —— 它是进程内单例，活到进程退出。
        #
        # 曾经写成「每轮新建 + finally 里 close/unlink」，实测泄漏：
        # 子进程的映射还挂着时，Windows 的 `unlink()` 抛 `PermissionError`，
        # 被 `suppress` 吞掉，段就永久留在系统里（6 轮 6 个，见
        # `/tmp/probe_leak2.py`）。单例方案从结构上消除了这个时机。
        #
        # 也**不要**在这里把段写回「未置位」：命中路径已经把哨兵写进去了，
        # 那正是给在飞分片的退出信号。下一轮由 `_stop_segment()` 换代覆盖。
        if executor is None:
            # 池根本没建起来（`_get_executor` 抛了）。此时 `persistent`
            # 尚未赋值，不能拿它做分支 —— 否则 finally 里再抛 NameError
            # 会把原始异常盖掉，降级逻辑拿到的是错误的异常类型。
            pass
        elif persistent:
            # 常驻池：只 `cancel()` **未开始**的分片。
            # 已在跑的 worker 的 `cancel()` 返回 False（无法取消），它们会跑完
            # 当前 chunk 后自然空闲，下一轮继续用 —— 这是复用池的固有代价，
            # 远小于重建池的 3.8–6.1 秒。
            #
            # 滚动提交（T-18）之后这里通常**无事可做**：在飞数上限 = workers，
            # 而 workers 个分片必定都已被 worker 取走（否则说明池有空闲 worker，
            # 那 `wait` 早就返回了）。所以 `cancel()` 基本全返回 False ——
            # 这不是 bug，是「没有残留可清」的正常形态。
            # 残留从 35 个降到 ≤ 8 个，下一轮的等待时间从 3.8 秒降到 ≤ 867ms。
            # ⚠️ 这里**绝不能**调 `shutdown()`：shutdown 之后池不可再 submit，
            # 下一轮会直接抛 `RuntimeError: cannot schedule new futures after shutdown`。
            for fut in submitted:
                if not fut.done():
                    fut.cancel()
        else:
            # 临时池：**必须 `wait=False`** —— worker 可能已经不可用，
            # 等它收尾会把整轮对话一起拖死。
            executor.shutdown(wait=False, cancel_futures=True)
    return None


def solve(
    challenge: Challenge,
    target_path: str,
    *,
    max_nonce: int | None = None,
    progress_every: int = 20000,
    on_progress=None,
    workers: int | None = None,
    chunk_size: int = 4096,
) -> dict:
    """暴力搜索 nonce。返回可直接塞进 `x-ds-pow-response` 的 dict。

    `workers=None` → 自动按 CPU 核数（上限 8）；`workers=1` → 单进程（便于调试）。
    小 `difficulty`（< 20000）自动走单进程，避免进程池启动开销反而更慢。

    返回值里除协议字段外，还有一组 **`_` 前缀的计量字段**
    （`_elapsed_ms` / `_mode` / `_workers` / `_trials` / `_pool_startup_ms`）：
    它们会被 `deepseek_rpc.py` 过滤掉、**不会**进请求头，只供上报与门禁使用。
    """
    if challenge.algorithm != "DeepSeekHashV1":
        raise ValueError(f"不支持的算法：{challenge.algorithm}")

    prefix = f"{challenge.salt}_{challenge.expire_at}_".encode()
    if len(prefix) + 10 >= RATE:
        raise ValueError(f"前缀过长（{len(prefix)}），单块求解器不支持")

    target = bytes.fromhex(challenge.challenge)
    if len(target) != 32:
        raise ValueError("challenge 必须是 32 字节的 hex（64 字符）")

    limit = challenge.difficulty if max_nonce is None else min(max_nonce, challenge.difficulty)
    started = time.monotonic()

    if workers is None:
        # 环境变量可强制覆盖进程数（`GAD_POW_WORKERS=1` 即退回单进程）。
        #
        # 为什么需要这个开关：`_parallel_search` 用的是 `multiprocessing` 的
        # **spawn** 池，它在某些宿主环境下会**静默卡死** —— 池起来了、任务发出去了，
        # 但结果永远不回来（2026-10-15 在「引擎作为子进程 + stdio 管道」这个组合下
        # 实测到）。那种卡死既不抛异常也不打日志，唯一能自救的办法就是把进程数
        # 降到 1，所以这个开关是**逃生舱**，不是调试余兴。
        override = os.environ.get("GAD_POW_WORKERS", "").strip()
        workers = int(override) if override.isdigit() else min(os.cpu_count() or 1, 8)

    # 走哪条路径、实际用了几个进程 —— 这三个变量最终会以 `_mode` / `_workers`
    # 回报上去。它们回答的是**一个否则无法回答的问题**：多进程到底通不通。
    # 若线上出现 `degraded`，说明 spawn 池在那个环境真的卡死过、已降级单进程，
    # 那是「单核 25~80 秒」量级的问题，和「多进程 3~7 秒」完全不是一回事。
    mode = "single"
    used_workers = 1
    pool_stats: dict = {}

    # ★ 串行化：**只锁多进程分支**。
    #
    # 这把锁修的是 2026-10-18 实测到的降级：后台预取线程与主路径同时调
    # `solve()`，共用同一个单例池，各 submit 全量分片，主路径等不到首个分片
    # → 超时 → 被误判成「池坏了」→ 本进程永久单进程。
    # 详见 `_solve_lock` 的注释与 `tools/diag_pow_concurrency.py`。
    #
    # 单进程分支**不进锁**：它不碰池，没有争用可言；把它也圈进来只会让
    # 「预取走单进程（40 秒）」把主路径的多进程堵在后面 —— 那是在制造
    # 本该避免的等待。它最多占一个核，对 8 worker 的池影响可以忽略。
    if workers <= 1 or limit < 20000:
        answer = _search_chunk((0, limit, prefix, target))
        if answer is not None and on_progress:
            on_progress(answer, limit, time.monotonic() - started)
    else:
        with _get_solve_lock():
            try:
                answer = _parallel_search(
                    limit, prefix, target, workers, chunk_size, on_progress, stats=pool_stats
                )
                mode = "multi"
                used_workers = workers
                _reset_timeout_streak()
            except PowParallelUnavailable as exc:
                # 进程池没能给出结果 —— **降级单进程，而不是把整轮对话挂死**。
                # 慢，但它一定会给出结果或者明确报错，不会静默停在那里
                # 让用户以为「消息发丢了」。
                if exc.transient:
                    # 这一次没抢到/超时，但**不能**就此判定池坏了。
                    # 连续失败到阈值才认定为真故障 —— 见 `_reset_timeout_streak`。
                    streak = _bump_timeout_streak()
                    if streak >= _TIMEOUT_STREAK_LIMIT:
                        _disable_parallel(f"连续 {streak} 次超时，判定池不可用")
                else:
                    _disable_parallel(str(exc))
                answer = _search_chunk((0, limit, prefix, target))
                mode = "degraded"
                if answer is not None and on_progress:
                    on_progress(answer, limit, time.monotonic() - started)

    if answer is None:
        raise RuntimeError(
            f"PoW 求解失败：在 0..{limit} 内未找到答案"
            f"（耗时 {time.monotonic() - started:.1f}s）"
        )

    elapsed = time.monotonic() - started
    result = {
        "algorithm": challenge.algorithm,
        "challenge": challenge.challenge,
        "salt": challenge.salt,
        "answer": answer,
        "signature": challenge.signature,
        "target_path": target_path,
        # ---- 下面几条都带 `_` 前缀：**不会**进 `x-ds-pow-response` ----
        # `deepseek_rpc.py` 组装请求头时按 `startswith("_")` 过滤，所以加字段
        # 不会污染发给服务端的 payload。它们是给计量与门禁用的。
        "_elapsed_ms": round(elapsed * 1000, 1),
        "_mode": mode,
        "_workers": used_workers,
        # 命中 nonce 在 `[0, difficulty)` 里的位置 + 1 —— 也就是**顺序语义**下
        # 要扫过的 nonce 数。**不是实际算过的哈希次数**：多进程下各分片并行推进，
        # 命中分片的兄弟分片也在同时跑，命中即全局终止，所以真实哈希数通常比它少。
        # 它的用途是观察「服务端把答案藏在区间的哪个位置」—— 均匀分布下中位应
        # 接近 difficulty/2（当前 difficulty=144000 ⇒ 约 72000）。
        "_trials": answer + 1,
    }
    if "pool_startup_ms" in pool_stats:
        result["_pool_startup_ms"] = round(pool_stats["pool_startup_ms"], 1)
    if "pool_created" in pool_stats:
        # True ⇒ 这一轮新建了池（`_pool_startup_ms` 含真实 spawn 成本）
        # False ⇒ 复用了上一轮的池（`_pool_startup_ms` 只剩首个分片的计算时间）
        result["_pool_created"] = pool_stats["pool_created"]
    return result


def selftest() -> tuple[bool, str]:
    """用 C# 侧的金标准向量验证本实现。

    向量 1（哈希）：`ComputeHashHex("3c1b67cba98a8235c00d_1788633061248_0")`
    向量 2（求解）：2026-09-12 线上真实挑战，答案 80804
    """
    want = "a77d69cad11ee53b776e0b5fb61ba36074e67eaaa1b6644ef5006deb6be55453"
    got = ds_hash_hex(b"3c1b67cba98a8235c00d_1788633061248_0")
    if got != want:
        return False, f"哈希向量不符：\n  期望 {want}\n  实得 {got}"

    ch = Challenge(
        algorithm="DeepSeekHashV1",
        challenge="a8f7edfeb5c161a8cada44aaf10163272931267ebbd5ac5a085da51c084961d2",
        salt="94ca9d35e51f15b7677a",
        difficulty=144000,
        signature="68440d49b655ed5b500474e79a84f9d8a07e5cda67f8114e75433b87cb84adf8",
        expire_at=1789148689858,
    )
    # 单进程验证正确性（答案 80804）
    solved = solve(ch, "/api/v0/chat/completion", workers=1)
    if solved["answer"] != 80804:
        return False, f"单进程求解向量不符：期望 80804，实得 {solved['answer']}"

    # 多进程验证（同样答案，且应显著更快）
    solved_mp = solve(ch, "/api/v0/chat/completion")
    if solved_mp["answer"] != 80804:
        return False, f"多进程求解向量不符：期望 80804，实得 {solved_mp['answer']}"

    return True, (
        f"两个向量全过 · 单进程 {solved['_elapsed_ms']}ms "
        f"· 多进程 {solved_mp['_elapsed_ms']}ms（{os.cpu_count()} 核）"
    )


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ok, msg = selftest()
    print(("[通过] " if ok else "[失败] ") + msg)
    raise SystemExit(0 if ok else 1)
