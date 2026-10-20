"""端到端冒烟：真起引擎子进程，验「对话与记忆」这条 RPC 链。

它补的是**跨进程**那一环。单元测试（`test_store` / `test_memory` /
`test_agent_conversation`）都在同一个进程里 import 模块，证明不了
「外壳发一条 `conversation.list` 能不能真拿到东西」—— 而外壳恰恰只会说 RPC。

**不发任何真实请求**：全程只动本地文件，不消耗额度、不碰熔断。
**不碰用户数据**：用隔离的 `LOCALAPPDATA`，跑完即弃。

用法：
    cd engine && ./.venv/Scripts/python.exe tools/smoke_conversation.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
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
    """把引擎当子进程驱动。"""

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

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self._id += 1
        msg_id = self._id
        payload: dict = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("引擎已退出")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "event":
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError(f"{method} 超时")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.call("shutdown", timeout=10)
        try:
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def main() -> int:
    print("=" * 70)
    print("对话与记忆 · 端到端冒烟（真起引擎子进程）")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="gad-smoke-") as tmp:
        env = dict(os.environ)
        env["LOCALAPPDATA"] = tmp          # 隔离：绝不碰用户真实数据
        env["APPDATA"] = tmp

        eng = Engine(env)
        try:
            # ---------- 1. 握手 ----------
            print("\n[1] 握手")
            init = eng.call("initialize")
            check("initialize 返回协议版本", bool(init.get("protocol")), str(init)[:120])

            # ---------- 2. 首个对话（引擎启动时自动建） ----------
            print("\n[2] 首个对话")
            cur = eng.call("conversation.current")
            first_id = str(cur.get("id") or "")
            check("启动即有一个当前对话", bool(first_id), str(cur)[:120])
            check("新对话消息为空", len(cur.get("messages") or []) == 0)
            check("标题已生成", bool(cur.get("title")), str(cur.get("title")))

            listed = eng.call("conversation.list")
            items = listed.get("items") or []
            check("列表含 1 条", len(items) == 1, str(len(items)))
            check("列表标出当前对话",
                  sum(1 for it in items if it.get("active")) == 1, str(items))

            # ---------- 3. 长期记忆 ----------
            print("\n[3] 长期记忆")
            mem = eng.call("memory.get")
            check("初始记忆为空", mem.get("text") == "", str(mem)[:120])
            check("记忆状态含路径", bool((mem.get("status") or {}).get("path")))

            eng.call("memory.append", {"text": "用户叫小莫", "section": "关于用户"})
            eng.call("memory.append", {"text": "偏好日系绘本", "section": "关于用户"})
            mem2 = eng.call("memory.get")
            text = str(mem2.get("text") or "")
            check("记忆写入生效", "用户叫小莫" in text, text[:120])
            check("分节生效", "## 关于用户" in text, text[:120])
            check("记忆字数已统计",
                  (mem2.get("status") or {}).get("chars", 0) > 0,
                  str(mem2.get("status")))

            # ---------- 4. 多对话 ----------
            print("\n[4] 多对话")
            new_conv = eng.call("conversation.new")
            second_id = str(new_conv.get("id") or "")
            check("新建得到新 id", bool(second_id) and second_id != first_id,
                  f"{second_id} vs {first_id}")
            check("新建后消息为空", len(new_conv.get("messages") or []) == 0)

            listed2 = eng.call("conversation.list")
            check("列表变成 2 条", len(listed2.get("items") or []) == 2,
                  str(len(listed2.get("items") or [])))

            # ---------- 5. 重命名 / 切换 / 删除 ----------
            print("\n[5] 重命名 / 切换 / 删除")
            renamed = eng.call("conversation.rename",
                               {"id": second_id, "title": "第二个对话"})
            check("重命名返回新标题", renamed.get("title") == "第二个对话", str(renamed))

            loaded = eng.call("conversation.load", {"id": first_id})
            check("切回第一个对话", loaded.get("id") == first_id, str(loaded.get("id")))
            check("切回后消息条数正确", len(loaded.get("messages") or []) == 0)

            after_load = eng.call("conversation.list")
            active = [it for it in after_load.get("items") or [] if it.get("active")]
            check("切换后 active 跟着变",
                  len(active) == 1 and active[0].get("id") == first_id, str(active))

            deleted = eng.call("conversation.delete", {"id": second_id})
            check("删除返回成功", deleted.get("deleted") is True, str(deleted))
            listed3 = eng.call("conversation.list")
            check("删除后剩 1 条", len(listed3.get("items") or []) == 1,
                  str(len(listed3.get("items") or [])))

            # ---------- 6. 错误路径 ----------
            print("\n[6] 错误路径")
            try:
                eng.call("conversation.load", {"id": "不存在的对话"})
                check("加载不存在的对话会报错", False, "居然成功了")
            except RuntimeError:
                check("加载不存在的对话会报错", True)
            try:
                eng.call("conversation.load", {"id": "../../config"})
                check("非法 id 被拒", False, "居然成功了")
            except RuntimeError:
                check("非法 id 被拒", True)

        finally:
            eng.close()

        # ---------- 7. 重启恢复（换一个进程读同一份数据） ----------
        print("\n[7] 重启恢复")
        eng2 = Engine(env)
        try:
            cur2 = eng2.call("conversation.current")
            check("重启后仍是同一个对话", cur2.get("id") == first_id,
                  f"{cur2.get('id')} vs {first_id}")
            listed4 = eng2.call("conversation.list")
            check("重启后对话列表还在", len(listed4.get("items") or []) == 1,
                  str(len(listed4.get("items") or [])))
            mem3 = eng2.call("memory.get")
            check("重启后记忆还在", "用户叫小莫" in str(mem3.get("text") or ""))

            eng2.call("memory.clear")
            check("清空记忆生效",
                  str(eng2.call("memory.get").get("text") or "") == "")
        finally:
            eng2.close()

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
