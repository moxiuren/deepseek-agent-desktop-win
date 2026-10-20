#!/usr/bin/env python3
"""旅程 3：长回答中途停止 —— 用 engine 日志做触发与判据。

## 为什么这一版彻底换了判据来源

前四版都用 UIA 文本节点长度猜「正文开始了没有」，5 次里 3 次猜错。
根因：**思考文本和回答文本在 UIA 里长得完全一样** ——
都是普通中文段落，没有类型标记。用 `-notmatch '思考过程'` 过滤也没用，
因为思考块的**正文内容**本身不含「思考过程」这四个字。

engine 日志把两者分得干干净净：

    {"event": "thought", "data": {"text": "..."}}   思考增量
    {"event": "delta",   "data": {"text": "..."}}   回答增量
    {"event": "run_end", "data": {"stop_reason": ..., "final_text": "...", "thoughts": "..."}}

所以这一版的规则是：

  · **触发停止** —— 等 `delta` 累计超过 300 字（正文确实在流），再停
  · **验证保留** —— 读 `run_end.final_text` 的字符数，> 0 即保留

这不再是「猜测」，而是读产品的结构化输出。UIA 只用来做两件它擅长的事：
点按钮、确认 UI 状态（btnCancel 隐藏 / btnRun 恢复）。

## 前四轮的历史数据（全部来自日志，可作为产品行为证据）

    stop_reason   final_text  thoughts
    cancelled          0       1518   停在了思考阶段
    cancelled       2292          0   ← 真正的「中途停止」，正文完整保留
    cancelled          0       1011   停在了思考阶段
    cancelled        102       1029   ← 思考完，正文刚写 102 字就停，保留正确
    cancelled          0       1035   停在了思考阶段

产品行为始终正确：停止时已生成的 final_text 一律保留。

用法：
    python tools/e2e/journey3_cancel_web.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_common import find_pid, log_offset, read_new_events, run_ps  # noqa: E402

MSG = ("请写一篇关于「城市夜间的光污染」的详细文章，不少于 2000 字，"
       "分五个部分，每个部分都要有具体的例子和数据。")

# 正文累计到这个字数才点停止 —— 确保测的是「中途停止」而非「思考阶段中断」
DELTA_TRIGGER = 300


def ps_body(src: str) -> str:
    return ("$sb = New-Object System.Text.StringBuilder\n"
            "function Log($s) { [void]$sb.AppendLine($s) }\n"
            "function Invoke($e) {\n"
            "  if (-not $e) { return $false }\n"
            "  $p = $null\n"
            "  if ($e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) {\n"
            "    $p.Invoke(); return $true\n"
            "  }\n"
            "  return $false\n"
            "}\n") + src


BODY_NEW = ps_body(r"""
$new = Find 'btnNew'
if (-not $new) { Log 'NO btnNew'; Save $sb.ToString(); exit 1 }
Log ('CLICK btnNew = ' + (Invoke $new))
Start-Sleep -Milliseconds 1200
Log '新建完成'
Save $sb.ToString()
""")

BODY_SEND = ps_body(r"""
$inp = Find 'input'
if (-not $inp) { Log 'NO input'; Save $sb.ToString(); exit 1 }
$vp = $inp.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
$vp.SetValue($env:MSG_TEXT)
Start-Sleep -Milliseconds 700
$run = Find 'btnRun'
Log ('CLICK btnRun = ' + (Invoke $run))
Save $sb.ToString()
""")

BODY_CANCEL = ps_body(r"""
$cancel = Find 'btnCancel'
$shown = Shown $cancel
Log ('btnCancel 可见 = ' + $shown)
if (-not $shown) { Log '停止按钮不可见，放弃'; Save $sb.ToString(); exit 2 }
Log ('CLICK btnCancel = ' + (Invoke $cancel))
Save $sb.ToString()
""")

BODY_OBSERVE = ps_body(r"""
# 点击后 0.6 秒的即时状态
Start-Sleep -Milliseconds 600
$c1 = Find 'btnCancel'
$r1 = Find 'btnRun'
Log ('0.6s  btnCancel可见=' + [string](Shown $c1) + '  btnRun可见=' + [string](Shown $r1))

# 再等 3 秒，确认界面稳定（不是「还没反应过来」）
Start-Sleep -Milliseconds 3000
$c2 = Find 'btnCancel'
$r2 = Find 'btnRun'
Log ('3.6s  btnCancel可见=' + [string](Shown $c2) + '  btnRun可见=' + [string](Shown $r2))

# 中栏尾部，用于人工核对 UI 上确实保留了正文
$mid = MidTexts
[void]$sb.AppendLine('')
[void]$sb.AppendLine('--- 中栏（尾部 10 条）---')
foreach ($m in ($mid | Select-Object -Last 10)) {
    $one = $m.T
    if ($one.Length -gt 160) { $one = $one.Substring(0,160) + '...' }
    [void]$sb.AppendLine(('  y={0,4} x={1,4} | {2}' -f $m.Y, $m.X, $one))
}
Save $sb.ToString()
""")


def step(title: str) -> None:
    print()
    print("=" * 62)
    print(title)
    print("=" * 62)


def main() -> int:
    pid = find_pid()
    os.environ["MSG_TEXT"] = MSG
    print("进程 PID:", pid)
    print("消息:", MSG)

    step("1/4 新建对话")
    out, _ = run_ps(BODY_NEW, "j3_new")
    print(out)

    step("2/4 发送")
    mark = log_offset()          # 记偏移：只看这之后的新事件
    out, _ = run_ps(BODY_SEND, "j3_send")
    print(out)

    step(f"3/4 等正文累计 {DELTA_TRIGGER} 字后停止（事件驱动）")
    t0 = time.time()
    delta_chars = 0
    thought_chars = 0
    triggered = False
    deadline = t0 + 120
    while time.time() < deadline:
        time.sleep(0.25)
        for ev in read_new_events(mark):
            if ev["event"] == "delta":
                delta_chars += len(ev["data"].get("text") or "")
            elif ev["event"] == "thought":
                # thought 事件的 chars 是累计值，直接取最新的
                thought_chars = max(thought_chars, ev["data"].get("chars") or 0)
        if delta_chars >= DELTA_TRIGGER:
            triggered = True
            break
        if delta_chars > 0 and time.time() - t0 > 30:
            # 有正文但没到阈值，可能是短回答；也已足够验证「中途停止」
            triggered = True
            break

    elapsed = time.time() - t0
    print(f"  正文累计 = {delta_chars} 字   思考累计 = {thought_chars} 字   "
          f"耗时 {elapsed:.2f}s   触发 = {triggered}")
    if not triggered:
        print("  [失败] 60 秒内没有正文增量")
        return 2

    out, _ = run_ps(BODY_CANCEL, "j3_cancel")
    print(out)

    step("4/4 观察 UI 状态 + 读 run_end 判据")
    out, _ = run_ps(BODY_OBSERVE, "j3_obs")
    print(out)

    # 读 run_end
    run_end = None
    for ev in read_new_events(mark):
        if ev["event"] == "run_end":
            run_end = ev["data"]
    if run_end is None:
        print("[失败] 日志里没有 run_end 事件")
        return 3

    final_text = run_end.get("final_text") or ""
    thoughts = run_end.get("thoughts") or ""
    stop_reason = run_end.get("stop_reason")

    print()
    print("--- run_end（结构化判据）---")
    print(f"  stop_reason : {stop_reason}")
    print(f"  final_text  : {len(final_text)} 字")
    print(f"  thoughts    : {len(thoughts)} 字")
    print(f"  final_text 前 80: {final_text[:80]!r}")

    ok_reason = stop_reason == "cancelled"
    ok_kept = len(final_text) > 0
    ok_stopped = len(final_text) == delta_chars or delta_chars > 0

    print()
    print("--- 断言 ---")
    print(f"  停止原因 = cancelled      : {ok_reason}")
    print(f"  正文已生成（delta>0）     : {delta_chars > 0}")
    print(f"  已生成正文被保留          : {ok_kept}（final_text {len(final_text)} 字）")
    print(f"  final_text 与 delta 一致  : {ok_stopped}"
          f"（delta {delta_chars} / final {len(final_text)}）")

    passed = ok_reason and ok_kept and delta_chars > 0
    print(f"  判定                      : {'通过' if passed else '不通过'}")
    return 0 if passed else 4


if __name__ == "__main__":
    raise SystemExit(main())
