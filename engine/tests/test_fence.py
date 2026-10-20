"""协议层 · 围栏块的**边界判定**自测。

为什么单独一个文件：这里的每一条约束都对应一次**真实故障**，而且故障形态
是「不报错、只是悄悄少做一件事」—— 最该被钉死在测试里的一类。

背景（2026-10-15 实测）：模型用 `write_file` 写一个给 `CONTRACT_RULES` 加说明的
补丁脚本，脚本正文里含 13 处 ```，其中三处是
`"```read_file:<绝对路径> [offset=N limit=M]```\\n"` 这种**内联字面量**。
旧实现取「info 行之后第一个 ```」当闭标记，于是：

  · 落盘的文件被截断（实测 796 字节，尾部停在半个字符串字面量上）；
  · 剩余正文失去同步，紧跟其后的两个 `local_cmd` 一个都没派发；
  · run 以 `stop_reason=completed` 正常结束 —— 用户看到的是「对话断了」。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_fence.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import protocol as P

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'通过' if cond else '失败'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def kinds(calls: list[P.ToolCall]) -> list[str]:
    return [c.kind.value for c in calls]


# 复刻真实那一轮的最小骨架：write_file 的正文里含**内联**的 ``` 字面量，
# 后面还跟着两个 local_cmd。旧解析器只认第一个（且正文被截断）。
REAL_WORLD = (
    "`classify` 正常。现在给 `CONTRACT_RULES` 加工具说明。\n"
    "\n"
    "```write_file:C:/proj/engine/tools/patch_protocol2.py\n"
    '"""给 protocol.py 加文件工具说明。"""\n'
    "\n"
    "insert = (\n"
    '    "文件工具（改现有文件优先用这些）：\\n"\n'
    '    "\\n"\n'
    '    "```read_file:<绝对路径> [offset=N limit=M]```\\n"\n'
    '    "```list_dir:<绝对路径> [depth=N]```\\n"\n'
    '    "\\n"\n'
    '    "或走异步泳道（长命令，不阻塞对话）："\n'
    ")\n"
    "\n"
    'print(f"{text}\\n\\n请输出下一个 ```local_cmd，或结束本轮。")\n'
    "```\n"
    "\n"
    "```local_cmd\n"
    "python tools/patch_protocol2.py 2>&1\n"
    "```\n"
    "\n"
    "```local_cmd\n"
    "python -c \"print('protocol ok')\" 2>&1\n"
    "```\n"
    "\n"
    "请回传两个结果。\n"
)


def main() -> int:
    print("=" * 70)
    print("协议层 · 围栏块边界判定自测")
    print("=" * 70)

    # ---------- 1. 真实故障复现 ----------
    print("\n[1] 正文里含**内联** ``` 时，块不能被提前截断")
    calls = P.ToolCallParser().feed(REAL_WORLD)
    check("解析出 3 个调用（1 个 write_file + 2 个 local_cmd）",
          len(calls) == 3, f"得到 {len(calls)} 个：{kinds(calls)}")
    if len(calls) == 3:
        wf, c1, c2 = calls
        check("第 1 个是 write_file", wf.kind is P.ToolKind.WRITE_FILE, str(wf.kind))
        check("write_file 目标正确", wf.target.endswith("patch_protocol2.py"), wf.target)
        # 正文完整性的判据：结尾那行必须还在，且中间的内联字面量没被吃掉
        check("write_file 正文没被截断（结尾在）",
              wf.payload.rstrip().endswith("或结束本轮。\"") or "print(f" in wf.payload,
              repr(wf.payload[-60:]))
        check("write_file 正文保留了内联的 ``` 字面量",
              "```read_file:" in wf.payload and "```list_dir:" in wf.payload,
              repr(wf.payload[:120]))
        check("第 2 个是 local_cmd", c1.kind is P.ToolKind.LOCAL_CMD, str(c1.kind))
        check("第 2 个载荷是第一条命令",
              "patch_protocol2.py" in c1.payload, repr(c1.payload))
        check("第 3 个是 local_cmd", c2.kind is P.ToolKind.LOCAL_CMD, str(c2.kind))
        check("第 3 个载荷是第二条命令",
              "protocol ok" in c2.payload, repr(c2.payload))
    check("残留缓冲只剩尾部正文",
          P.ToolCallParser().feed(REAL_WORLD) and True)

    # ---------- 2. 闭标记的判定规则 ----------
    print("\n[2] 闭标记必须独占一行")

    # 闭标记带前导空白 / 缩进：认
    indented = "```local_cmd\nls\n   ```\n"
    got = P.ToolCallParser().feed(indented)
    check("缩进的闭标记仍然认", len(got) == 1, f"得到 {len(got)} 个")

    # 闭标记后面同一行还有别的字符：不认（否则内联字面量会把块截断）
    inline_tail = "```local_cmd\nls\n```then prose\n```\n"
    got = P.ToolCallParser().feed(inline_tail)
    check("行尾带内容的 ``` 不当闭标记", len(got) == 1, f"得到 {len(got)} 个")
    if got:
        check("正文把带尾巴那行也算进去", "then prose" in got[0].payload, repr(got[0].payload))

    # CRLF：闭标记判定要能吃掉 \r
    crlf = "```local_cmd\r\nls\r\n```\r\n"
    got = P.ToolCallParser().feed(crlf)
    check("CRLF 行尾的闭标记能识别", len(got) == 1, f"得到 {len(got)} 个")

    # ---------- 3. 开标记必须独占行首 ----------
    print("\n[3] 开标记必须独占行首")
    inline_open = '他说 "```local_cmd" 就是开标记。\n\n```local_cmd\necho hi\n```\n'
    got = P.ToolCallParser().feed(inline_open)
    check("内联的 ```local_cmd 不被当开标记", len(got) == 1, f"得到 {len(got)} 个")
    if got:
        check("载荷来自真正的那个块", got[0].payload.strip() == "echo hi", repr(got[0].payload))

    # ---------- 4. 非工具围栏照旧跳过 ----------
    print("\n[4] 非工具围栏（```python）跳过，但不影响后面的调用")
    mixed = (
        "看这段代码：\n\n"
        "```python\n"
        "print('```local_cmd')\n"
        "```\n\n"
        "然后执行：\n\n"
        "```local_cmd\n"
        "echo done\n"
        "```\n"
    )
    got = P.ToolCallParser().feed(mixed)
    check("python 围栏被跳过，local_cmd 被认出来", len(got) == 1, f"得到 {len(got)} 个：{kinds(got)}")
    if got:
        check("载荷正确", got[0].payload.strip() == "echo done", repr(got[0].payload))

    # ---------- 5. 增量喂入（逐字符）必须与一次性喂入等价 ----------
    print("\n[5] 增量解析：逐字符喂入结果一致")
    incremental = P.ToolCallParser()
    acc: list[P.ToolCall] = []
    for ch in REAL_WORLD:
        acc += incremental.feed(ch)
    check("逐字符喂入也得到 3 个调用", len(acc) == 3, f"得到 {len(acc)} 个：{kinds(acc)}")
    check("逐字符喂入的载荷与一次性喂入一致",
          [c.payload for c in acc] == [c.payload for c in calls],
          "载荷不一致")

    # 半个开标记到达时不能误判
    half = P.ToolCallParser()
    check("半个开标记到达时不判定", half.feed("```loca") == [])
    check("半个开标记被扣在缓冲里", half.pending() == "```loca", repr(half.pending()))
    got = half.feed("l_cmd\necho hi\n```\n")
    check("补齐后能解析出来", len(got) == 1, f"得到 {len(got)} 个")

    # 未闭合：保持不判定
    open_only = P.ToolCallParser()
    check("块未闭合时不判定", open_only.feed("```local_cmd\necho hi\n") == [])
    check("未闭合的块留在缓冲里", "echo hi" in open_only.pending())

    # ---------- 6. 去重仍然生效 ----------
    print("\n[6] 同一个块重复出现只派发一次")
    dup = P.ToolCallParser()
    block = "```local_cmd\necho once\n```\n"
    check("首次派发", len(dup.feed(block)) == 1)
    check("重复内容被去重", len(dup.feed(block)) == 0)

    print("\n" + "-" * 70)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  失败：{f}")
    print("  [围栏判定自测通过]" if not FAIL else "  [围栏判定自测未通过]")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
