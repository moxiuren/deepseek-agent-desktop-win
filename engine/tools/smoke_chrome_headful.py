"""headful Chrome 档位真实环境验证（不是离线单测）。

## 这个脚本回答什么

`test_*` 那一批全是离线测试 —— 它们证明不了下面任何一件事：

  1. Chrome 真的能被拉起来（路径探测、调试端口、CDP 连接）
  2. 窗口真的**看不见**（屏幕外 + SW_HIDE，且没有污染用户自己的 Chrome）
  3. 用的是**独立 profile**，不是用户日常那个（会被 SingletonLock 踢掉）
  4. 一轮真实对话能打通（token 注入、PoW、SSE 流、正文收全）
  5. `stop()` 之后进程树干净、临时 profile 被删

这五项恰好都是「环境因素」，而环境因素只有在真机上跑才会暴露。

## 代价与授权

会**真实发送一条极短消息**（「只回复两个字：收到」），消耗一次极小额度。
受 `GAD_ALLOW_LIVE_AUTH=1` 闸门约束 —— 本脚本自己设置它，因为跑这个脚本
本身就是一个明确的人工决定。

## 用法

    cd engine && ./.venv/Scripts/python.exe tools/smoke_chrome_headful.py

可选参数：
    --keep       结束后不 stop()，留着窗口给人工看一眼（调试用）
    --profile D  指定 profile 目录（默认走临时目录，验证「独立 profile」）
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.identity import load_token  # noqa: E402
from gad_engine.session import SessionMeta  # noqa: E402
from gad_engine.transport.chrome_cdp import ChromeCdpTransport  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" —— {detail}"
    print(line)
    return cond


# ---------- Win32 窗口观测 ----------


def _window_title(hwnd: int) -> str:
    n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def enum_windows(pid: int) -> list[dict]:
    """枚举**指定 pid** 的顶层窗口（含不可见的）。

    必须按 pid 过滤：`EnumWindows` 是全系统的，不过滤就会把用户自己开着的
    Chrome 一起数进来，结论完全失真。
    """
    user32 = ctypes.windll.user32
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            out.append(
                {
                    "hwnd": int(hwnd),
                    "visible": bool(user32.IsWindowVisible(hwnd)),
                    "title": _window_title(hwnd),
                    "rect": (rect.left, rect.top, rect.right, rect.bottom),
                }
            )
        return True

    user32.EnumWindows(_cb, 0)
    return out


def chrome_pids() -> set[int]:
    """**属于本脚本**的 chrome.exe pid 集合（按命令行里的 profile 归属判断）。

    ## 为什么不能用「启动前后 pid 差集」

    第一版就是那么写的，结果每次都在「无残留」这条上假失败：报出的 pid
    经查父进程是**用户日常 Chrome**，是用户自己浏览器新开的 renderer。
    差集测的是时序巧合，不是身份。

    真正能判定归属的是 Chrome 启动参数里的 `--user-data-dir=<我们的
    临时 profile>` —— 那是我们亲手塞进去的，用户进程不可能有。
    命令行只有 `Get-CimInstance` 给得到（`tasklist` 不给）。
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*gad-chrome-*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(x) for x in r.stdout.split() if x.isdigit()}


def pid_alive(pid: int) -> bool:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # 查不了就当它活着，宁可保守
    return str(pid) in r.stdout and "chrome.exe" in r.stdout.lower()


# ---------- 主流程 ----------


async def run(keep: bool, profile_dir: str) -> int:
    token = load_token(ROOT / ".token")
    if not check("engine/.token 可读且非空", bool(token), f"长度={len(token)}"):
        return 2

    # 基线：属于本引擎的 Chrome（正常应为空）。非空说明上一轮留了孤儿。
    before_pids = chrome_pids()
    print(f"  [信息] 启动前属于本引擎的 chrome.exe：{len(before_pids)} 个"
          + (f"（遗留：{sorted(before_pids)}）" if before_pids else ""))

    t = ChromeCdpTransport(
        token_provider=lambda: load_token(ROOT / ".token"),
        device_id_path=ROOT / ".device-id",
        # 显式写死这两个默认值：脚本要验证的正是「headful + 离屏」这条组合，
        # 不该因为将来默认值漂移而悄悄变成测别的路径。
        offscreen=True,
        headless=False,
        # 空闲看门狗关掉 —— 脚本跑完自己 stop()，不让后台任务干扰清理断言。
        idle_close_sec=0.0,
        profile_dir=profile_dir or None,
    )

    t_start = time.monotonic()
    try:
        await t.start()
    except Exception as exc:  # noqa: BLE001
        check("start() 成功", False, f"{type(exc).__name__}: {exc}")
        return 2
    launch_ms = (time.monotonic() - t_start) * 1000.0
    check("start() 成功", True, f"{launch_ms:.0f} ms")

    pid = t._proc.pid if t._proc is not None else 0
    port = t._port
    check("Chrome 进程存在", pid > 0 and pid_alive(pid), f"pid={pid}")
    check("调试端口已分配", port > 0, f"port={port}")
    check(
        "Chrome 是新增进程（没复用用户的）",
        pid not in before_pids,
        f"pid={pid} 不在启动前的 {len(before_pids)} 个里",
    )

    # ---- 独立 profile ----
    prof = t._tmp_profile or t._profile_dir or ""
    check("使用独立 profile", bool(prof), prof or "（空）")
    if prof:
        check("profile 目录存在", Path(prof).is_dir(), prof)
        user_data = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
        check(
            "不是用户日常 profile",
            Path(prof).resolve() != user_data.resolve(),
            f"用户日常 = {user_data}",
        )

    # ---- 窗口可见性 ----
    wins = enum_windows(pid)
    visible = [w for w in wins if w["visible"]]
    print(f"  [信息] 本进程顶层窗口 {len(wins)} 个，其中可见 {len(visible)} 个")
    for w in wins:
        print(f"         hwnd={w['hwnd']} visible={w['visible']} rect={w['rect']} title={w['title']!r}")

    check("窗口已隐藏（SW_HIDE 生效）", len(visible) == 0, f"可见窗口 {len(visible)} 个")
    # 注意：Chrome **不会**照搬 `--window-position=-32000,-32000`，实测会把它
    # 钳到约 -21845（负坐标，仍在所有显示器之外）。所以判据是「左上角为负」，
    # 不是「等于 -32000」—— 后者会把一个正常工作误判成失败。
    offscreen_rects = [w for w in wins if w["rect"][0] < 0 or w["rect"][1] < 0]
    check(
        "窗口坐标在屏幕外（或已被隐藏）",
        bool(offscreen_rects) or len(visible) == 0,
        f"屏幕外窗口 {len(offscreen_rects)} 个"
        + (f"，首个 rect={offscreen_rects[0]['rect']}" if offscreen_rects else ""),
    )

    # 窗口创建可能晚于 CDP target —— 等 2 秒再看一眼，
    # 若此时冒出可见窗口，说明 `_hide_windows()` 存在时序缺口（真实缺陷）。
    await asyncio.sleep(2.0)
    late_visible = [w for w in enum_windows(pid) if w["visible"]]
    check("2 秒后仍无可见窗口（无时序缺口）", len(late_visible) == 0, f"可见 {len(late_visible)} 个")

    # ---- 健康检查 ----
    st = await t.health()
    check("health() reachable", st.reachable, st.note or st.account_status_text)
    if st.reachable:
        print(f"  [信息] 账号状态：{st.account_status_short} / {st.account_status_text}")

    # ---- 一轮真实对话 ----
    deltas: list[str] = []
    delta_times: list[float] = []

    def on_delta(text: str) -> None:
        deltas.append(text)
        delta_times.append(time.monotonic())

    # 提示词**必须让回答足够长**：第一版用的是「只回复两个字：收到」，
    # 服务端一次就把整段推完，只产生 1 帧 —— 于是「流式是否被隐藏窗口节流」
    # 这个断言根本无从判断（1 帧无法区分「流式正常」与「一次性推完」）。
    # 换成 40 个数字，输出约 100+ 字符，必然分多帧。
    prompt = "请输出 1 到 40 的整数，用英文逗号分隔。只输出数字，不要任何解释。"
    session = SessionMeta(temporary=True)
    t_send = time.monotonic()
    try:
        outcome = await asyncio.wait_for(
            t.send(prompt, session, temporary=True, on_delta=on_delta),
            timeout=180.0,
        )
    except Exception as exc:  # noqa: BLE001
        check("真实对话打通", False, f"{type(exc).__name__}: {exc}")
        outcome = None
    send_ms = (time.monotonic() - t_send) * 1000.0

    if outcome is not None:
        text = (outcome.text or "").strip()
        check("真实对话打通", bool(text), f"{send_ms:.0f} ms，正文 {len(text)} 字：{text[:80]!r}")
        check("拿到 response id（可续接）", bool(outcome.rid), f"rid={outcome.rid!r}")
        check("会话已建立", bool(session.cid), f"cid={session.cid!r}")
        print(
            f"  [信息] PoW：cached={outcome.pow_cached} mode={outcome.pow_mode} "
            f"workers={outcome.pow_workers} pow_ms={outcome.pow_ms}"
        )
        # 把页面自己看到的可见性状态也打出来 —— 这是「窗口确实不可见」
        # 在**页面内部**的旁证（Win32 侧看到的是窗口属性，这里看到的是
        # 浏览器自己认定的状态，两者不一致就说明 SW_HIDE 没作用到页面）。
        vis = ""
        if t._cdp is not None:
            try:
                vis = str(await t._cdp.eval("document.visibilityState"))
            except Exception:  # noqa: BLE001
                vis = "（读取失败）"
        if vis:
            print(f"  [信息] 页面内 document.visibilityState = {vis}")
            check("页面内部也认定不可见", vis in ("hidden", "prerender"), f"visibilityState={vis}")

        if delta_times:
            span = (delta_times[-1] - delta_times[0]) * 1000.0
            print(f"  [信息] 流式帧 {len(deltas)} 个，跨度 {span:.0f} ms")
            check(
                "隐藏窗口下流式仍在推送（≥2 帧）",
                len(deltas) >= 2,
                f"{len(deltas)} 帧 / {span:.0f} ms",
            )
        else:
            check("隐藏窗口下流式仍在推送（≥2 帧）", False, "一帧都没收到")

    # ---- 清理 ----
    if keep:
        print("  [信息] --keep：跳过 stop()，窗口留着给人工确认")
        return 0 if not FAIL else 1

    t_stop = time.monotonic()
    await t.stop()
    stop_ms = (time.monotonic() - t_stop) * 1000.0
    print(f"  [信息] stop() 耗时 {stop_ms:.0f} ms")

    check("主进程已退出", not pid_alive(pid), f"pid={pid}")
    # 判据是「还有没有命令行带 gad-chrome- 的进程」，不是 pid 差集。
    leftover = chrome_pids()
    check(
        "无残留 Chrome 进程（子进程一并清理）",
        len(leftover) == 0,
        f"仍存活 {len(leftover)} 个：{sorted(leftover)[:8]}",
    )
    if prof and t._tmp_profile is None:
        check("临时 profile 已删除", not Path(prof).exists(), prof)

    return 0 if not FAIL else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="不 stop()，留着窗口人工看")
    ap.add_argument("--profile", default="", help="指定 profile 目录（默认临时目录）")
    args = ap.parse_args()

    # 真实发送闸门：本脚本的存在本身就是一次人工决定，所以在这里显式打开。
    os.environ["GAD_ALLOW_LIVE_AUTH"] = "1"

    print("=" * 68)
    print("headful Chrome 档位 · 真实环境验证")
    print("  会真实发送一条极短消息（消耗一次极小额度）")
    print("=" * 68)

    code = asyncio.run(run(args.keep, args.profile))

    print("=" * 68)
    print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    for name in FAIL:
        print(f"  [失败] {name}")
    print("=" * 68)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
