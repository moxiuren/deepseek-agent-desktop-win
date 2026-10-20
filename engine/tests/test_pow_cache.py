"""PoW 预取缓存自测（离线：不碰网络、不跑真求解）。

守护三件事：

1. **过期判据** —— 剩余寿命不足 `expire_margin_sec` 的条目必须被丢弃。
   发一个快过期的 challenge 去换一次 403，比 miss 更糟。
2. **容量控制** —— 缓存满了后台线程必须停下。每次预取都是一次真实的
   `create_pow_challenge` 请求，无上限预取等于刷接口。
3. **失败不崩** —— 取 challenge 失败只记数 + 退避，线程不能死。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_pow_cache.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.pow_cache import PowCache

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def wait_until(fn, timeout: float = 2.0, step: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(step)
    return False


def make_cache(**kw) -> PowCache:
    """测试专用构造：把预热延迟压到 0。

    生产默认 `initial_delay_sec=5.0` —— 引擎 `start()` 就在事件循环里，
    立刻开 8 个进程解 PoW 会拖慢启动。测试没这个顾虑，也不该等 5 秒。
    """
    kw.setdefault("initial_delay_sec", 0)
    return PowCache(**kw)


def make_fetch(expire_in_sec: float, counter: dict | None = None):
    """造一个 `fetch_solve`：每次返回一个 `expire_in_sec` 秒后过期的结果。"""

    def fetch():
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        return time.time() + expire_in_sec, {"answer": counter["n"] if counter else 1}

    return fetch


# ---------- 1. 空缓存 ----------

def scenario_empty() -> None:
    print("\n[1] 空缓存")
    c = make_cache(capacity=2)
    check("take() 在没有任何条目时返回 None", c.take() is None)
    check("size() 为 0", c.size() == 0)


# ---------- 2. 手动入队与命中 ----------

def scenario_hit() -> None:
    print("\n[2] 未过期 → 命中")
    c = make_cache(capacity=2)
    # 直接用后台线程灌一条（capacity=2 会继续补，但先只验 take 语义）
    c.start(make_fetch(300.0))
    ok = wait_until(lambda: c.size() >= 1)
    check("后台线程填进第一条", ok, f"size={c.size()}")

    got = c.take()
    check("take() 返回已解结果", isinstance(got, dict), repr(got))
    check("返回的是 fetch 造的那条", got is not None and got.get("answer") == 1)
    c.stop()


# ---------- 3. 过期丢弃 ----------

def scenario_expired() -> None:
    print("\n[3] 已过期 → 丢弃")
    c = make_cache(capacity=2, expire_margin_sec=30.0)
    c.start(make_fetch(-10.0))  # 已经过期 10 秒
    # 后台线程会解完发现过期、直接丢弃，不入队
    time.sleep(0.5)
    check("过期条目没有进缓存", c.size() == 0, f"size={c.size()}")
    check("take() 返回 None", c.take() is None)
    st = c.stats()
    check("expired 计数 >= 1", st["expired"] >= 1, str(st))
    c.stop()


# ---------- 4. 余量不足 → 丢弃 ----------

def scenario_margin() -> None:
    print("\n[4] 剩余寿命 < margin → 丢弃")
    c = make_cache(capacity=2, expire_margin_sec=30.0)
    # 10 秒后过期，余量 30 秒 ⇒ 不合格
    c.start(make_fetch(10.0))
    time.sleep(0.5)
    check("余量不足的条目没有进缓存", c.size() == 0, f"size={c.size()}")
    check("take() 返回 None", c.take() is None)
    c.stop()


# ---------- 5. 容量上限 ----------

def scenario_capacity() -> None:
    print("\n[5] 容量上限")
    counter = {"n": 0}
    c = make_cache(capacity=2, poll_sec=0.05)
    c.start(make_fetch(300.0, counter))
    ok = wait_until(lambda: c.size() >= 2)
    check("缓存填到容量 2", ok, f"size={c.size()}")
    # 再等一会儿，确认不会继续预取
    time.sleep(0.5)
    n_after_full = counter["n"]
    time.sleep(0.5)
    check(
        "满之后不再继续预取",
        counter["n"] == n_after_full,
        f"调用次数 {n_after_full} -> {counter['n']}",
    )
    check("size 不超过容量", c.size() <= 2, f"size={c.size()}")
    c.stop()


# ---------- 6. 失败退避 ----------

def scenario_failure() -> None:
    print("\n[6] fetch 抛异常 → 记数 + 不崩")
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("模拟网络失败")

    c = make_cache(capacity=2, fail_backoff_sec=0.2, poll_sec=0.05)
    c.start(boom)
    ok = wait_until(lambda: calls["n"] >= 1, timeout=2.0)
    check("fetch 被调用", ok, f"n={calls['n']}")
    time.sleep(0.6)
    check("线程仍存活（没被异常打死）", c._thread is not None and c._thread.is_alive())
    check("failed 计数 >= 1", c.stats()["failed"] >= 1, str(c.stats()))
    check("缓存为空", c.size() == 0)
    c.stop()
    check("stop 后线程退出", c._thread is None)


# ---------- 7. capacity=0 是空操作 ----------

def scenario_disabled() -> None:
    print("\n[7] capacity=0 → 空操作")
    called = {"n": 0}

    def fetch():
        called["n"] += 1
        return time.time() + 300, {}

    c = make_cache(capacity=0)
    c.start(fetch)
    time.sleep(0.2)
    check("没有启动线程", c._thread is None)
    check("fetch 一次都没调", called["n"] == 0)
    check("take() 返回 None", c.take() is None)


# ---------- 8. stop 幂等 ----------

def scenario_stop_idempotent() -> None:
    print("\n[8] stop 幂等")
    c = make_cache(capacity=1)
    c.start(make_fetch(300.0))
    wait_until(lambda: c.size() >= 1)
    c.stop()
    try:
        c.stop()
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    check("连续 stop 两次不抛异常", ok)


# ---------- 9. 过期条目不得阻塞补货（2026-10-18 回归） ----------

def scenario_expired_does_not_block() -> None:
    """引擎闲置后第一次发送，预取结果必须还是新鲜的。

    ## 这个 bug 是怎么被发现的

    端到端测试读引擎日志，看到 `produced=3 taken=0 expired=2 cached=1`：
    预取产出了 3 个，发送时**一个都用不上**，用户照旧干等 5–8 秒同步求解。

    根因：判断「缓存满了没有」只看 `len(self._items)`，而清过期只在
    `take()` 里做。两个 5 分钟前就烂掉的条目一直占着容量，后台认为满了，
    一直 sleep —— 直到某次发送把它们弹出来才清理，那时已经太晚。

    修法：后台循环在判断容量之前先 `_purge_expired_locked()`。
    本场景就是这条的回归锁。
    """
    print("\n[9] 过期条目不阻塞补货")
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        # 第 1 个 1.5 秒后过期（模拟「很久以前预取的」），之后都给长寿的
        life = 1.5 if calls["n"] == 1 else 300.0
        return time.time() + life, {"n": calls["n"]}

    c = make_cache(capacity=1, poll_sec=0.05, expire_margin_sec=0.0)
    c.start(fetch)

    check("第一个条目入队", wait_until(lambda: c.size() >= 1, timeout=2.0), f"size={c.size()}")

    # 等它过期。此后后台应当：purge → 退避 → 补一个新的新鲜货。
    time.sleep(2.0)
    ok = wait_until(lambda: c.size() >= 1 and calls["n"] >= 2, timeout=5.0)
    check("过期后自动补了新货", ok, f"size={c.size()} calls={calls['n']}")

    got = c.take()
    check("take() 能拿到结果（不是 None）", got is not None, str(got))
    st = c.stats()
    check("expired 计数 >= 1", st["expired"] >= 1, str(st))
    check("taken 计数 >= 1", st["taken"] >= 1, str(st))
    c.stop()


# ---------- 10. take() 重置闲置退避 ----------

def scenario_take_resets_idle() -> None:
    """用户一回来，补货节奏必须立刻恢复勤快。

    闲置退避是为了「人不在时别每 5 分钟白烧一次求解」。但如果用户回来了
    它还在按几分钟的节奏等，等于把延迟转嫁给用户。`take()` 是唯一的
    「用户在场」信号，所以它必须把 streak 归零。
    """
    print("\n[10] take() 重置闲置退避")
    c = make_cache(capacity=1, poll_sec=0.05)

    # 手动把 streak 推高（模拟闲置了很久）
    with c._lock:
        c._idle_streak = 10
    check("streak 初始为 10", c._idle_streak == 10)

    c.take()
    check("take() 后 streak 归零", c._idle_streak == 0, f"streak={c._idle_streak}")


def main() -> int:
    print("=" * 60)
    print("pow_cache 自测（离线）")
    print("=" * 60)

    scenario_empty()
    scenario_hit()
    scenario_expired()
    scenario_margin()
    scenario_capacity()
    scenario_failure()
    scenario_disabled()
    scenario_stop_idempotent()
    scenario_expired_does_not_block()
    scenario_take_resets_idle()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        for n in FAIL:
            print(f"  失败: {n}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
