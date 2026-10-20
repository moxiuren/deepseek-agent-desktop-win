"""文件工具端到端：模型输出 → ToolCallParser → tools_fs。

这一层验证的是**协议链路**，不是单测 tools_fs 本身（那个在 test_tools_fs.py）。
它回答的问题是：模型在回复正文里写出这些围栏，解析器能不能认出来、
参数有没有跑偏、喂给底层函数能不能真的干活。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_fs_pipeline.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import protocol as P
from gad_engine import tools_fs as T

PASS: list[str] = []
FAIL: list[str] = []

TICK = chr(96) * 3


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def fence(info: str, body: str) -> str:
    return TICK + info + "\n" + body + "\n" + TICK


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-fspipe-"))
    try:
        # ---------- 1. 解析 read_file ----------
        print("\n[1] 解析 read_file")
        p = P.ToolCallParser()
        stream = "让我先看看这个文件。\n\n" + fence("read_file:C:/x.py offset=10 limit=5", "")
        calls = p.feed(stream)
        check("解析出 1 个调用", len(calls) == 1, str(len(calls)))
        if calls:
            c = calls[0]
            check("kind 正确", c.kind is P.ToolKind.READ_FILE, c.kind.value)
            check("target 是完整参数串", c.target == "C:/x.py offset=10 limit=5", c.target)
            # 单行工具：payload 应为空（参数在 target 里）
            check("单行工具 payload 空", c.payload == "", repr(c.payload))

        # ---------- 2. 解析 list_dir ----------
        print("\n[2] 解析 list_dir")
        p2 = P.ToolCallParser()
        calls = p2.feed(fence("list_dir:C:/proj depth=3", ""))
        check("list_dir 解析", len(calls) == 1 and calls[0].kind is P.ToolKind.LIST_DIR, str(len(calls)))

        # ---------- 3. 解析 replace_in_file（带 body）----------
        print("\n[3] 解析 replace_in_file")
        p3 = P.ToolCallParser()
        body = (
            f"{T.MARK_OLD}\n"
            "x = 1\n"
            f"{T.MARK_SEP}\n"
            "x = 2\n"
            f"{T.MARK_NEW}"
        )
        stream = fence("replace_in_file:C:/x.py count=1", body)
        calls = p3.feed(stream)
        check("replace 解析出 1 个", len(calls) == 1, str(len(calls)))
        if calls:
            c = calls[0]
            check("kind 正确", c.kind is P.ToolKind.REPLACE_IN_FILE, c.kind.value)
            check("target 是路径+参数", c.target == "C:/x.py count=1", c.target)
            parsed = T.parse_edit_body(c.payload)
            check("payload 能解析出 OLD/NEW", parsed == ("x = 1", "x = 2"), repr(parsed))

        # ---------- 4. 分块到达（模拟流式）----------
        print("\n[4] 分块到达")
        p4 = P.ToolCallParser()
        full = fence("read_file:C:/y.py", "")
        got: list = []
        # 逐字符喂，最严苛的分块场景
        for ch in full:
            got.extend(p4.feed(ch))
        check("逐字符喂仍解析出 1 个", len(got) == 1, str(len(got)))
        check("分块后 kind 正确", got and got[0].kind is P.ToolKind.READ_FILE)

        # ---------- 5. 与旧工具混排 ----------
        print("\n[5] 与旧工具混排")
        p5 = P.ToolCallParser()
        stream = (
            fence("local_cmd", "Get-Date")
            + "\n"
            + fence("read_file:C:/z.py", "")
            + "\n"
            + fence("write_file:C:/new.txt", "hello")
        )
        calls = p5.feed(stream)
        kinds = [c.kind for c in calls]
        check("解析出 3 个", len(calls) == 3, str(len(calls)))
        check(
            "顺序与类型正确",
            kinds == [P.ToolKind.LOCAL_CMD, P.ToolKind.READ_FILE, P.ToolKind.WRITE_FILE],
            str([k.value for k in kinds]),
        )

        # ---------- 6. 解析 → 执行 的真闭环 ----------
        print("\n[6] 解析 → 执行 闭环")
        target = tmp / "real.py"
        target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

        # 6a. 先 read_file 看清内容
        p6 = P.ToolCallParser()
        calls = p6.feed(fence(f"read_file:{target}", ""))
        ok, out = T.read_file(calls[0].target)
        check("read_file 读到 3 行", ok and "共 3 行" in out, out[:80])
        check("行号可见", "2\tb = 2" in out, out[:200])

        # 6b. 用 replace_in_file 改一行
        p7 = P.ToolCallParser()
        edit_body = f"{T.MARK_OLD}\nb = 2\n{T.MARK_SEP}\nb = 22\n{T.MARK_NEW}"
        calls = p7.feed(fence(f"replace_in_file:{target}", edit_body))
        ok, out = T.replace_in_file(calls[0].target, calls[0].payload)
        check("replace 成功", ok and "替换 1 处" in out, out[:120])
        check("文件只改了目标行", target.read_text(encoding="utf-8") == "a = 1\nb = 22\nc = 3\n",
              repr(target.read_text(encoding="utf-8")))

        # 6c. 改完再 read 确认（模型会做的动作）
        p8 = P.ToolCallParser()
        calls = p8.feed(fence(f"read_file:{target}", ""))
        ok, out = T.read_file(calls[0].target)
        check("改后复查可见新值", ok and "b = 22" in out, out[:200])

        # ---------- 7. 旧工具不受影响 ----------
        print("\n[7] 旧工具回归")
        check("local_cmd 仍解析", P.classify("local_cmd")[0] is P.ToolKind.LOCAL_CMD)
        check("local_cmd:async 仍解析", P.classify("local_cmd:async")[0] is P.ToolKind.LOCAL_CMD_ASYNC)
        check("write_file 仍解析", P.classify("write_file:C:/x")[0] is P.ToolKind.WRITE_FILE)
        check("agent-screenshot 仍解析", P.classify("agent-screenshot")[0] is P.ToolKind.SCREENSHOT)
        check("普通围栏不误判", P.classify("python") is None)
        check("未知 info 不误判", P.classify("some-random-text") is None)
        check("read_file_x 不误判", P.classify("read_file_x:C:/a") is None)
        check("list_directory 不误判", P.classify("list_directory:C:/a") is None)
        check("裸名 list_dir 可识别", P.classify("list_dir")[0] is P.ToolKind.LIST_DIR)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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
