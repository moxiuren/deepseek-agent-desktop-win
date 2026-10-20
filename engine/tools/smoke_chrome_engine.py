"""引擎层 + headful Chrome 档位 · 真实环境验证。

## 与 `smoke_chrome_headful.py` 的分工

那个脚本直接实例化 `ChromeCdpTransport`，验的是**传输层**。
这个脚本起的是**真引擎子进程**（`python -m gad_engine`），验的是
「档位换成 chrome 之后，整条链路还成不成立」：

  1. `create_transport()` 真的按 `transport: "chrome"` 装配了浏览器档位
  2. 引擎 `status` 里能看到 chrome_pid / debug_port（说明后台状态接得上）
  3. 一轮**带工具调用**的真实对话能跑通 —— 这是引擎层的核心能力，
     单测传输层时走不到（`local_cmd` 的执行在 `executor` 里）
  4. 会话续接（第二轮带 parent_message_id）
  5. `shutdown` 之后 Chrome 进程树干净

## 为什么必须用临时 LOCALAPPDATA

引擎启动会写 config / 对话 / 记忆。用临时目录隔离，跑完即删 ——
不碰用户真实的对话历史与配置。token 例外：**只读**复用 `engine/.token`，
因为浏览器档位不负责首次登录（见 `ChromeCdpTransport.start()` 的报错文案）。

## 代价与授权

真实发送 2 条消息（一条带工具调用，一条短续接）。受 `GAD_ALLOW_LIVE_AUTH`
闸门约束，本脚本自己设置 —— 跑它本身就是一次人工决定。

## 用法

    cd engine && ./.venv/Scripts/python.exe tools/smoke_chrome_engine.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
PY = str(Path(sys.executable))

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


def our_chrome_pids(profile_marker: str = "gad-chrome-") -> set[int]:
    """**属于本引擎**的 chrome.exe pid 集合。

    判据是命令行里的 `--user-data-dir=...\\Temp\\gad-chrome-xxxx`，
    不是「启动前后 pid 的差集」。后者会误判：用户自己的 Chrome 随时会新开
    renderer 进程（实测 2026-10-18 一次跑里就冒出一个，父进程是用户日常
    Chrome），diff 会把它算成「我们的残留」。

    命令行只有 `Get-CimInstance` 拿得到（`tasklist` 不给）。它慢一点，
    但这条断言本来就是收尾时跑一次。
    """
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + profile_marker + "*' } | "
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


class Engine:
    """最小 RPC 客户端 —— 与 `smoke_live.py` 同形，但把事件按 kind 归好。"""

    def __init__(self, env: dict) -> None:
        self.proc = subprocess.Popen(
            [PY, "-m", "gad_engine"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        self._id = 0
        self.events: list[dict] = []
        self.stderr_lines: list[str] = []
        # stderr 必须持续排空 —— 管道写满后引擎会阻塞在写上，
        # 表现是「既不完成也不报错」（见 smoke_live.py 同名注释）。
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr
        try:
            for line in self.proc.stderr:
                self.stderr_lines.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    def _read_until(self, msg_id: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        assert self.proc.stdout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("引擎已退出")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "event":
                self.events.append(msg)
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError(f"id={msg_id} 超时")

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        payload: dict = {"id": self._id, "method": method}
        if params:
            payload["params"] = params
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return self._read_until(self._id, timeout)

    def mark(self) -> int:
        """当前已收事件的条数 —— 作为 `wait_event` 的下界。

        **没有它就会拿到上一轮的旧事件**：`run.async` 第二轮等
        `run.completed` 时，第一轮那条还在 `self.events` 里，
        于是立刻命中、立刻返回，看起来「0.1 秒就跑完了」。
        """
        return len(self.events)

    def wait_event(self, kind: str, timeout: float, since: int = 0) -> dict | None:
        """等某个事件。只认 `since` 之后收到的事件。"""
        deadline = time.monotonic() + timeout
        assert self.proc.stdout
        while time.monotonic() < deadline:
            for ev in self.events[since:]:
                if ev.get("event") == kind:
                    return ev
            line = self.proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "event":
                self.events.append(msg)
        return None

    def close(self) -> None:
        # 关闭阶段不抛 —— 脚本已经在收尾，这里再抛只会掩盖真正的失败项。
        with contextlib.suppress(Exception):
            self.call("shutdown", timeout=20)
        try:
            self.proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def main() -> int:
    print("=" * 72)
    print("引擎层 · headful Chrome 档位真实环境验证")
    print("  会真实发送 2 条消息（含一次工具调用）")
    print("=" * 72)

    # 基线用**归属判据**取：命令行里带 `gad-chrome-` 的 chrome.exe。
    # 正常情况这个集合是空的；非空说明上一轮留了孤儿 —— 那本身就是
    # 一条值得看见的信息，所以不静默丢弃，打印出来。
    before = our_chrome_pids()
    print(f"  [信息] 启动前属于本引擎的 chrome.exe：{len(before)} 个"
          + (f"（遗留：{sorted(before)}）" if before else ""))

    with tempfile.TemporaryDirectory(prefix="gad-chrome-engine-") as tmp:
        appdir = Path(tmp) / "DeepSeek-Agent"
        appdir.mkdir(parents=True, exist_ok=True)
        cfg_path = appdir / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    # ★ 本脚本验证的核心：档位真的是 chrome，不是 rpc。
                    "transport": "chrome",
                    "identity": {
                        "account_label": "smoke-chrome-engine",
                        # token 只读复用项目里的那一份；临时目录里没有 token，
                        # 浏览器档位也不会去弹登录窗（它明确不负责首次登录）。
                        "token_file": str(ROOT / ".token"),
                    },
                    "max_tool_turns": 4,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["LOCALAPPDATA"] = tmp
        env["APPDATA"] = tmp
        # 配置路径走环境变量，而不是靠 appdir 的位置推断 ——
        # 这是 `default_config_path()` 明确支持的覆盖口。
        env["GAD_CONFIG_PATH"] = str(cfg_path)
        env["GAD_ALLOW_LIVE_AUTH"] = "1"

        eng = Engine(env)
        try:
            print("\n[1] 引擎启动与档位装配")
            init = eng.call("initialize")
            check(
                "引擎报告 transport=chrome",
                init.get("transport") == "chrome",
                f"transport={init.get('transport')!r}",
            )
            ident = init.get("identity") or {}
            check(
                "token 可读（浏览器档位不负责首次登录）",
                bool(ident.get("has_token")),
                f"token_len={ident.get('token_len')}",
            )

            print("\n[2] 启动运行时（transport.start() 会真拉 Chrome）")
            # **必须先显式 start()**：`status` 只读快照，不会触发启动；
            # 只有 `run()` 才会在内部自动 start。这里先起，才能单独验证
            # 「Chrome 起没起来、pid/端口有没有暴露给外壳」这件事本身，
            # 而不是把它和「对话能不能跑通」混在一次断言里。
            t_start = time.monotonic()
            eng.call("start", timeout=120)
            start_ms = (time.monotonic() - t_start) * 1000.0
            check("start() 完成", True, f"{start_ms:.0f} ms")

            # 传输层起不来时 `start` 不抛（见 agent.start 的 docstring），
            # 而是把原因记进 `_start_error` —— 所以这里必须查一次。
            #
            # `_emit_event` 是 `create_task` 派出去的，**不保证在 `start`
            # 返回之前就写到 stdout**。所以要么等事件、要么等一小会儿再读；
            # 直接读 `eng.events` 会拿到空（第一次跑就是这么误判的）。
            ready = eng.wait_event("ready", timeout=5.0)
            ready_ev = (ready or {}).get("data") or {}
            check(
                "ready 事件报告未降级",
                bool(ready_ev) and not ready_ev.get("degraded"),
                json.dumps(ready_ev, ensure_ascii=False)[:240],
            )
            for ev in eng.events:
                if ev.get("event") == "transport_unavailable":
                    print("     [错误] " + json.dumps(ev.get("data"), ensure_ascii=False)[:400])

            st = eng.call("status", timeout=60)
            tr = st.get("transport") or {}
            bg = tr.get("background") or {}
            check(
                "transport.name 是 chrome-cdp",
                tr.get("name") == "chrome-cdp",
                str(tr.get("name")),
            )
            pid = int(bg.get("chrome_pid") or 0)
            port = int(bg.get("debug_port") or 0)
            check("status 里有 chrome_pid", pid > 0, f"pid={pid}")
            check("status 里有 debug_port", port > 0, f"port={port}")
            check("新起的 Chrome（没复用用户的）", pid > 0 and pid not in before, f"pid={pid}")
            check("引擎侧报告 offscreen", bool(bg.get("offscreen")), json.dumps(bg, ensure_ascii=False)[:200])
            check("引擎侧报告非 headless", bg.get("headless") is False, f"headless={bg.get('headless')}")

            print("\n[3] 一轮带工具调用的真实对话")
            # 让它跑一条命令并把输出念回来 —— 这一步同时验证：
            # 模型能吐 ```local_cmd``` 围栏、executor 能执行、结果能回灌给模型。
            goal = (
                "请用 local_cmd 工具执行 `echo GAD_CHROME_OK`，"
                "然后把命令的原始输出原样复述给我，不要加解释。"
            )
            t0 = time.monotonic()
            started = eng.call("run.async", {"goal": goal, "max_turns": 4})
            check("run.async 已受理", started.get("started") is True, str(started))
            done = eng.wait_event("run.completed", timeout=240)
            failed = None if done else eng.wait_event("run.failed", timeout=5)
            elapsed = time.monotonic() - t0
            if not check(
                "收到 run.completed",
                done is not None,
                f"{elapsed:.1f}s；failed={str(failed)[:300]}" if failed else f"{elapsed:.1f}s",
            ):
                print("     事件序列：")
                for e in eng.events[-25:]:
                    print("       " + json.dumps(e, ensure_ascii=False)[:260])
                print("     stderr 末 40 行：")
                for line in eng.stderr_lines[-40:]:
                    print(f"       {line}")
                return 1

            result = (done.get("data") or done.get("result") or {})
            report = result.get("report") if isinstance(result.get("report"), dict) else result
            final = str(report.get("final_text") or "")
            execs = report.get("executions") or []
            check("工具调用真的发生了", len(execs) >= 1, f"{len(execs)} 次")
            if execs:
                e0 = execs[0]
                check(
                    "工具是 local_cmd 且成功",
                    str(e0.get("kind")) == "local_cmd" and e0.get("exit_code") == 0,
                    f"kind={e0.get('kind')} exit={e0.get('exit_code')}",
                )
                check(
                    "命令输出里有 GAD_CHROME_OK",
                    "GAD_CHROME_OK" in str(e0.get("output_preview") or ""),
                    str(e0.get("output_preview"))[:120],
                )
            check(
                "模型把输出复述回来了",
                "GAD_CHROME_OK" in final,
                f"final_text={final[:160]!r}",
            )
            print(f"  [信息] 轮次={report.get('turns')} 工具={report.get('tool_calls')} "
                  f"stop_reason={report.get('stop_reason')} 用时={elapsed:.1f}s")

            print("\n[4] 会话续接（第二轮带 parent_message_id）")
            # 记住事件游标 —— 否则会立刻命中上一轮那条 `run.completed`。
            mark2 = eng.mark()
            t1 = time.monotonic()
            started2 = eng.call("run.async", {"goal": "只回复两个字：继续", "max_turns": 2})
            check("第二轮已受理", started2.get("started") is True, str(started2))
            done2 = eng.wait_event("run.completed", timeout=180, since=mark2)
            elapsed2 = time.monotonic() - t1
            if check("第二轮完成", done2 is not None, f"{elapsed2:.1f}s"):
                conv = eng.call("conversation.current")
                msgs = conv.get("messages") or []
                check("对话里累计 4 条消息", len(msgs) == 4, f"{len(msgs)} 条")
                sess = conv.get("session") or {}
                check("会话三元组已落盘", bool(sess.get("cid")), str(sess)[:160])

            print("\n[5] 环境因素：窗口仍然不可见")
            # 直接问引擎侧拿 pid，再用 Win32 复核 —— 引擎自己说「隐藏了」不算数。
            st2 = eng.call("status", timeout=60)
            bg2 = (st2.get("transport") or {}).get("background") or {}
            pid2 = int(bg2.get("chrome_pid") or 0)
            visible = _visible_windows(pid2) if pid2 else -1
            check("Chrome 进程仍存活", pid2 > 0, f"pid={pid2}")
            check("仍然 0 个可见窗口", visible == 0, f"可见 {visible} 个")

        finally:
            print("\n[6] 关闭与清理")
            eng.close()
            # 给 job 的 TerminateJobObject 一点时间落地（内核回收是异步的）。
            time.sleep(1.5)
            # 判据是「命令行里还带 gad-chrome- 的进程」——
            # 不看 pid 差集，理由见 `our_chrome_pids()` 的 docstring。
            leftover = our_chrome_pids()
            check(
                "无残留 Chrome 进程（含孤儿兜底）",
                len(leftover) == 0,
                f"仍存活 {len(leftover)} 个：{sorted(leftover)[:8]}",
            )

    print("\n" + "-" * 72)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    for name in FAIL:
        print(f"    [失败] {name}")
    print("-" * 72)
    return 1 if FAIL else 0


def _visible_windows(pid: int) -> int:
    """数指定 pid 的可见顶层窗口。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    count = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        nonlocal count
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            count += 1
        return True

    user32.EnumWindows(_cb, 0)
    return count


if __name__ == "__main__":
    raise SystemExit(main())
