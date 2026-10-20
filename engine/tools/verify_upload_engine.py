"""真链路验证 · 经引擎进程（`run.async` 的 `files` 参数）发附件。

前两个脚本（`verify_upload_live.py` / `verify_upload_edge.py`）直接调传输层，
绕过了引擎。这个走**完整的引擎进程链路**：

    run.async {goal, files} → Agent.run() → transport.send(files=) → 上传 → completion

验证的是「界面能用的那条路」通不通 —— 传输层通不代表这条路通，
中间还隔着参数解析、首轮挂载、附件清空时机这些环节。

用法：
    cd engine && ./.venv/Scripts/python.exe tools/verify_upload_engine.py
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


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


class Engine:
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
        # stderr 必须持续排空 —— 管道写满（约 64KB）后引擎会阻塞在写 stderr 上，
        # 表现是「既不完成也不报错」。
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.proc.stderr
        try:
            for line in self.proc.stderr:
                self.stderr_lines.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    def _next_msg(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        assert self.proc.stdout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("引擎已退出")
            try:
                return json.loads(line)
            except ValueError:
                continue
        raise TimeoutError("读消息超时")

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        payload: dict = {"id": self._id, "method": method}
        if params:
            payload["params"] = params
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self._next_msg(max(0.1, deadline - time.monotonic()))
            if msg.get("type") == "event":
                self.events.append(msg)
                continue
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError(f"id={self._id} 超时")

    def wait_event(self, kind: str, timeout: float) -> dict | None:
        for ev in self.events:
            if ev.get("event") == kind:
                return ev
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self._next_msg(max(0.1, deadline - time.monotonic()))
            except TimeoutError:
                return None
            if msg.get("type") == "event":
                self.events.append(msg)
                if msg.get("event") == kind:
                    return msg
        return None

    def close(self) -> None:
        # 引擎可能已经自己退了 / 正忙，关不掉不该让脚本挂掉
        with contextlib.suppress(Exception):
            self.call("shutdown", timeout=10)
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def main() -> int:
    print("=" * 70)
    print("真链路验证 · 经引擎进程发附件（run.async 的 files 参数）")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="gad-eng-") as tmp:
        tmpdir = Path(tmp)
        appdir = tmpdir / "DeepSeek-Agent"
        appdir.mkdir(parents=True, exist_ok=True)
        (appdir / "config.json").write_text(
            json.dumps(
                {"identity": {"account_label": "verify-upload"}, "max_tool_turns": 2},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        sample = tmpdir / "引擎链路样本.txt"
        sample.write_text(
            "这是一份验证引擎链路用的文件。\n唯一的暗号是：紫罗兰色的考拉。\n",
            encoding="utf-8",
        )
        print(f"\n样本: {sample.name}（{sample.stat().st_size} 字节）")

        env = dict(os.environ)
        # 引擎子进程继承真实 LOCALAPPDATA 才能读到 .token —— 但配置目录用临时的，
        # 免得把验证对话写进用户真实历史。
        env["LOCALAPPDATA"] = tmp
        env["APPDATA"] = tmp
        env["GAD_ALLOW_LIVE_AUTH"] = "1"

        # token 文件是相对 engine/ 的 `.token`，引擎按项目路径找，不受 LOCALAPPDATA 影响
        eng = Engine(env)
        try:
            print("\n[1] 握手")
            eng.call("initialize")
            st = eng.call("status")
            # `started` 指的是**runtime 已创建**，不是「引擎进程活着」。
            # 刚 initialize 完还没跑过任何任务，runtime 是惰性创建的，
            # 所以这里 `started=False` 是**正常状态**，不能当失败。
            # 真正该验的是凭据已读到（`has_token`）。
            ident = st.get("identity") or {}
            check("引擎已握手（有 token）", ident.get("has_token") is True, str(ident)[:200])
            print(f"  runtime started={st.get('started')}（惰性创建，此处 False 正常）")

            print("\n[2] run.async 带 files")
            t0 = time.monotonic()
            started = eng.call("run.async", {"goal": "只回答文件里的暗号，不要解释。", "files": [str(sample)]})
            check("run.async 已受理", started.get("started") is True, str(started))

            done = eng.wait_event("run.completed", timeout=180)
            elapsed = (time.monotonic() - t0) * 1000.0
            if done is None:
                failed = eng.wait_event("run.failed", timeout=1)
                check("run.completed 到达", False, json.dumps(failed, ensure_ascii=False)[:300] if failed else "超时")
                print("\n  stderr 尾部:")
                for line in eng.stderr_lines[-15:]:
                    print(f"    {line}")
                return 1
            check("run.completed 到达", True)
            print(f"  总耗时 {elapsed:.0f} ms")

            report = done.get("data") or done
            print(f"  最终正文: {str(report.get('final_text'))[:200]!r}")

            # file_upload 事件 —— 证明附件真的上传了，而不只是参数被收下
            fu = eng.wait_event("file_upload", timeout=2)
            if fu:
                print(f"\n  ★ file_upload 事件: {json.dumps(fu.get('data'), ensure_ascii=False)}")
            check("收到 file_upload 事件", fu is not None, "没收到，附件可能没上传")
            if fu:
                d = fu.get("data") or {}
                check("事件里 count == 1", d.get("count") == 1, str(d))
                check("事件里 upload_ms > 0", (d.get("upload_ms") or 0) > 0, str(d))
                fl = d.get("files") or []
                if fl:
                    print(f"  附件详情: {json.dumps(fl[0], ensure_ascii=False)}")
                    check("事件里有 file id", bool(fl[0].get("id")), str(fl[0]))

            text = str(report.get("final_text") or "")
            check("正文含暗号「紫罗兰色的考拉」", "紫罗兰色的考拉" in text, f"实际: {text[:200]!r}")
        finally:
            eng.close()

    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
