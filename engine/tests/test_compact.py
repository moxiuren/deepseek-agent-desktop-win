"""上下文压缩（`compact.py`）自测。

为什么单独钉这一条
------------------

`summarize_local()` 是**模型摘要失败时的兜底**，也是 `mode=local` 时的唯一
实现。它的地位决定了它必须能**独立工作** —— 如果它挂了，用户按「接力新会话」
就会得到一张白纸，而这恰恰是那个按钮最不该给出的结果。

`extract_summary_reply()` 同样值得钉：模型对「只输出摘要」这个要求**经常不
完全遵守**（先来一句「好的，我来总结一下」、把整段包进代码围栏）。这些噪声
一旦漏进摘要，就会被注入到新会话的每一轮提示词里，而且是持续的。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_compact.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.compact import (
    extract_commands,
    extract_paths,
    extract_summary_reply,
    looks_like_summary,
    summarize_local,
)
from gad_engine.store import Message

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def msg(role: str, text: str) -> Message:
    return Message(role=role, text=text)


def main() -> int:
    print("=" * 70)
    print("上下文压缩 · 本地摘要与回复清理自测")
    print("=" * 70)

    # ---------- 1. 本地摘要的结构化抽取 ----------
    print("\n[1] 本地摘要：抽得出结构，且不编造")
    convo = [
        msg("user", "帮我把 engine 目录下的测试全跑一遍"),
        msg(
            "assistant",
            "好的，我先看一下目录。\n\n```local_cmd\nGet-ChildItem -Path engine -Depth 3\n```",
        ),
        msg("user", "顺便把失败的修一下"),
        msg(
            "assistant",
            "已修好 3 个失败用例。\n\n"
            "```write_file:C:/proj/engine/tests/test_x.py\nprint(1)\n```\n"
            "改动落在 C:/proj/engine/tests/test_x.py，其余用例全绿。",
        ),
    ]
    s = summarize_local(convo)
    check("含【目标】", "【目标】" in s, s[:200])
    check("目标取自首条用户消息", "engine 目录下的测试全跑一遍" in s, s)
    check("含【用户先后提出的要求】", "【用户先后提出的要求】" in s, s)
    check("含【已经做过的操作】", "【已经做过的操作】" in s, s)
    check("含【最近一轮的结论】", "【最近一轮的结论】" in s, s)
    check("命令被抽出", "Get-ChildItem" in s, s)
    check("写文件路径被抽出", "test_x.py" in s, s)
    check("摘要里没有代码围栏", "```" not in s, s)

    # 压缩比：短对话本来就没得压（结构化列一遍反而更长），
    # 真正的意义在**长对话**上 —— 用它来验证。
    # 分布要**贴近真实**：用户提问短、助手回答长。反过来（回答比提问短）
    # 是不真实的，而且会得出「压不动」的错误结论。
    long_convo = []
    for i in range(8):
        long_convo.append(msg("user", f"第{i}个要求：" + "详细说明" * 8))
        long_convo.append(msg("assistant", f"第{i}轮的回答：" + "展开阐述" * 120))
    src_len = sum(len(m.text) for m in long_convo)
    out_len = len(summarize_local(long_convo))
    check("长对话有压缩比（< 40%）", out_len < src_len * 0.4, f"{src_len} -> {out_len}")

    # ---------- 2. 空对话 / 只有一条 ----------
    print("\n[2] 边界：空对话不产出摘要")
    check("空列表 → 空串", summarize_local([]) == "")
    check("全是空白 → 空串", summarize_local([msg("user", "   "), msg("assistant", "")]) == "")

    one = summarize_local([msg("user", "只有一句话")])
    check("只有一条用户消息也能出摘要", "只有一句话" in one, one)
    check("单条时不出现「先后要求」段", "【用户先后提出的要求】" not in one, one)

    # ---------- 3. prior_summary（多代接力不丢前文） ----------
    print("\n[3] 多代接力：上一代摘要要带上")
    prior = "【目标】第一代的目标"
    s2 = summarize_local([msg("user", "第二代的问题")], prior_summary=prior)
    check("带上更早的背景", "更早的背景" in s2 and "第一代的目标" in s2, s2)

    # ---------- 4. 上限截断 ----------
    print("\n[4] 上限：超长必须截断（它要注入每一轮提示词）")
    long_convo = [msg("user", "很长的一句话" * 400)]
    clipped = summarize_local(long_convo, max_chars=300)
    check("被截断到上限附近", len(clipped) <= 300 + 20, str(len(clipped)))
    check("截断有明确标注", "已截断" in clipped, clipped[-40:])

    # ---------- 5. 命令 / 路径抽取 ----------
    print("\n[5] 抽取：命令与路径")
    cmds = extract_commands(
        [msg("assistant", "```local_cmd:async\nnpm run build\n```\n```local_cmd\nls\n```")]
    )
    check("异步围栏也认", "npm run build" in cmds, str(cmds))
    check("普通围栏也认", "ls" in cmds, str(cmds))
    check("重复命令去重", len(extract_commands([msg("assistant", "```local_cmd\nls\n```" * 3)])) == 1)

    paths = extract_paths([msg("assistant", "```write_file:D:/a/b.py\nx\n```\n见 C:/proj/c.md")])
    check("write_file 目标被抽出", "D:/a/b.py" in paths, str(paths))
    check("正文里的绝对路径被抽出", "C:/proj/c.md" in paths, str(paths))

    # ---------- 6. 模型回复的清理 ----------
    print("\n[6] 回复清理：剥寒暄 / 剥围栏 / 剥工具调用")
    raw = "好的，我来总结一下这段对话：\n\n【目标】\n把测试跑绿"
    cleaned = extract_summary_reply(raw)
    check("剥掉开场白", not cleaned.startswith("好的"), cleaned[:40])
    check("保留摘要正文", "【目标】" in cleaned and "把测试跑绿" in cleaned, cleaned)

    fenced = "```markdown\n【目标】\n完成迁移\n```"
    cleaned2 = extract_summary_reply(fenced)
    check("剥掉整段代码围栏", cleaned2 == "【目标】\n完成迁移", repr(cleaned2))

    with_tool = "【目标】\n修好\n```local_cmd\nrm -rf /\n```"
    cleaned3 = extract_summary_reply(with_tool)
    check("剥掉残留的工具围栏", "rm -rf" not in cleaned3, repr(cleaned3))

    check("空输入 → 空串", extract_summary_reply("") == "")
    check("纯空白 → 空串", extract_summary_reply("   \n  ") == "")

    # ---------- 7. looks_like_summary 的宽容度 ----------
    print("\n[7] 像不像摘要：宁可放过，不可误杀")
    check("正常摘要通过", looks_like_summary("【目标】\n把 engine 目录下的测试全部跑绿，并提交代码。"))
    check("过短拒绝", not looks_like_summary("好的"))
    check("空拒绝", not looks_like_summary(""))
    # 判据要**宽**：一份没有方括号但足够长的摘要也算数 —— 否则会因为判据太严
    # 白白降级，用户那次点击的模型质量就浪费了。
    check(
        "无方括号但够长也算数",
        looks_like_summary(
            "我们讨论了三个方案，最后决定采用第二个，因为它的改动面最小，"
            "而且不需要改动数据库结构，风险也最低，回滚也方便。"
        ),
    )
    # 上游报错文本有时也很长，光看长度挡不住 —— 必须靠特征词。
    check(
        "长错误文本被拒",
        not looks_like_summary(
            "Server busy, please try again later. (generation_timeout)"
        ),
    )

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
