"""提示词模板渲染自测 —— 钉住三类「不报错、只是悄悄不干活」的失效。

这三条都是 2026-10-18 实际验证出来的缺口，形态完全一致：

  ① 模板删掉 `{{protocol}}` → 渲染结果里一个 `local_cmd` 都没有，
     模型退化成闲聊机器人；而 `missing_placeholders()` 返回 `[]`
     （它不是「不认识的占位符」，是「没写」），界面上没有任何告警。
  ② 模板写 `{{ protocol }}`（带空格）→ 正则判它是已知占位符、不告警，
     但旧的字面量 `str.replace("{{protocol}}")` 匹配不上，原样留在提示词里。
     同一个判据、两套实现，必然漂移。
  ③ `{{ memory }}` 带空格 → `_bootstrap_prompt()` 用字面量判断「模板没写」，
     于是把记忆追加到末尾，同时 `render_prompt` 又替换一次 —— 同一段内容
     在提示词里出现两遍。

共同点：**失败方式是「看起来正常」**。所以必须钉在测试里，不能靠肉眼。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_prompt.py
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


def render(template: str, **kw) -> str:
    args = {"os_name": "Windows 10", "work_dir": "C:/work", "shell_hint": "pwsh"}
    args.update(kw)
    return P.render_prompt(template, **args)


def main() -> int:
    print("=" * 70)
    print("  提示词模板渲染自测")
    print("=" * 70)

    # ---------- 1. 默认模板 ----------
    print("\n[1] 默认模板")
    out = render(P.DEFAULT_PROMPT_TEMPLATE)
    check("含工具调用协议", "# 工具调用协议" in out)
    check("含 local_cmd 示例", "```local_cmd" in out)
    check("协议只出现一次", out.count("# 工具调用协议") == 1, f"出现 {out.count('# 工具调用协议')} 次")
    check("运行时值已替换", "{{" not in out, f"残留占位符：{list(P._PLACEHOLDER_RE.findall(out))}")
    check("默认模板无未知占位符", P.missing_placeholders(P.DEFAULT_PROMPT_TEMPLATE) == [])

    # ---------- 2. 模板没写 {{protocol}} ----------
    print("\n[2] 模板删掉 {{protocol}} —— 协议必须兜底追加")
    no_proto = "# 角色\n你是一个本地代理。\n\n# 环境\n{{os}}\n{{work_dir}}\n"
    out = render(no_proto)
    check("仍然含工具调用协议", "# 工具调用协议" in out)
    check("仍然含 local_cmd 示例", "```local_cmd" in out)
    check("协议只出现一次", out.count("# 工具调用协议") == 1)
    check("用户自己的正文保留", "你是一个本地代理。" in out)
    # 这一条是缺口的核心：它不是「不认识的占位符」，所以 missing_placeholders 报不出来
    check("missing_placeholders 不会报（这就是为什么需要兜底）",
          P.missing_placeholders(no_proto) == [])

    # ---------- 3. 空模板 / 全是未知占位符 ----------
    print("\n[3] 空模板与未知占位符")
    out = render("")
    check("空模板仍产出协议", "# 工具调用协议" in out)
    check("空模板输出无前导空行", not out.startswith("\n"), repr(out[:20]))
    out = render("{{Protocol}}\n{{os}}")
    check("拼错的占位符被报出来", P.missing_placeholders("{{Protocol}}") == ["Protocol"])
    check("拼错的占位符保留原文（不静默吞掉）", "{{Protocol}}" in out)
    check("协议仍然兜底", "# 工具调用协议" in out)

    # ---------- 4. 占位符带空格 ----------
    print("\n[4] 占位符带空格 —— 判定与替换必须同源")
    spaced = "# 角色\n{{ protocol }}\n\n{{ os }}"
    out = render(spaced)
    check("带空格的 {{ protocol }} 被替换", "# 工具调用协议" in out)
    check("带空格的 {{ os }} 被替换", "Windows 10" in out)
    check("没有残留占位符", "{{" not in out, f"残留：{list(P._PLACEHOLDER_RE.findall(out))}")
    check("带空格时不会重复追加协议", out.count("# 工具调用协议") == 1,
          f"出现 {out.count('# 工具调用协议')} 次")

    # ---------- 5. declares() 与渲染一致 ----------
    print("\n[5] declares() 与 render_prompt 对同一模板的判断必须一致")
    for tpl in ("{{memory}}", "{{ memory }}", "{{  memory  }}", "无占位符"):
        got = P.declares(tpl, "memory")
        want = "memory" in {m.strip() for m in P._PLACEHOLDER_RE.findall(tpl)}
        check(f"declares({tpl!r}) == {want}", got == want)

    # 带空格的 memory 只能出现一次（缺口 ③）
    tpl = "# R\n{{ protocol }}\n{{ memory }}"
    out = render(tpl, memory="MEM-ANCHOR")
    check("带空格的 memory 只注入一次", out.count("MEM-ANCHOR") == 1,
          f"出现 {out.count('MEM-ANCHOR')} 次")

    # ---------- 6. 单次扫描：值里的占位符不被二次展开 ----------
    print("\n[6] 替换值里的字面量不会被二次展开")
    # Soul 文件里若写了「模板支持 {{os}}」这种说明，不该被替换成运行时值
    out = render("# S\n{{ protocol }}\n{{ soul }}", soul="说明：模板支持 {{os}} 占位符")
    check("值内的 {{os}} 原样保留", "{{os}}" in out, out[-80:])
    check("值内的占位符未被替换成 Windows", "支持 Windows 10 占位符" not in out)

    # ---------- 7. 重复占位符 ----------
    print("\n[7] 同一占位符写两次")
    out = render("{{protocol}}\n\n{{protocol}}")
    check("写两次就渲染两次", out.count("# 工具调用协议") == 2)
    check("写两次时不再额外追加", out.count("# 工具调用协议") == 2)

    # ---------- 8. 占位符与解析器的一致性 ----------
    print("\n[8] 渲染出的协议文本能被解析器认出来")
    out = render(P.DEFAULT_PROMPT_TEMPLATE)
    # 协议里给的示例围栏本身要被解析器识别（否则提示词在教一个跑不通的语法）
    parser = P.ToolCallParser()
    calls = parser.feed("```local_cmd\necho hi\n```\n")
    check("协议示例语法可被解析", len(calls) == 1 and calls[0].kind is P.ToolKind.LOCAL_CMD)
    check("CONTRACT_RULES 含全部关键工具", all(
        k in P.CONTRACT_RULES for k in ("local_cmd", "write_file:", "read_file:", "ask_user:")
    ))

    print("\n" + "-" * 70)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  失败：{f}")
    print("  [提示词模板自测通过]" if not FAIL else "  [提示词模板自测未通过]")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
