"""实验·阶段 2：系统提示词到底能不能只发一次？

## 阶段 1 的结论，以及它为什么不成立

阶段 1（本文件旧版）只做了一件事：第 1 轮带提示词，第 2/3 轮发一段「假工具结果」，
看模型还出不出 ```local_cmd 围栏。结论写的是「仍遵守协议 → 长期方案可行」。

**那个结论无效。** 假工具结果的末尾是：

    请输出下一个 ```local_cmd，或结束本轮。

这句话来自 `protocol.py::format_command_result()` —— 它是**每一轮**工具结果都带的
短协议提醒。所以阶段 1 实际测的是「**无完整提示词 + 有短提醒**」，
而不是「无提示词」。

两者差别是决定性的：如果短提醒才是模型继续调工具的原因，那么「提示词只发一次」
这个方案就**不成立** —— 因为提醒同样每轮都在发，省下的只是几千字提示词，
而不是「每轮都必须带一句指令」这件事本身。

## 本阶段分离的三个变量

| 变量 | 取值 |
|---|---|
| 完整系统提示词 | 有（仅第 1 轮） / 无（第 2 轮起） |
| 短协议提醒 | 有（A 组） / 无（B 组） |
| 轮次深度 | 1–4（看衰减，而不是看单点） |

  * **A 组 = 生产现状**：第 1 轮完整提示词，后续轮次 = 工具结果 + 短提醒。
  * **B 组 = 目标方案**：第 1 轮完整提示词，后续轮次 = **裸**工具结果。

第 1 轮两组完全相同（同一份 `build_contract_prompt()` 产物、同一个任务），
否则基线不可比。

## 判据：三档，中间那档才是真正危险的

只看「有没有围栏」太粗。要分成：

  * `ok`    —— 解析出至少一个 `ToolCall`（生产代码能接续）
  * `soft`  —— 正文里有 ``` 围栏，但解析不出工具（模型换成了 ```bash 之类）
  * `hard`  —— 完全不出围栏（模型开始正常聊天）

`soft` 为什么最危险：`ToolCallParser` 认不出，于是 agent 认为这一轮没有工具调用、
结束循环；而模型那边在等一个永远不会来的结果。用户看到的是「聊着聊着就不动了」，
**全程没有任何报错**。这种失效形态比 `hard` 难查得多。

## 拟人操作（用户明确要求「不要因为测试导致封号」）

不自己造节奏，**直接复用生产的那套**：

  * `PacingGate` 的真实 log-normal 采样（中位 2.5s，非固定间隔）
  * `max_burst=2` + `hold_after_burst_ms=12000` 的突发硬停
  * `read_pause_every=6` 的阅读停顿
  * `record_send(now=发出时刻)` —— 回答耗时天然算进间隔，不额外叠加等待

刻意**不**调大 `hourly_max` / 不关 `prefetch`：实验要观察的是生产形态。

## 熔断（硬性）

任何一条命中就**立即停**，不重试、不硬撑：

  * `auth` / `suspended` / `fatal` → 账号级问题，停手
  * `Server busy` / `generation_timeout` → 服务端已经在明示「别打了」
  * 连续 RPC 异常 → 链路不健康

账号 2026-10-17 刚被 suspend 过一次，教训写在 `deepseek_rpc.py:180-195`。
实验能少跑一轮就少跑一轮，绝不为「凑样本」硬撑。

## 全 Log（用户明确要求「出问题能查回」）

落盘到 `docs/evidence/20261018-提示词持久化-阶段2/`：

  * `run-<时间戳>.jsonl` —— **逐行追加 + 每行 flush**。中途崩溃时前面的事件都在，
    这是「查回」的关键；整份 JSON 一次性写出做不到这一点。
  * `report-<时间戳>.md` —— 跑完生成的人类可读结论。

每轮记录：prompt 全文、响应全文、三档判定、耗时、gate 决策、熔断快照、
PoW 数据、会话三元组。**不记录 token**（连前缀都不记）。

## 用法

    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/exp_prompt_persistence.py --dry-run
    ./.venv/Scripts/python.exe -X utf8 tools/exp_prompt_persistence.py

    # 只跑一组 / 只跑 2 轮（小规模试跑）
    ./.venv/Scripts/python.exe -X utf8 tools/exp_prompt_persistence.py --groups A --turns 2

**这个脚本发真实请求**，且在账号里留下会话（`ChatConfig.temporary` 尚未实现，
见 config.py 的注释）。所以它不进自动测试，只能手动跑。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import re
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.circuit import BreakerLevel, CircuitBreaker  # noqa: E402
from gad_engine.config import EngineConfig  # noqa: E402
from gad_engine.gates import GateChain, GateDecision  # noqa: E402
from gad_engine.pacing import PacingGate  # noqa: E402
from gad_engine.protocol import (  # noqa: E402
    ToolCallParser,
    build_contract_prompt,
)
from gad_engine.session import SessionManager  # noqa: E402
from gad_engine.transport import TransportError, create_transport  # noqa: E402
from gad_engine.transport.deepseek_rpc import ALLOW_LIVE_ENV  # noqa: E402

#: 真实发送闸门拒绝时的错误特征串。
#:
#: 为什么必须单独识别：`deepseek_rpc.send()` 拦下真实发送时抛的是
#: `TransportError(auth=False, fatal=False, config=False)` —— 四个标志位全空，
#: 于是 `note_and_check()` 会判 `must_stop=False`，脚本继续跑下一轮。
#: 实测（2026-10-18 第一次试跑）就是这样：两轮全部「成功发出去了」，
#: 实际一个请求都没出网，日志里只有两条一样的拒绝文本。
#: 这属于**环境没准备好**，不是实验失败 —— 必须立刻停，并告诉人怎么解决。
GATE_REFUSED_MARK = "已阻止真实发送"

OUT_DIR = ROOT.parent / "docs" / "evidence" / "20261018-提示词持久化-阶段2"

#: 工具围栏的**独立复述** —— 故意不 import 引擎里的私有常量。
#: 验证工具如果复用被测代码的常量，「两边一起改错」就查不出来了。
#: （这个做法沿用 `verify_fence_live.py`。）
OPENER_RE = re.compile(
    r"(?m)^[ \t]*```(?:local_cmd:async|local_cmd|write_file:|replace_in_file:"
    r"|read_file:|list_dir:|grep:|glob:|agent-screenshot|agent-attach)"
)

#: 任务 —— 要一个「必须连续多轮用工具」的活，这样第 2 轮起模型有明确的下一步，
#: 而不是「上一件事已经做完了」然后自然收尾（那会被误判成协议失效）。
TASK = (
    "先列出当前工作目录下的文件，然后逐个读取每个文件的第一行内容。\n"
    "必须用工具，不要凭空回答。"
)

#: 模拟的工具结果内容。两组用**同一份**正文，只有尾巴不同 ——
#: 这是 A/B 唯一允许的差异，否则就分不清是哪个变量起作用。
TOOL_BODY = "a.txt\nb.txt\nc.txt"

#: A 组尾巴 —— 逐字复刻 `protocol.py::format_command_result()`。
HINT_TAIL = "\n\n请输出下一个 ```local_cmd，或结束本轮。"

#: 第 2 轮起的轮次里，工具结果对应的「做了什么」。只用于日志可读性。
STEP_LABELS = ["列出文件", "读 a.txt", "读 b.txt", "读 c.txt", "读 d.txt"]


def tool_result(turn: int, with_hint: bool) -> str:
    """造一份第 `turn` 轮的工具结果。

    `turn` 从 2 开始（第 1 轮是带提示词的任务）。内容本身不重要 ——
    重要的是**有没有那句尾巴**，那是本实验要分离的变量。
    """
    label = STEP_LABELS[min(turn - 1, len(STEP_LABELS) - 1)]
    body = f"{TOOL_BODY}\n\n（{label} 完成）" if turn > 2 else TOOL_BODY
    head = "[Tool Call Result (Exit: 0)]:"
    out = f"{head}\n{body}"
    return out + HINT_TAIL if with_hint else out


# ---------------------------------------------------------------- 判定


def judge(text: str) -> dict:
    """三档判定 + 独立的围栏计数。

    `parsed` 走生产解析器（这是「agent 到底能不能接续」的真判据）；
    `raw_fences` 走本文件自带的正则（这是「模型到底有没有想调工具」的独立对照）。
    两者不一致时，差额就是被吞掉的调用 —— 这正是 `soft` 退化的定义。
    """
    body = text or ""
    calls = ToolCallParser().feed(body)
    raw = len(OPENER_RE.findall(body))
    has_fence_char = "```" in body

    if calls:
        grade = "ok"
    elif raw > 0:
        # 有工具围栏但解析器没吐出来 —— 理论上不该发生（解析器以完整块为单位），
        # 出现即说明解析器有边界问题，必须单独标出来。
        grade = "parse_lost"
    elif has_fence_char:
        grade = "soft"          # 用了 ``` 但不是工具围栏（```bash / ```shell …）
    else:
        grade = "hard"          # 完全没出围栏

    return {
        "grade": grade,
        "parsed_calls": len(calls),
        "kinds": [c.kind.value for c in calls],
        "targets": [c.target for c in calls if c.target],
        "raw_fences": raw,
        "has_fence_char": has_fence_char,
        "chars": len(body),
    }


# ---------------------------------------------------------------- 日志


class RunLog:
    """逐行 JSON 日志。

    **为什么不是「跑完写一份 JSON」**：中途崩溃 / 被 Ctrl+C / 撞上熔断时，
    一次性写出的方案会**一个字节都不留**。逐行追加 + 每行 flush 才能保证
    「已经发生的都在盘上」—— 这正是用户要求的「万一有什么问题可以查回」。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8", newline="\n")

    def event(self, kind: str, **fields) -> None:
        rec = {
            "t": datetime.now().isoformat(timespec="milliseconds"),
            "kind": kind,
            **fields,
        }
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fh.close()


# ---------------------------------------------------------------- 闸门


async def await_gate(gates: GateChain, log: RunLog, budget_s: float = 900.0) -> GateDecision:
    """在闸门放行前一直等 —— 逻辑与 `agent.py::_await_gate` 一致。

    为什么是轮询而不是「算好要等多久就睡多久」：闸门状态会被外部改变
    （熔断退避结束、限流冷却到期），一次性长睡会错过。
    轮询本身廉价，因为 `PacingGate.evaluate()` 保证幂等（目标间隔抽一次就记住）。
    """
    deadline = time.monotonic() + budget_s
    last_reason: str | None = None
    while True:
        decision = gates.evaluate()
        if decision.allowed or decision.fatal:
            return decision
        if decision.reason != last_reason:
            last_reason = decision.reason
            log.event("gate_hold", layer=decision.layer,
                      reason=decision.reason, wait_ms=decision.wait_ms)
            print(f"    [闸门] {decision.reason}（等 {decision.wait_sec}s）", flush=True)
        wait = min(decision.wait_ms / 1000.0, 30.0)
        if time.monotonic() + wait > deadline:
            return decision
        await asyncio.sleep(max(wait, 0.2))


def note_and_check(gates: GateChain, log: RunLog, *, exc: TransportError | None = None,
                   outcome=None) -> tuple[bool, str]:
    """把一次失败喂给熔断层，返回 (是否必须立刻停, 原因)。

    **判据刻意保守**：账号 2026-10-17 刚被 suspend 过一次，实验不值得冒第二次。
    任何认证类信号、任何「服务端忙」、任何 fatal，一律停手 ——
    哪怕理论上退避 30 分钟还能继续。
    """
    if exc is not None:
        # 环境闸门（默认拒绝真实发送）—— 必须先于所有信号分类判断。
        # 它四个标志位全空，落到下面会被当成「普通 RPC 异常」而放行继续，
        # 于是脚本一路空转到结束。见 GATE_REFUSED_MARK 的注释。
        if GATE_REFUSED_MARK in str(exc):
            log.event("gate_refused", detail=str(exc)[:300],
                      env=ALLOW_LIVE_ENV)
            return True, f"真实发送被环境闸门拦下（{ALLOW_LIVE_ENV} 未设为 1）"
        if exc.suspended:
            log.event("breaker_input", source="transport", signal="suspended",
                      detail=str(exc)[:300])
            return True, f"账号被处置：{exc}"
        if exc.fatal:
            log.event("breaker_input", source="transport", signal="fatal",
                      detail=str(exc)[:300])
            return True, f"fatal：{exc}"
        event = gates.note_transport_error(exc)
        log.event("breaker_input", source="transport",
                  signal=(event.signal.value if event and event.signal else ""),
                  level=(event.level.value if event else ""),
                  detail=str(exc)[:300])
        if exc.auth:
            return True, f"认证失败：{exc}"
        return False, str(exc)

    if outcome is not None and not outcome.ok:
        event = gates.note_outcome(outcome)
        log.event("breaker_input", source="outcome",
                  signal=(event.signal.value if event and event.signal else ""),
                  level=(event.level.value if event else ""),
                  detail=(outcome.error or "")[:300])
        # 「服务端忙」直接停 —— 退避是 30 分钟起，实验没必要等，也不该等。
        return True, f"服务端返回错误：{outcome.error}"

    return False, ""


# ---------------------------------------------------------------- 单轮


async def one_turn(
    *,
    group: str,
    turn: int,
    prompt: str,
    prompt_kind: str,
    transport,
    manager: SessionManager,
    gates: GateChain,
    log: RunLog,
) -> dict:
    """发一轮、记全量、返回这一轮的判定结果。"""
    meta = manager.meta
    print(f"\n  [第 {turn} 轮] {prompt_kind} · {len(prompt)} 字符 · "
          f"会话 turns={meta.turns} cid={meta.cid[:8] or '（新）'}", flush=True)

    # ---- 闸门 ----
    decision = await await_gate(gates, log)
    if not decision.allowed:
        log.event("turn_skipped", group=group, turn=turn,
                  layer=decision.layer, reason=decision.reason, fatal=decision.fatal)
        return {"group": group, "turn": turn, "prompt_kind": prompt_kind,
                "skipped": True, "reason": decision.reason, "fatal": decision.fatal}

    log.event("turn_send", group=group, turn=turn, prompt_kind=prompt_kind,
              prompt=prompt, gate_reason=decision.reason,
              gate_layer=decision.layer, pacing=gates.pacing.snapshot(),
              breaker=gates.breaker.snapshot())

    # ---- 发送 ----
    deltas: list[str] = []
    thoughts: list[str] = []
    t0 = time.monotonic()
    try:
        outcome = await transport.send(
            prompt,
            meta,
            temporary=False,
            extended_thinking=False,
            search_enabled=False,
            files=None,
            on_delta=deltas.append,
            on_thought=thoughts.append,
        )
    except TransportError as exc:
        elapsed = (time.monotonic() - t0) * 1000.0
        must_stop, why = note_and_check(gates, log, exc=exc)
        log.event("turn_error", group=group, turn=turn, elapsed_ms=round(elapsed, 1),
                  error=str(exc)[:1000], auth=exc.auth, fatal=exc.fatal,
                  config=exc.config, suspended=exc.suspended, must_stop=must_stop)
        print(f"    [错误] {str(exc)[:200]}", flush=True)
        return {"group": group, "turn": turn, "prompt_kind": prompt_kind,
                "grade": "error", "error": str(exc), "must_stop": must_stop,
                "stop_reason": why}
    except Exception as exc:  # noqa: BLE001 - 实验脚本不能因为未知异常丢掉日志
        elapsed = (time.monotonic() - t0) * 1000.0
        log.event("turn_error", group=group, turn=turn, elapsed_ms=round(elapsed, 1),
                  error=f"{type(exc).__name__}: {exc}"[:1000], unexpected=True)
        print(f"    [错误] 未预期异常 {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        return {"group": group, "turn": turn, "prompt_kind": prompt_kind,
                "grade": "error", "error": f"{type(exc).__name__}: {exc}",
                "must_stop": True, "stop_reason": "未预期的异常，停手排查"}

    elapsed = (time.monotonic() - t0) * 1000.0

    # ---- 记账：只有真实成功才记（与生产一致）----
    # 注意 `now` 传**发送时刻**（t0），不是此刻 —— 间隔闸门要量的是两次请求
    # 之间的间隔，用轮末时刻会把回答耗时从间隔里抹掉。
    gates.commit(now=t0)
    if outcome.cid:
        manager.update_from_response(outcome.cid, outcome.rid, outcome.rcid)

    # ---- 失败路径 ----
    if not outcome.ok:
        must_stop, why = note_and_check(gates, log, outcome=outcome)
        log.event("turn_outcome_error", group=group, turn=turn,
                  elapsed_ms=round(elapsed, 1), error=outcome.error[:1000],
                  must_stop=must_stop)
        return {"group": group, "turn": turn, "prompt_kind": prompt_kind,
                "grade": "error", "error": outcome.error, "must_stop": must_stop,
                "stop_reason": why}

    gates.note_success()

    # 流式可能整个为空（服务端把正文只放在快照帧里）—— 与 agent.py 同款兜底。
    text = "".join(deltas) or outcome.text or ""
    verdict = judge(text)

    record = {
        "group": group,
        "turn": turn,
        "prompt_kind": prompt_kind,
        "grade": verdict["grade"],
        "parsed_calls": verdict["parsed_calls"],
        "kinds": verdict["kinds"],
        "targets": verdict["targets"],
        "raw_fences": verdict["raw_fences"],
        "has_fence_char": verdict["has_fence_char"],
        "chars": verdict["chars"],
        "elapsed_ms": round(elapsed, 1),
        "response": text,
        "thoughts_chars": sum(len(x) for x in thoughts),
        "pow": {
            "mode": outcome.pow_mode,
            "challenge_ms": outcome.challenge_ms,
            "pow_ms": outcome.pow_ms,
            "cached": outcome.pow_cached,
            "solve_ms": outcome.pow_solve_ms,
            "trials": outcome.pow_trials,
            "workers": outcome.pow_workers,
        },
        "session": {"cid": outcome.cid, "rid": outcome.rid, "rcid": outcome.rcid},
        "must_stop": False,
    }
    log.event("turn_done", **record)

    mark = {"ok": "[通过]", "soft": "[警告]", "hard": "[失败]",
            "parse_lost": "[严重]"}.get(verdict["grade"], "[?]")
    print(f"    {mark} grade={verdict['grade']} 解析={verdict['parsed_calls']} "
          f"围栏={verdict['raw_fences']} 耗时={elapsed / 1000:.1f}s", flush=True)
    preview = (text or "").replace("\n", " ")[:120]
    print(f"    正文：{preview}", flush=True)
    return record


# ---------------------------------------------------------------- 一组


async def run_group(
    *,
    group: str,
    turns: int,
    base_prompt: str,
    transport,
    gates: GateChain,
    log: RunLog,
) -> list[dict]:
    """跑一组（一个独立会话，连续 `turns` 轮）。

    每组**必须用独立会话** —— 否则 A 组第 1 轮发过的完整提示词还留在上下文里，
    B 组第 2 轮就「继承」了它，实验就白做了。
    """
    with_hint = group.upper() == "A"
    print(f"\n{'=' * 72}", flush=True)
    print(f"  {group} 组 —— "
          f"{'工具结果 + 短提醒（生产现状）' if with_hint else '裸工具结果（目标方案）'}", flush=True)
    print(f"{'=' * 72}", flush=True)

    # 独立会话：auto_rotate=False 保证中途不换；temporary=False 与生产默认一致。
    manager = SessionManager(max_turns=0, max_age_sec=0,
                             temporary=False, auto_rotate=False)
    log.event("group_start", group=group, turns=turns, with_hint=with_hint,
              task=TASK, base_prompt_chars=len(base_prompt))

    results: list[dict] = []
    for turn in range(1, turns + 1):
        if turn == 1:
            prompt, kind = base_prompt, "full_system_prompt"
        else:
            prompt = tool_result(turn, with_hint)
            kind = "tool_result_with_hint" if with_hint else "tool_result_bare"

        rec = await one_turn(
            group=group, turn=turn, prompt=prompt, prompt_kind=kind,
            transport=transport, manager=manager, gates=gates, log=log,
        )
        results.append(rec)

        if rec.get("must_stop"):
            log.event("group_aborted", group=group, turn=turn,
                      reason=rec.get("stop_reason", ""))
            print(f"    [熔断] 停手：{rec.get('stop_reason', '')}", flush=True)
            break

        # 闸门层判了 fatal（例如账号被处置）也要停。
        if gates.breaker.evaluate().level is BreakerLevel.HALTED:
            log.event("group_aborted", group=group, turn=turn, reason="熔断层 HALTED")
            print("    [熔断] 熔断层已 HALTED，停手。", flush=True)
            break

    log.event("group_done", group=group, results=len(results))
    return results


# ---------------------------------------------------------------- 报告


def build_report(all_results: dict[str, list[dict]], *, started: str,
                 transport: str, model: str, log_path: Path) -> str:
    """把结果渲染成人类可读的 Markdown。

    `transport` / `model` 走参数而不是整个 `EngineConfig`：`--from-log` 重建
    报告时手上只有日志，没有（也不该重新加载）配置 —— 报告必须反映**当时**
    跑的档位，而不是今天的配置。
    """
    lines: list[str] = [
        "# 提示词持久化实验 · 阶段 2",
        "",
        f"- 开始时间：{started}",
        f"- 传输档位：`{transport}`",
        f"- 模型：`{model}`",
        "- 思考模式：关（与生产默认一致）",
        "- 联网搜索：关",
        f"- 原始日志：`{log_path.name}`",
        "",
        "## 判据",
        "",
        "| 档位 | 含义 |",
        "|---|---|",
        "| `ok` | 解析出至少一个 ToolCall（生产代码能接续） |",
        "| `soft` | 有 ``` 围栏但解析不出工具（模型换了语言标记） |",
        "| `hard` | 完全不出围栏（模型开始聊天） |",
        "| `error` | 请求失败或被熔断拦下 |",
        "",
        "## 逐轮结果",
        "",
        "| 组 | 轮 | 提示词形态 | 判定 | 解析调用 | 原始围栏 | 耗时(s) | PoW(s) | 命中缓存 | 模式 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for group, results in all_results.items():
        for r in results:
            if r.get("skipped"):
                lines.append(
                    f"| {group} | {r['turn']} | {r['prompt_kind']} | 跳过 "
                    "| - | - | - | - | - | - |"
                )
                continue
            pow_ = r.get("pow") or {}
            lines.append(
                f"| {group} | {r['turn']} | {r['prompt_kind']} | "
                f"`{r['grade']}` | {r.get('parsed_calls', '-')} | "
                f"{r.get('raw_fences', '-')} | {r.get('elapsed_ms', 0) / 1000:.1f} | "
                f"{pow_.get('pow_ms', 0) / 1000:.1f} | "
                f"{'是' if pow_.get('cached') else '否'} | "
                f"`{pow_.get('mode', '?')}` |"
            )

    lines += ["", "## 结论", ""]
    for group, results in all_results.items():
        later = [r for r in results if r.get("turn", 0) >= 2 and not r.get("skipped")]
        if not later:
            lines.append(f"- **{group} 组**：后续轮次没有有效样本，无法下结论。")
            continue
        ok = sum(1 for r in later if r.get("grade") == "ok")
        soft = sum(1 for r in later if r.get("grade") == "soft")
        hard = sum(1 for r in later if r.get("grade") == "hard")
        err = sum(1 for r in later if r.get("grade") == "error")
        lines.append(
            f"- **{group} 组**（后续 {len(later)} 轮）："
            f"ok {ok} · soft {soft} · hard {hard} · error {err}"
        )

    # ---- PoW 耗时：这是与「提示词持久化」无关、但影响所有请求的独立发现 ----
    #
    # 量的是 `pow_ms`（**用户实际等了多久**），不是 `solve_ms`。两者在缓存
    # 命中时是反的：`pow_ms=0`（不用等），`solve_ms` 却是当初后台预取求解的
    # 耗时（11–32 秒）。拿 `solve_ms` 算「命中 vs 未命中」的比值会得出 1.5 倍
    # 这种荒谬数字 —— 那是预取线程的账，不是用户的账。
    pow_rows = [
        (r, r.get("pow") or {})
        for rs in all_results.values()
        for r in rs
        if not r.get("skipped") and r.get("pow")
    ]
    if pow_rows:
        hit = [p for _, p in pow_rows if p.get("cached")]
        miss = [p for _, p in pow_rows if not p.get("cached")]
        lines += ["", "### PoW 耗时（附带发现，与提示词无关）", ""]
        lines.append(
            f"- 样本：{len(pow_rows)} 次请求，缓存命中 {len(hit)} 次 / 未命中 {len(miss)} 次"
        )
        if hit:
            lines.append("- 命中缓存：`pow_ms` 恒为 0（无需等待，预取已算好）")
        if miss:
            mv = sorted(p.get("pow_ms", 0) for p in miss)
            lines.append(
                f"- 未命中时 `pow_ms`（用户实际等待）："
                f"{mv[0] / 1000:.1f} – {mv[-1] / 1000:.1f} 秒"
            )
        # 求解模式 —— 这比耗时本身更能说明问题：degraded = 多进程池挂了，
        # 退回单进程硬扫。见 pow.py 的 PowParallelUnavailable 注释。
        modes: dict[str, int] = {}
        for _, p in pow_rows:
            m = p.get("mode") or "?"
            modes[m] = modes.get(m, 0) + 1
        lines.append(
            "- 求解模式：" + " · ".join(f"`{m}` {n} 次" for m, n in sorted(modes.items()))
        )
        if modes.get("degraded"):
            workers = sorted({p.get("workers") for _, p in pow_rows
                              if p.get("mode") == "degraded" and p.get("workers")})
            wtext = "/".join(str(w) for w in workers) or "?"
            lines.append("")
            lines.append(
                f"[警告] 有 {modes['degraded']} 次走 `degraded` 路径"
                f"（多进程池不可用，已降级单进程，实际 workers={wtext}）。"
            )
            lines.append(
                "这是 `pow.py::_parallel_search()` 抛 `PowParallelUnavailable` 后"
                "**全局永久降级**的结果 —— 一次失败，本进程后续所有轮次都走单进程。"
                "文档 `docs/20261017-防封禁研究分析.md` 记载的「同步解 5.1–7.7 秒」"
                "是**多进程**路径的数字，与这里的单进程耗时不可比。"
            )
            lines.append("")
            lines.append(
                "读 `模式` 列时注意：`cached=True` 的轮次里，模式记录的是**当初预取"
                "求解**时走的路径，而那一轮用户根本没等（`pow_ms=0`）。预取与主路径"
                "共用 `pow.py` 的模块级 `_parallel_disabled`，所以缓存里出现 `multi` "
                "只说明「预取那一刻降级还没发生」，不代表该轮次走得多进程。"
                "只有 `cached=False` 的行，模式才代表用户实际等待的那条路径。"
            )

    a_later = [r for r in all_results.get("A", []) if r.get("turn", 0) >= 2]
    b_later = [r for r in all_results.get("B", []) if r.get("turn", 0) >= 2]
    if a_later and b_later:
        a_ok = all(r.get("grade") == "ok" for r in a_later)
        b_ok = all(r.get("grade") == "ok" for r in b_later)
        lines += ["", "### 对「短提醒是否必需」的回答", ""]
        if a_ok and b_ok:
            lines.append(
                "A、B 两组后续轮次都保持 `ok` ⇒ **短提醒不是必要条件**。"
                "裸工具结果（不带「请输出下一个 ```local_cmd」那句）同样能让模型继续出围栏。"
            )
            lines.append("")
            lines.append(
                "注意范围：本实验**没有**证明「完整提示词只发一次」可行 —— 那是"
                "`agent.py:391` 的既有生产行为（`_bootstrap_prompt()` 在循环外只调一次），"
                "本实验只是在这条既有路径上，验证了后续轮次连短提醒都可以不带。"
            )
        elif a_ok and not b_ok:
            lines.append(
                "A 组保持 `ok` 而 B 组退化 ⇒ **短提醒是必要条件**。"
                "「提示词只发一次」不成立；能省的只有提示词那几千字，"
                "每轮那句协议提醒必须留着。"
            )
        elif not a_ok and not b_ok:
            lines.append(
                "两组后续轮次都退化 ⇒ 连短提醒也不够，"
                "当前实现依赖**每一轮**都带完整提示词。"
            )
        else:
            lines.append("结果不一致（A 组退化而 B 组正常）—— 样本量不足或存在噪声，需重跑。")

    lines += [
        "",
        "## 副作用（必须知道）",
        "",
        "- 本次实验在账号里留下了会话（`ChatConfig.temporary` 尚未实现，"
        "无论配置如何，会话都会出现在网页版历史里）。",
        "- 每个组一个会话，组内连续多轮 —— 这本身是正常使用形态，不是异常密度。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 从日志重建


def load_results_from_log(path: Path) -> tuple[dict[str, list[dict]], dict]:
    """从既有 `run-*.jsonl` 重建结果，用来**不重跑**就重出报告。

    为什么要这条路径：报告格式改了（比如这次补 PoW 列），不该为了看一眼
    新格式就再发 8 次真实请求 —— 那既是白烧额度，也是对账号的额外暴露。
    日志本来就是全量落盘的，重建是纯本地操作。

    只依赖 `run_start` / `turn_done` / `turn_error` 三种事件；`gate_hold`
    之类的过程事件忽略（它们不进报告）。事件按**文件顺序**归组，不排序 ——
    日志是按时间写的，顺序本身就是证据。
    """
    meta: dict = {"transport": "?", "model": "?", "groups": [], "turns": 0}
    all_results: dict[str, list[dict]] = {}
    order: list[str] = []

    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # 半截行（进程被杀）不该让整个重建失败 —— 跳过并继续。
                print(f"  [警告] 第 {lineno} 行不是合法 JSON，已跳过", flush=True)
                continue

            kind = rec.get("kind")
            if kind == "run_start":
                meta["transport"] = rec.get("transport", "?")
                meta["model"] = rec.get("model", "?")
                meta["groups"] = rec.get("groups", [])
                meta["turns"] = rec.get("turns", 0)
                meta["started"] = rec.get("t", "")
            elif kind == "turn_done":
                group = rec.get("group", "?")
                if group not in all_results:
                    all_results[group] = []
                    order.append(group)
                all_results[group].append({k: v for k, v in rec.items()
                                           if k not in ("t", "kind")})
            elif kind == "turn_error":
                group = rec.get("group", "?")
                if group not in all_results:
                    all_results[group] = []
                    order.append(group)
                all_results[group].append({
                    "group": group,
                    "turn": rec.get("turn"),
                    "prompt_kind": rec.get("prompt_kind", "?"),
                    "grade": "error",
                    "error": rec.get("error", ""),
                    "elapsed_ms": rec.get("elapsed_ms", 0),
                    "must_stop": rec.get("must_stop", False),
                    "stop_reason": "",
                })

    # 按 run_start 声明的顺序排 —— 日志里若有组被打断，dict 插入序仍可用，
    # 但声明顺序才是人读报告时期待的顺序。
    declared = [g for g in meta.get("groups") or [] if g in all_results]
    ordered = {g: all_results[g] for g in declared}
    for g in order:
        ordered.setdefault(g, all_results[g])
    return ordered, meta


def rebuild_report(log_path: Path) -> int:
    """从日志重建报告，写到同一目录的 `report-<同时间戳>.md`。

    刻意**不覆盖**原报告：原报告是当时跑出来的第一手产物，重建版可能因为
    模板改动而不同。两份都在，才看得出「结论变没变」。
    """
    if not log_path.exists():
        print(f"[错误] 日志不存在：{log_path}")
        return 2

    all_results, meta = load_results_from_log(log_path)
    if not all_results:
        print("[错误] 日志里没有任何 turn_done / turn_error 事件，无法重建。")
        return 2

    # 文件名 `run-20261018-145629.jsonl` -> 时间戳 `20261018-145629`。
    # 认不出来就退化成「现在」，不硬凑。
    m = re.search(r"(\d{8}-\d{6})", log_path.stem)
    stamp = m.group(1) if m else datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = log_path.parent / f"report-{stamp}-rebuilt.md"

    started = meta.get("started") or stamp
    # `t` 是 ISO 带毫秒（2026-10-18T14:56:29.123）—— 报告头部原来用的是
    # `%Y-%m-%d %H:%M:%S`，这里就地裁到秒，保持前后一致。
    if "T" in started:
        started = started.replace("T", " ")[:19]

    report = build_report(all_results, started=started,
                          transport=meta.get("transport", "?"),
                          model=meta.get("model", "?"),
                          log_path=log_path)
    out_path.write_text(report, encoding="utf-8", newline="\n")

    print("=" * 72)
    print("  从日志重建报告（未发送任何请求）")
    print("=" * 72)
    for group, results in all_results.items():
        seq = " · ".join(f"{r.get('turn')}:{r.get('grade', '?')}" for r in results)
        print(f"  {group} 组：{seq}")
    print()
    print(f"  报告：{out_path}")
    return 0


# ---------------------------------------------------------------- 主流程


def build_base_prompt(cfg: EngineConfig) -> str:
    """用**生产同一份** `build_contract_prompt()` 生成第 1 轮提示词。

    为什么不用旧版那种手写的最小提示词：本实验要回答的是「生产用的那份提示词
    能不能只发一次」。拿最小提示词测出来的结论没法外推到生产 ——
    那只是「一段很短的话能不能只发一次」。

    `read_prompt_template(seed=False)`：**不播种**。播种会往真实配置目录写文件，
    实验脚本不该有这种副作用（用户可能从没打开过设置面板）。
    """
    return build_contract_prompt(
        os_name=f"{platform.system()} {platform.release()}",
        work_dir=str(ROOT.parent),
        shell_hint="bash",
        soul=cfg.read_soul(),
        memory="",
        summary="",
        max_turns=cfg.max_tool_turns,
        template=cfg.read_prompt_template(seed=False),
    ) + f"\n\n# 任务\n{TASK}"


async def main() -> int:
    ap = argparse.ArgumentParser(description="提示词持久化实验 · 阶段 2")
    ap.add_argument("--turns", type=int, default=4, help="每组跑几轮（默认 4）")
    ap.add_argument("--groups", default="A,B", help="跑哪些组，逗号分隔（默认 A,B）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    ap.add_argument("--from-log", default="",
                    help="不发送任何请求，从既有 run-*.jsonl 重建报告")
    args = ap.parse_args()

    # 重建路径放在最前：它不发请求、不需要配置、不需要闸门，
    # 放在后面就会被下面的预检和配置加载挡住。
    if args.from_log:
        return rebuild_report(Path(args.from_log))

    groups = [g.strip().upper() for g in args.groups.split(",") if g.strip()]
    unknown = [g for g in groups if g not in ("A", "B")]
    if unknown:
        print(f"[错误] 未知分组：{unknown}（只支持 A / B）")
        return 2
    if args.turns < 1:
        print("[错误] --turns 必须 >= 1")
        return 2

    out_dir = Path(args.out)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = out_dir / f"run-{stamp}.jsonl"
    report_path = out_dir / f"report-{stamp}.md"

    cfg = EngineConfig.load()
    # 仅用于本地日志标识，不参与任何请求。不设的话 L1 闸门会判「未声明专用账号」。
    if not cfg.identity.account_label:
        cfg.identity.account_label = "提示词持久化实验"

    base_prompt = build_base_prompt(cfg)

    print("=" * 72)
    print("  提示词持久化实验 · 阶段 2")
    print("=" * 72)
    print(f"  传输档位   : {cfg.transport}")
    print(f"  模型       : {cfg.identity.model}")
    print(f"  分组       : {', '.join(groups)}")
    print(f"  每组轮数   : {args.turns}")
    print(f"  第1轮提示词: {len(base_prompt)} 字符")
    print(f"  日志       : {log_path}")
    print()
    print("  节奏（复用生产闸门，不自己造）:")
    print(f"    base_gap={cfg.pacing.base_gap_ms}ms sigma={cfg.pacing.sigma} "
          f"max_burst={cfg.pacing.max_burst} hold={cfg.pacing.hold_after_burst_ms}ms")
    print(f"    read_pause_every={cfg.pacing.read_pause_every} "
          f"hourly_max={cfg.pacing.hourly_max}")
    print(f"    PoW 预取={cfg.pow.prefetch} x{cfg.pow.prefetch_count}")
    print()
    print("  预估发送量:")
    for g in groups:
        print(f"    {g} 组: {args.turns} 次（1 次带提示词 + {args.turns - 1} 次工具结果）")
    print(f"    合计  : {len(groups) * args.turns} 次 completion 请求")
    print()
    print("  已知副作用：会话会留在账号的网页版历史里（temporary 尚未实现）。")
    print("  熔断策略：认证失败 / 账号被处置 / 服务端忙 / fatal → 立即停手，不重试。")
    print("=" * 72)

    # 真实发送闸门预检 —— 早失败，别等发出第一轮才发现一个请求都出不去。
    live_ok = os.environ.get(ALLOW_LIVE_ENV) == "1"
    if not live_ok:
        print()
        print(f"  [警告] {ALLOW_LIVE_ENV} 未设为 1 —— 真实发送会被传输层拒绝。")
        print("         这是 2026-10-17 封号事故后加的闸门，默认拒绝是刻意的。")
        print("         真要跑实验，先设这个变量（本脚本只做实验，不冒充产品外壳）：")
        print()
        print(f"           PowerShell : $env:{ALLOW_LIVE_ENV}='1'")
        print(f"           bash       : export {ALLOW_LIVE_ENV}=1")
        print()

    if args.dry_run:
        print("\n[dry-run] 未发送任何请求。去掉 --dry-run 才会真跑。")
        return 0

    if not live_ok:
        # 不在预检这里硬退 —— 用户可能用 `--dry-run` 之外的方式测试闸门行为。
        # 但要明确告知：这一轮不会产生任何有效数据。
        print(f"[错误] {ALLOW_LIVE_ENV} 未设为 1，实验无法进行。已中止，未发任何请求。")
        return 3

    log = RunLog(log_path)
    log.event("run_start", transport=cfg.transport, model=cfg.identity.model,
              groups=groups, turns=args.turns,
              base_prompt=base_prompt,
              pacing_cfg={
                  "base_gap_ms": cfg.pacing.base_gap_ms,
                  "sigma": cfg.pacing.sigma,
                  "max_burst": cfg.pacing.max_burst,
                  "hold_after_burst_ms": cfg.pacing.hold_after_burst_ms,
                  "read_pause_every": cfg.pacing.read_pause_every,
                  "hourly_max": cfg.pacing.hourly_max,
              },
              pow_cfg={"prefetch": cfg.pow.prefetch,
                       "count": cfg.pow.prefetch_count})

    transport = create_transport(cfg)
    try:
        await transport.start()
    except TransportError as exc:
        log.event("start_failed", error=str(exc)[:500], auth=exc.auth,
                  fatal=exc.fatal, suspended=exc.suspended)
        print(f"\n[错误] 传输层启动失败：{exc}")
        print("       若是 token 失效，先刷新凭据再跑。本脚本不打印 token 内容。")
        log.close()
        return 1
    except Exception as exc:  # noqa: BLE001 - 启动失败必须落日志，不能静默退出
        log.event("start_failed", error=f"{type(exc).__name__}: {exc}"[:500])
        print(f"\n[错误] 传输层启动异常：{type(exc).__name__}: {exc}")
        log.close()
        return 1

    # 闸门三件套 —— 与生产 `AgentRuntime` 用的是同一批类。
    pacing = PacingGate(cfg.pacing)
    breaker = CircuitBreaker(cfg.circuit)
    gates = GateChain(cfg, pacing, breaker, SessionManager(auto_rotate=False))

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_results: dict[str, list[dict]] = {}
    try:
        for group in groups:
            all_results[group] = await run_group(
                group=group, turns=args.turns, base_prompt=base_prompt,
                transport=transport, gates=gates, log=log,
            )
            # 两组之间也要过闸门 —— 不能因为「换组」就绕过节奏。
            if group != groups[-1]:
                print("\n  [组间] 等待闸门放行…", flush=True)
                await await_gate(gates, log)
    finally:
        with contextlib.suppress(Exception):
            await transport.stop()
        log.event("run_end", groups=list(all_results.keys()),
                  summary={g: [r.get("grade", "?") for r in rs]
                           for g, rs in all_results.items()})
        log.close()

    # ---- 报告 ----
    report = build_report(all_results, started=started,
                          transport=cfg.transport, model=cfg.identity.model,
                          log_path=log_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8", newline="\n")

    print("\n" + "=" * 72)
    print("  结果")
    print("=" * 72)
    for group, results in all_results.items():
        seq = " · ".join(f"{r.get('turn')}:{r.get('grade', '?')}" for r in results)
        print(f"  {group} 组：{seq}")
    print()
    print(f"  原始日志：{log_path}")
    print(f"  报告    ：{report_path}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
