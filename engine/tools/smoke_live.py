"""端到端冒烟（真链路）：真发一条消息，验证「对话落盘」在真实链路上成立。

与 `smoke_conversation.py` 的分工：
  * 那个只验本地文件与 RPC 形状 —— 不发网络请求
  * **这个真发一次请求**，验证 `run` 跑完之后消息确实进了对话、并且落了盘

单元测试全绿也证明不了这一点：`_record_user` / `_record_assistant` 是在
`run()` 的**真实路径**上被调用的（中间夹着闸门、SSE 解析、熔断），
只有真跑一轮才知道那两行有没有被走到。

**代价**：消耗一次极小的额度（一条「只回复两个字」的短消息）。
**隔离**：用临时 `LOCALAPPDATA`，不碰用户真实的对话与记忆。

用法：
    cd engine && ./.venv/Scripts/python.exe tools/smoke_live.py
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
        # 引擎的 stderr 必须**持续排空** —— 管道写满（默认约 64KB）之后，
        # 引擎会阻塞在写 stderr 上，表现就是「既不完成也不报错」。
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

    def wait_event(self, kind: str, timeout: float) -> dict | None:
        """等某个事件。已经在 `events` 里的也算。"""
        for ev in self.events:
            if ev.get("event") == kind:
                return ev
        deadline = time.monotonic() + timeout
        assert self.proc.stdout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "event":
                self.events.append(msg)
                if msg.get("event") == kind:
                    return msg
        return None

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.call("shutdown", timeout=10)
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def main() -> int:
    print("=" * 70)
    print("真链路冒烟 · 发一条消息，验证对话落盘")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="gad-live-") as tmp:
        appdir = Path(tmp) / "DeepSeek-Agent"
        appdir.mkdir(parents=True, exist_ok=True)
        # 最小配置：L1 闸门要求声明专用账号标识，否则一切提交被拦。
        (appdir / "config.json").write_text(
            json.dumps(
                {"identity": {"account_label": "smoke-live"}, "max_tool_turns": 3},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["LOCALAPPDATA"] = tmp
        env["APPDATA"] = tmp
        # 转储原始 SSE 帧 —— 超时时唯一能看清「服务端到底回了什么」的东西。
        env["DS_SSE_DUMP"] = str(Path(tmp) / "sse")
        # 每 20 秒把所有线程的栈打到 stderr —— 定位 worker 卡点用。
        env["GAD_DUMP_STACKS"] = "20"

        eng = Engine(env)
        try:
            print("\n[1] 握手与前置状态")
            eng.call("initialize")
            before = eng.call("conversation.current")
            conv_id = str(before.get("id") or "")
            check("拿到当前对话", bool(conv_id), str(before)[:120])
            check("起手消息为空", len(before.get("messages") or []) == 0)

            print("\n[2] 真发一条消息")
            t0 = time.monotonic()
            started = eng.call("run.async", {"goal": "只回复两个字：收到"})
            check("run.async 已受理", started.get("started") is True, str(started))

            done = eng.wait_event("run.completed", timeout=150)
            elapsed = time.monotonic() - t0
            if done is None:
                failed = eng.wait_event("run.failed", timeout=5)
                check("收到 run.completed", False,
                      f"{elapsed:.1f}s 内没有完成事件；failed={failed}")
                print(f"     已收事件：{[e.get('event') for e in eng.events]}")
                # 探一下引擎是否还活着 —— 这能区分两种完全不同的病因：
                # 「事件循环被阻塞」（status 也不回）与「worker 卡在 HTTP 上」
                # （status 照常回，只是那一轮永远不完成）。
                try:
                    st = eng.call("status", timeout=15)
                    print("     引擎仍响应 status："
                          f"transport.ready={(st.get('transport') or {}).get('ready')}")
                except Exception as exc:  # noqa: BLE001
                    print(f"     引擎不响应 status：{exc}")
                for kind in ("notice", "sent", "ready"):
                    for e in [x for x in eng.events if x.get("event") == kind][:2]:
                        print(f"     {kind}：" + json.dumps(e, ensure_ascii=False)[:500])
                if eng.events:
                    print("     最后事件："
                          + json.dumps(eng.events[-1], ensure_ascii=False)[:300])
                sse_dir = Path(tmp) / "sse"
                if sse_dir.is_dir():
                    for f in sorted(sse_dir.iterdir()):
                        print(f"     SSE 转储 {f.name}（{f.stat().st_size} 字节）")
                        body = f.read_text(encoding="utf-8", errors="replace")[:600]
                        print("       " + body.replace("\n", "\n       "))
                print("     引擎 stderr（末 70 行）：")
                for line in eng.stderr_lines[-70:]:
                    print(f"       {line}")
                return 1
            check("收到 run.completed", True, f"{elapsed:.1f}s")
            print(f"     用时 {elapsed:.1f}s")

            print("\n[3] 消息是否落进对话")
            after = eng.call("conversation.current")
            msgs = after.get("messages") or []
            check("对话里已有 2 条消息", len(msgs) == 2, str(len(msgs)))
            if len(msgs) >= 2:
                check("第 1 条是用户消息",
                      msgs[0].get("role") == "user" and
                      "收到" in str(msgs[0].get("text") or ""),
                      str(msgs[0])[:120])
                check("第 2 条是助手消息", msgs[1].get("role") == "assistant",
                      str(msgs[1])[:120])
                check("助手正文非空", bool(str(msgs[1].get("text") or "").strip()),
                      str(msgs[1])[:120])
                check("助手消息带服务端 cid", bool(msgs[1].get("cid")),
                      str(msgs[1])[:160])
            check("标题由首条用户消息生成",
                  "收到" in str(after.get("title") or ""), str(after.get("title")))

            print("\n[4] 是否真的落了盘")
            files = list((appdir / "conversations").glob("*.json"))
            check("对话目录里有文件", len(files) >= 2, str([f.name for f in files]))
            entity = appdir / "conversations" / f"{conv_id}.json"
            check("当前对话的实体文件存在", entity.exists(), str(entity))
            if entity.exists():
                raw = json.loads(entity.read_text(encoding="utf-8"))
                check("落盘的消息数与内存一致",
                      len(raw.get("messages") or []) == len(msgs),
                      f"{len(raw.get('messages') or [])} vs {len(msgs)}")
                check("落盘带会话三元组",
                      bool((raw.get("session") or {}).get("cid")),
                      str(raw.get("session"))[:160])

            print("\n[5] 记忆注入到真实提示词")
            eng.call("memory.append", {"text": "冒烟标记：记忆注入验证"})
            mem = eng.call("memory.get")
            check("记忆已写入", "冒烟标记" in str(mem.get("text") or ""))

        finally:
            eng.close()

    print("\n" + "-" * 70)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("  失败项：")
        for name in FAIL:
            print(f"    - {name}")
    print("-" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
