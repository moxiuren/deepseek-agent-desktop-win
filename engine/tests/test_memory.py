"""长期记忆（`memory.py`）自测。

关注两件事：

1. **注入形态** —— 记忆是要塞进系统提示词的。它必须自解释（告诉模型这是什么、
   该怎么对待），否则模型会把记忆内容复述给用户，变成纯噪声。
2. **上限** —— 记忆会长，而提示词每轮都要发。截断必须真的发生，且要有提示。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_memory.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.memory import DEFAULT_MAX_CHARS, MemoryStore

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def main() -> int:
    print("=" * 70)
    print("长期记忆（memory.py）自测")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="gad-memory-") as tmp:
        path = Path(tmp) / "memory.md"
        mem = MemoryStore(path)

        # ---------- 1. 基本读写 ----------
        print("\n[1] 基本读写")
        check("初始不存在", not mem.exists())
        check("初始读取为空串", mem.read() == "")
        check("空记忆不注入", mem.as_prompt_block() == "")

        mem.append("用户叫小莫")
        check("追加后文件存在", mem.exists())
        text = mem.read()
        check("追加内容写入", "用户叫小莫" in text, text)
        check("自动补标题", text.startswith("# 长期记忆"), text[:20])
        check("自动加列表符号", "- 用户叫小莫" in text, text)

        # ---------- 2. 分节 ----------
        print("\n[2] 分节追加")
        mem2 = MemoryStore(Path(tmp) / "m2.md")
        mem2.append("用户叫小莫", section="关于用户")
        mem2.append("偏好日系绘本", section="关于用户")
        mem2.append("正在做 deepseek-agent-desktop", section="项目")
        body = mem2.read()
        check("分节标题存在", "## 关于用户" in body and "## 项目" in body, body)
        check("同节内容聚在一起",
              body.index("用户叫小莫") < body.index("偏好日系绘本") < body.index("## 项目"),
              body)
        check("无多余三连空行", "\n\n\n" not in body, repr(body))

        # 已有写法不被重复加符号
        mem2.append("- 自己带的符号", section="关于用户")
        check("已带符号不重复加",
              body.count("- - ") == 0 and "- 自己带的符号" in mem2.read())

        # ---------- 3. 注入形态 ----------
        print("\n[3] 注入形态 · 必须自解释")
        block = mem.as_prompt_block()
        check("注入含标题", block.startswith("# 长期记忆"), block[:20])
        check("注入含原内容", "用户叫小莫" in block)
        check("注入含「已知前提」说明", "已知前提" in block, block[-120:])
        check("注入含「不要复述」约束", "不要复述" in block, block[-120:])
        check("注入含冲突时的处置", "冲突" in block, block[-120:])

        # ---------- 4. 上限与截断 ----------
        print("\n[4] 上限与截断")
        small = MemoryStore(Path(tmp) / "m3.md", max_chars=50)
        small.write("# 长期记忆\n\n" + ("很长的记忆内容。" * 40))
        blk = small.as_prompt_block()
        check("超限时截断", "只注入了开头" in blk, blk[-120:])
        check("截断后仍带完整说明", "已知前提" in blk)
        check("正文部分不超过上限太多", len(blk) < 50 + 300, str(len(blk)))

        st = small.status()
        check("status 报告超限", st["over_limit"] is True, str(st))
        check("status 含路径", bool(st["path"]), str(st))
        check("status 含字数", st["chars"] > 50, str(st))

        # 默认上限是个正数，且不该小得离谱
        check("默认上限合理", 1000 <= DEFAULT_MAX_CHARS <= 20000, str(DEFAULT_MAX_CHARS))

        # ---------- 5. 清空 ----------
        print("\n[5] 清空")
        mem.clear()
        check("清空后读取为空", mem.read() == "")
        check("清空后不再注入", mem.as_prompt_block() == "")
        check("清空不删文件（路径仍可见）", mem.exists(), str(mem.path()))

        # ---------- 6. 边界 ----------
        print("\n[6] 边界")
        mem.append("")
        check("追加空串无副作用", mem.read() == "")
        mem.write(None)  # type: ignore[arg-type]
        check("写入 None 不炸", mem.read() == "")

        # 文件被外部写坏（非 UTF-8 字节）→ 读取返回空串而非抛错
        bad = Path(tmp) / "bad.md"
        bad.write_bytes(b"\xff\xfe\x00\x01")
        check("非 UTF-8 文件读取不抛错", MemoryStore(bad).read() == "")

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
