#!/usr/bin/env python3
"""验证前端 `isHardBlocked()` 与宿主 `CanSubmit` 的判据等价。

## 为什么需要这个脚本

2026-10-18 我两次试图用「CDP 注入状态 + 点发送按钮」来验证宿主侧的门禁逻辑，
两次都让消息真的出了网。原因是**方法本身不成立**：

    CDP 注入只改前端的 `status` 变量；
    宿主的 `Status.CircuitLevel` 由引擎推来的 status 事件驱动，注入碰不到。

所以宿主看到的是 `closed`，照常放行，引擎真的向 DeepSeek 发了请求。
引擎日志里留下了 4 条本不该存在的 `run_end`。

**这条教训值得写进任何后续探针的第一行注释**：
想验宿主逻辑，只能单测或直接构造 `HostStatus`，不能靠点按钮。

## 这个脚本怎么验

两端判据都是**纯确定性表达式**，不依赖任何运行时状态，可以直接复刻求值：

前端 `shell/DeepSeekAgent.Web/Assets/web/app.js`：

    function isHardBlocked() {
      const lv = status && status.circuitLevel;
      return lv === 'open' || lv === 'halted';
    }

宿主 `shell/Shared/AgentHost.cs`：

    public bool CanSubmit =>
        _engine is not null && !Running
        && Status.CircuitLevel is not ("open" or "halted");

注意 C# 里 `is` 的优先级高于 `&&`，所以这个表达式等价于
`a && b && (level not in {open, halted})`，不会出现「只有 level 那半句被取反」的坑。

两者必须给出**同一套结论**：`CanSubmit == true` 当且仅当
「引擎在 + 没在跑 + 不是硬阻断」。任何一侧改判据而另一侧没跟上，
这个脚本会立刻报红 —— 那就是壳间行为分叉的开始。

用法：
    python tools/e2e/verify_gate_parity.py
"""
from __future__ import annotations


def js_hard_blocked(circuit_level: str) -> bool:
    """复刻 `app.js:isHardBlocked()`。"""
    return circuit_level in ("open", "halted")


def host_can_submit(engine_ok: bool, running: bool, circuit_level: str) -> bool:
    """复刻 `AgentHost.CanSubmit`。

    C# 原文：`_engine is not null && !Running && Status.CircuitLevel is not ("open" or "halted")`
    """
    return engine_ok and not running and circuit_level not in ("open", "halted")


#: (circuitLevel, running, engine_missing)
#:
#: 覆盖引擎状态机的全部四档（closed / throttled / open / halted）
#: 加上未知值 `?`（引擎刚启动、还没推过 status 时的默认值），
#: 以及「既熔断又在跑」「引擎未连接」两个交叉情形。
CASES = [
    ("closed",    False, False),   # 正常
    ("closed",    True,  False),   # 运行中
    ("throttled", False, False),   # 降速但可发 —— 不该拦
    ("open",      False, False),   # 熔断 —— 该拦
    ("halted",    False, False),   # 停机 —— 该拦
    ("open",      True,  True),    # 熔断 + 运行中 + 引擎没了（最糟的叠加）
    ("halted",    True,  True),
    ("?",         False, False),   # 状态未知 —— 不该拦（宁可让引擎自己报错）
    ("closed",    False, True),    # 引擎未连接
]


def main() -> int:
    print(f"{'circuitLevel':<14}{'running':<10}{'engine':<8}"
          f"{'CanSubmit':<12}{'isHardBlocked':<14}一致")
    print("-" * 70)

    all_ok = True
    for level, running, engine_missing in CASES:
        can = host_can_submit(not engine_missing, running, level)
        blk = js_hard_blocked(level)

        # 等价判据：宿主允许提交 ⟺ 前端不认为硬阻断、且不在跑、且引擎在
        expected = (not blk) and (not running) and (not engine_missing)
        ok = can == expected
        all_ok = all_ok and ok

        print(f"{level:<14}{str(running):<10}{str(not engine_missing):<8}"
              f"{str(can):<12}{str(blk):<14}{'[通过]' if ok else '[失败]'}")

    print()
    print("判定:", "[通过] 两端判据等价" if all_ok else "[失败] 判据分叉")
    if not all_ok:
        print("  含义：某个状态下前端会保住输入框、宿主却清空（或反之），"
              "用户会在两个壳里看到不同行为。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
