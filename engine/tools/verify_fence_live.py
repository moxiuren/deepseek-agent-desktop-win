"""端到端验证「围栏块的边界判定」—— **会真发消息**（1 次请求）。

要回答的问题
------------

`tests/test_fence.py` 喂的是**历史日志里还原出来的**真实文本，它能证明解析器
对那一轮是对的；但它证明不了**上游下一次还会不会这样输出**，也证明不了
新的协议措辞（`CONTRACT_RULES` 里那两条关于围栏的约束）模型认不认。

而用户报的正是这条：「最新的对话好像就断了，没有 tool call 继续」。

所以这个工具把整条链路跑一遍，并且用一个**不依赖解析器自己**的判据：

    正文里的工具围栏数  ==  真的派发出去的 tool_start 数

为什么这条判据是独立的：正文是**逐字流出来**的（`delta` 事件），无论解析器
怎么判，它都在那儿。旧实现下正文里有 3 个围栏、只派发 1 个 —— 差额就是
被吞掉的那些调用，而那正是「模型在等一个永远不会来的结果」的成因。

判据（三条）
------------

1. **正文里的工具围栏数 == `tool_start` 数**（核心）
2. 至少发生了 2 次工具调用（否则这个用例是空跑，证明不了什么）
3. 写出去的文件**没有在第一个围栏处被截断** —— 文件内容里那个三反引号
   代码块必须完整存在

代价（**有痕迹**）
------------------

会发 **1 次 completion 请求**，并在账号里留下 **1 条会话**。
所以它**不放进 `--enginecheck`**，只能手动跑。
本地存储仍然隔离到临时目录（不碰真实对话库）。

用法
----

    cd engine
    ./.venv/Scripts/python.exe -X utf8 tools/verify_fence_live.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.agent import AgentRuntime  # noqa: E402
from gad_engine.config import EngineConfig  # noqa: E402
from gad_engine.identity import DEFAULT_TOKEN_FILE, load_token  # noqa: E402
from gad_engine.store import ConversationStore  # noqa: E402
from gad_engine.transport import DeepSeekRpcTransport  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}", flush=True)


def log(msg: str = "") -> None:
    print(msg, flush=True)


#: 工具围栏的**独立复述**（故意不 import 引擎里那份私有常量）。
#: 验证工具如果复用了被测代码的常量，「两边一起改错」就查不出来了。
OPENER = re.compile(
    r"(?m)^[ \t]*```(?:local_cmd:async|local_cmd|write_file:|replace_in_file:"
    r"|read_file:|list_dir:|agent-screenshot|agent-attach)"
)


def make_cfg(tmp: Path) -> EngineConfig:
    cfg = EngineConfig()
    cfg.identity.account_label = "围栏判定验证"
    # 这个用例要验证的是**解析**，不是思考通道 —— 关掉思考能省一半时间，
    # 而且思考里的围栏会被 ThinkFilter 剥掉，反而干扰「正文里数几个围栏」。
    cfg.chat.extended_thinking = False
    cfg.pacing.max_burst = 100
    cfg.pacing.hold_after_burst_ms = 0
    cfg.pacing.read_pause_every = 0
    cfg.pacing.active_start_hour = 0
    cfg.pacing.active_end_hour = 24
    cfg.prompt.template_file = str(tmp / "system-prompt.md")
    cfg.memory.file = str(tmp / "memory.md")
    return cfg


def build_prompt(target: Path) -> str:
    """题面刻意复刻**出故障的那个场景**：写出去的文件内容里本身含三反引号。"""
    posix = target.as_posix()
    return (
        "请做两件事，做完就停，不要做别的。\n"
        "\n"
        f"第一件：用 write_file 写入 `{posix}`，内容是一个 markdown 文档，"
        "包含一个二级标题「示例」和一个 bash 代码块。"
        "那个代码块的**围栏（三个反引号）必须原样出现在文件内容里**，"
        "代码块里的命令是 `echo hi`。\n"
        "\n"
        "第二件：用 local_cmd 把这个文件的内容打印出来看一眼。\n"
    )


async def scenario() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gad-fence-live-"))
    target = tmp / "demo.md"
    log(f"[准备] 本地存储隔离到 {tmp}")
    log(f"[准备] 目标文件 {target}")
    log("[注意] 本工具会发 1 次请求，并在账号里留下 1 条会话。")

    cfg = make_cfg(tmp)
    store = ConversationStore(tmp / "conversations")
    events: list[tuple[str, dict]] = []
    tp = DeepSeekRpcTransport(
        token_provider=lambda: load_token(DEFAULT_TOKEN_FILE),
        device_id_path=ROOT / ".device-id",
        model="default",
        search_enabled=False,
    )
    rt = AgentRuntime(
        cfg,
        transport=tp,
        on_event=lambda kind, payload: events.append((kind, dict(payload))),
        store=store,
    )

    def payloads(name: str) -> list[dict]:
        return [p for k, p in events if k == name]

    try:
        prompt = build_prompt(target)
        log("\n[1] 发一次请求")
        report = await rt.run(prompt)
        log(f"     stop_reason={report.stop_reason} turns={report.turns} "
            f"tool_calls={report.tool_calls}")
        log(f"     回答：{(report.final_text or '').strip()[:100]!r}")

        # ---------- 核心判据 ----------
        # 正文是逐字流出来的，与解析器怎么判无关 —— 拿它当独立的对照。
        body = "".join(p.get("text", "") for p in payloads("delta"))
        in_text = len(OPENER.findall(body))
        starts = payloads("tool_start")
        log(f"\n[2] 正文里的工具围栏 {in_text} 个 · 真的派发出去的 {len(starts)} 个")
        for s in starts:
            log(f"     · {s.get('kind')} target={str(s.get('target'))[:70]} "
                f"payload={len(str(s.get('payload', '')))} 字")

        check("至少发生了 2 次工具调用（否则这个用例是空跑）",
              len(starts) >= 2, f"只有 {len(starts)} 次")
        check("正文里的工具围栏数 == 派发出去的调用数（没有块被吞掉）",
              in_text == len(starts),
              f"正文 {in_text} 个 vs 派发 {len(starts)} 个 —— "
              f"差 {in_text - len(starts)} 个说明块被提前闭合吞掉了")
        check("report.tool_calls 与 tool_start 数一致",
              report.tool_calls == len(starts),
              f"{report.tool_calls} vs {len(starts)}")

        # ---------- 写出去的文件有没有被截断 ----------
        log("\n[3] 落盘的文件完整性")
        if not target.exists():
            check("目标文件已写出", False, str(target))
        else:
            written = target.read_text(encoding="utf-8", errors="replace")
            log(f"     文件 {len(written)} 字，结尾：{written[-40:]!r}")
            check("文件内容里含完整的代码块围栏",
                  written.count("```") >= 2,
                  f"只有 {written.count('```')} 个反引号序列 —— 正文被截断了")
            check("文件结尾是完整的一行（不是半个字符串字面量）",
                  written.endswith("\n") or written.rstrip().endswith("```"),
                  repr(written[-30:]))

            # write_file 的载荷必须和落盘内容一致（截断时两者会一起变短，
            # 所以这一条要配合上面那条一起看）。
            wf = [s for s in starts if s.get("kind") == "write_file"]
            if wf:
                payload = str(wf[-1].get("payload", ""))
                log(f"     write_file 载荷 {len(payload)} 字")
                check("write_file 载荷长度与落盘内容相当",
                      abs(len(payload) - len(written)) <= 8,
                      f"载荷 {len(payload)} vs 文件 {len(written)}")

        # ---------- 收尾 ----------
        log("\n[4] 收尾状态")
        check("这一轮正常收尾（不是被门禁/熔断掐掉的）",
              report.stop_reason in ("completed", "max_turns"),
              report.stop_reason)
    finally:
        try:
            await rt.stop()
        except Exception as exc:  # noqa: BLE001
            log(f"[收尾] 停止运行时出错（不影响结论）：{exc}")


def main() -> int:
    print("=" * 70)
    print("  围栏块边界判定 · 端到端验证（真实请求）")
    print("=" * 70)
    asyncio.run(scenario())

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
