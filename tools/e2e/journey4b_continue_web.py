#!/usr/bin/env python3
"""旅程 4 后半段：接管之后继续发消息 —— 验证「续接锚点」真的生效。

## 这一步要证伪的失败模式

接管只做对了一半的样子很像成功：历史拉回来了、界面显示正常，但
**锚点没校正**。下一轮发消息时，`parent_message_id` 用的是本地那个
毫无意义的 `rid`，服务端把它当成新的 root —— 结果是：

  · 请求成功（HTTP 200，无报错）
  · 模型却在**空白上下文**里回答

用户看到的是「它突然忘了我刚才说的」，而日志里没有任何异常。
所以判据不能只看「发成功了」，必须看**会话身份有没有漂移**：

  · `cid`（chat_session_id）必须**不变** —— 变了就是被轮换进新会话
  · `rid`（parent_message_id）必须**变** —— 每轮服务端都会回新的
  · `turns` 必须**递增**
  · `auto_rotate` 必须是 `False` —— 接管的会话不自动轮换
  · 模型回答里要**引用得上历史内容** —— 这条是「上下文真的在」的直接证据

## 2026-10-18 的假失败（必须记住）

第一版报「cid 从 94399d4a 变成 bc4f11ef，会话漂移」，**结论是错的**。
根因：`latest_log()` 按 mtime 选日志，而**当时 WPF 壳与 Web 壳同时在跑**，
各有一个 gad_engine、各写一份日志，两个文件 mtime 每秒交错。

于是基线读的是 WPF 引擎的会话，对照读的是 Web 引擎的会话 —— 两个不同的
产品实例，当然「cid 变了」。消息其实发对了，回答也引用对了历史文件名。

修法：`web_common.engine_log_for(shell_pid)` 按进程父子关系绑定日志。
**教训与旅程 2 的假阴性同源：判据错了，产品就被冤枉成坏的。**

用法：
    python tools/e2e/journey4b_continue_web.py            # 真发一条
    python tools/e2e/journey4b_continue_web.py --replay   # 只回放已有日志，不发消息
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from web_common import (engine_log_for, find_pid, log_offset_for,  # noqa: E402
                        read_new_events, run_ps)

MSG = "用一句话总结：你刚才读到的三个文件分别叫什么名字？"

# 历史里那三个文件名 —— 模型只有真的看到上下文才答得出来
EXPECT_NAMES = ("a.txt", "b.txt", "c.txt")


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


def scan_sessions(path: Path, start: int = 0) -> list[tuple[int, dict]]:
    """从 start 字节起扫描，返回 [(行号, session 对象), ...]，按出现顺序。"""
    with path.open("rb") as f:
        f.seek(start)
        raw = f.read().decode("utf-8", errors="replace")
    out = []
    for n, line in enumerate(raw.splitlines()):
        i = line.find('{"id"')
        if i < 0:
            continue
        try:
            d = json.loads(line[i:])
        except Exception:
            continue
        r = d.get("result")
        if isinstance(r, dict) and isinstance(r.get("session"), dict):
            out.append((n, r["session"]))
    return out


def scan_run_ends(path: Path, start: int = 0) -> list[dict]:
    with path.open("rb") as f:
        f.seek(start)
        raw = f.read().decode("utf-8", errors="replace")
    out = []
    for line in raw.splitlines():
        j = line.find('{"type": "event"')
        if j < 0:
            continue
        try:
            e = json.loads(line[j:])
        except Exception:
            continue
        if e.get("event") == "run_end":
            out.append(e.get("data") or {})
    return out


def judge(base: dict, after: dict, final_text: str) -> bool:
    ok_cid = after.get("cid") == base.get("cid")
    ok_rid = after.get("rid") != base.get("rid")
    ok_turns = (after.get("turns") or 0) > (base.get("turns") or 0)
    ok_norotate = after.get("auto_rotate") is False
    ok_attached = after.get("attached") is True
    ok_ctx = all(n in final_text for n in EXPECT_NAMES)

    print()
    print("--- 断言 ---")
    print(f"  cid 未变（没被轮换进新会话） : {ok_cid}")
    print(f"  rid 已更新（锚点前进）       : {ok_rid}"
          f"（{base.get('rid')} → {after.get('rid')}）")
    print(f"  turns 递增                   : {ok_turns}"
          f"（{base.get('turns')} → {after.get('turns')}）")
    print(f"  attached 保持                : {ok_attached}")
    print(f"  auto_rotate=False            : {ok_norotate}")
    print(f"  回答引用了历史文件名         : {ok_ctx}")
    passed = all((ok_cid, ok_rid, ok_turns, ok_norotate, ok_attached, ok_ctx))
    print(f"  判定                         : {'通过' if passed else '不通过'}")
    return passed


def step(title: str) -> None:
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def replay(path: Path) -> int:
    """不发消息，只用已有日志回放判据 —— 用于「已经发过一条」的场景。"""
    print("=== 回放模式（不发消息）===")
    print("日志:", path.name)
    sess = scan_sessions(path)
    runs = scan_run_ends(path)

    # 基线要取「**当前**这条会话」的最早状态，不是日志里第一个接管过的会话 ——
    # 同一个引擎可能先后接管过多条（本日志里就有 94399d4a 和 bc4f11ef 两条），
    # 拿错基线会把「换了一条会话」误判成「会话漂移」。
    after = sess[-1][1]
    cur = after.get("cid")
    first = next((s for _, s in sess if s.get("cid") == cur), None)
    if first is None or first.get("attached") is not True:
        print(f"[失败] 当前会话 {cur} 不是接管来的")
        return 1
    base = first
    final_text = runs[-1].get("final_text") if runs else ""

    print(f"\n接管点（基线）: cid={base.get('cid')} rid={base.get('rid')} "
          f"turns={base.get('turns')} attached={base.get('attached')}")
    print(f"最新状态      : cid={after.get('cid')} rid={after.get('rid')} "
          f"turns={after.get('turns')} attached={after.get('attached')}")
    print(f"最后一次 run_end: stop_reason={runs[-1].get('stop_reason') if runs else '?'} "
          f"final_text={len(final_text)} 字")
    print(f"final_text: {final_text[:220]!r}")
    return 0 if judge(base, after, final_text) else 2


def main() -> int:
    pid = find_pid()
    log, engine_pid = engine_log_for(pid)
    print("进程 PID:", pid, "（Web 壳）   引擎 PID:", engine_pid)
    print("引擎日志:", log.name)

    if "--replay" in sys.argv:
        return replay(log)

    os.environ["MSG_TEXT"] = MSG
    print("消息:", MSG)

    step("1/4 发送前的会话身份（基线）")
    sess = scan_sessions(log)
    if not sess:
        print("[失败] 日志里读不到任何 session")
        return 1
    base = sess[-1][1]
    print(f"  cid={base.get('cid')}  rid={base.get('rid')}  turns={base.get('turns')}")
    print(f"  attached={base.get('attached')}  auto_rotate={base.get('auto_rotate')}")
    if base.get("attached") is not True:
        print("[失败] 当前会话不是接管来的 —— 请先跑 journey4_attach_web.py")
        return 1

    step("2/4 发送")
    mark = log_offset_for(pid)
    out, _ = run_ps(BODY_SEND, "j4b_send")
    print(out)

    step("3/4 等 run_end")
    run_end = None
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(0.4)
        for ev in read_new_events(mark):
            if ev["event"] == "run_end":
                run_end = ev["data"]
        if run_end:
            break

    if not run_end:
        print("[失败] 180 秒内没有 run_end")
        return 2

    final_text = run_end.get("final_text") or ""
    print("--- run_end ---")
    print(f"  stop_reason : {run_end.get('stop_reason')}")
    print(f"  turns       : {run_end.get('turns')}")
    print(f"  final_text  : {len(final_text)} 字")
    print(f"  前 220 字   : {final_text[:220]!r}")

    step("4/4 发送后的会话身份（对照）")
    time.sleep(1.0)
    after = scan_sessions(log, mark[1])[-1][1]
    print(f"  cid={after.get('cid')}  rid={after.get('rid')}  turns={after.get('turns')}")
    print(f"  attached={after.get('attached')}  auto_rotate={after.get('auto_rotate')}")

    return 0 if judge(base, after, final_text) else 4


if __name__ == "__main__":
    raise SystemExit(main())
