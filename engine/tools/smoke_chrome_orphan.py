"""孤儿 Chrome 回收验证 —— 只测「父进程被强杀」这一条路径。

## 为什么需要单独一个脚本

`smoke_chrome_engine.py` 的清理断言走的是**正常路径**：

    eng.close() -> shutdown -> agent.stop() -> transport.stop() -> kill_tree()

Chrome 是被 `taskkill /F /T` 带走的。**Job Object 全程没出过力** ——
所以那个脚本 23/23 全通过，也证明不了兜底机制有效。这是典型的
「判据不能区分被测机制」：用正常路径测兜底，永远是假阳性。

## 这个脚本怎么制造「非正常路径」

    taskkill /F /PID <引擎 pid>        ← 注意：**不带 /T**

不带 `/T` 意味着只杀引擎自己，不碰它的子进程树。引擎在没有任何机会
执行 `stop()` / `kill_tree()` 的情况下消失，这正是要复现的场景：

  * 测试超时被强杀
  * 用户任务管理器里结束进程
  * 引擎崩溃

在这之后，Chrome 还活着吗？
  * 活 -> 兜底没生效（真实缺陷，之前实测就发生过：pid 8596 成孤儿）
  * 死 -> Job Object 的 KILL_ON_JOB_CLOSE 生效

## 为什么判据是 `--user-data-dir`

用「命令行里带 `gad-chrome-` 的 chrome.exe」判断归属，不看 pid 差集。
理由见 `smoke_chrome_engine.our_chrome_pids()` —— 用户的 Chrome 随时
新开 renderer，差集会有假阳性。

## 用法

    cd engine && ./.venv/Scripts/python.exe tools/smoke_chrome_orphan.py
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


def our_chrome_pids(marker: str = "gad-chrome-") -> set[int]:
    """命令行里带 `marker` 的 chrome.exe pid 集合。"""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + marker + "*' } | "
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
        return True  # 查不了就当活着，宁可保守
    return str(pid) in r.stdout


class Engine:
    """最小 RPC 客户端 —— 只需要 initialize / start / status。"""

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
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr
        try:
            for line in self.proc.stderr:
                self.stderr_lines.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    def call(self, method: str, params: dict | None = None, timeout: float = 120.0) -> dict:
        self._id += 1
        payload: dict = {"id": self._id, "method": method}
        if params:
            payload["params"] = params
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
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
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError(f"id={self._id} 超时")


def main() -> int:
    print("=" * 72)
    print("孤儿 Chrome 回收验证 —— 强杀引擎（不带 /T）")
    print("  不发任何消息；只起 Chrome 然后杀掉它的父进程")
    print("=" * 72)

    before = our_chrome_pids()
    if before:
        print(f"  [警告] 启动前已有 {len(before)} 个本引擎遗留进程：{sorted(before)}")
        print("         先手工清掉再跑，否则判据会失真。")
        return 2

    with tempfile.TemporaryDirectory(prefix="gad-chrome-orphan-") as tmp:
        appdir = Path(tmp) / "DeepSeek-Agent"
        appdir.mkdir(parents=True, exist_ok=True)
        cfg_path = appdir / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "transport": "chrome",
                    "identity": {
                        "account_label": "smoke-chrome-orphan",
                        "token_file": str(ROOT / ".token"),
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["LOCALAPPDATA"] = tmp
        env["APPDATA"] = tmp
        env["GAD_CONFIG_PATH"] = str(cfg_path)
        env["GAD_ALLOW_LIVE_AUTH"] = "1"

        eng = Engine(env)
        try:
            print("\n[1] 起引擎并拉起 Chrome")
            eng.call("initialize")
            t0 = time.monotonic()
            eng.call("start", timeout=120)
            print(f"  [信息] start() 耗时 {(time.monotonic() - t0) * 1000:.0f} ms")

            st = eng.call("status", timeout=60)
            bg = (st.get("transport") or {}).get("background") or {}
            chrome_pid = int(bg.get("chrome_pid") or 0)
            job_bound = bg.get("job_bound")
            check("Chrome 已启动", chrome_pid > 0, f"pid={chrome_pid}")
            # 这一条是本脚本的前提：job 没绑上，后面的「死了」就不能归因于它。
            check("status 暴露 job_bound 且为真", job_bound is True, f"job_bound={job_bound!r}")

            mine = our_chrome_pids()
            check("按命令行能找到我们的 Chrome", chrome_pid in mine,
                  f"匹配 {len(mine)} 个：{sorted(mine)[:8]}")
            if not mine:
                print("     判据没命中，后续结论无意义，提前退出")
                return 2

            print("\n[2] 强杀引擎（不带 /T —— 不给 stop() 任何机会）")
            r = subprocess.run(
                ["taskkill", "/F", "/PID", str(eng.proc.pid)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            print(f"  [信息] taskkill 返回码={r.returncode} {r.stdout.strip()[:120]}")
            time.sleep(2.0)
            check("引擎进程已死", not pid_alive(eng.proc.pid), f"pid={eng.proc.pid}")

            print("\n[3] 内核兜底是否把 Chrome 一起带走了")
            # 给内核一点时间：TerminateJobObject 是异步的，但通常远快于 2 秒。
            deadline = time.monotonic() + 10.0
            alive: set[int] = set()
            while time.monotonic() < deadline:
                alive = our_chrome_pids()
                if not alive:
                    break
                time.sleep(0.5)
            ok = check(
                "父进程被强杀后 Chrome 也被回收",
                len(alive) == 0,
                f"仍存活 {len(alive)} 个：{sorted(alive)[:8]}" if alive else "全部回收",
            )
            if not ok:
                print("\n     兜底失效，手工清理：")
                for p in sorted(alive):
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p)],
                                   capture_output=True)
                print("     已执行 taskkill /F /T 清理")

            # 临时 profile 目录：正常路径里是 stop() 删的，强杀路径下没人删。
            # 这里**不断言它必须消失** —— 引擎没机会执行清理代码，目录留着是
            # 预期行为，不该算失败。但要打出来，让人知道磁盘上会有残留。
            leftovers = list(Path(tempfile.gettempdir()).glob("gad-chrome-*"))
            if leftovers:
                print(f"  [信息] 临时 profile 目录残留 {len(leftovers)} 个（预期，强杀路径没人删）")
                for p in leftovers[:5]:
                    print(f"         {p}")

        finally:
            # 引擎可能已经被强杀；这里只做无害收尾。
            with contextlib.suppress(Exception):
                if eng.proc.poll() is None:
                    eng.proc.kill()

    print("\n" + "-" * 72)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    for name in FAIL:
        print(f"    [失败] {name}")
    print("-" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
