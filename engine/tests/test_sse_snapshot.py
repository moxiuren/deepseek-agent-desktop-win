"""SSE 解析自测 —— **快照正文必须走增量通道**。

背景（2026-10-15 14:15 实跑暴露的真缺陷）
------------------------------------------
DeepSeek 的 completion 流是「快照 + 增量」结构。原来的实现里，快照帧的
`fragments[].content` 只写进 `_Acc.snapshot_text`，**从不经过 `emit`**。
短回复（例如「收到」）可能**整段只出现在快照帧**，一个增量帧都没有 ——
于是：

  * `reply_text()` 能拼出正文（transport 侧正确）
  * 但 `on_delta` 一次都不会被调用 ⇒ 界面看不到这段字，
    编排层用增量拼出的 `final_text` 也是空的
  * 用户看到的现象：**消息发出去了，一个字都没回来**

用例里的帧序列全部取自真实转储（`docs/evidence/sse/`），不是构造的。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_sse_snapshot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.transport.deepseek_rpc import _Acc, _SseParser

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'通过' if cond else '失败'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


class Recorder:
    """收集 parser 抛出的 (channel, payload)。"""

    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.thoughts: list[str] = []

    def __call__(self, kind: str, payload: object) -> None:
        if kind == "delta":
            self.deltas.append(str(payload))
        elif kind == "thought":
            self.thoughts.append(str(payload))


def drive(frames: list[tuple[str | None, str]]) -> tuple[_Acc, Recorder]:
    acc = _Acc()
    rec = Recorder()
    p = _SseParser(acc, rec)
    for event, data in frames:
        p.feed(event, data)
    return acc, rec


# ---------- 真实帧片段（取自 docs/evidence/sse/） ----------

READY = ("ready", '{"request_message_id":1,"response_message_id":2,"model_type":"default"}')


def snap(content: str, *, kind: str = "RESPONSE", status: str = "WIP") -> tuple[str | None, str]:
    return (
        None,
        json.dumps(
            {
                "v": {
                    "response": {
                        "message_id": 2,
                        "status": status,
                        "fragments": [{"id": 2, "type": kind, "content": content}],
                    }
                }
            },
            ensure_ascii=False,
        ),
    )


def delta_first(path: str, value: str) -> tuple[str | None, str]:
    return None, json.dumps({"p": path, "o": "APPEND", "v": value}, ensure_ascii=False)


def delta_sticky(value: str) -> tuple[str | None, str]:
    return None, json.dumps({"v": value}, ensure_ascii=False)


def snap_search() -> tuple[str | None, str]:
    """搜索轮次的首帧快照（实测形态：只有一个 SEARCH 片段，content 为 null）。"""
    return (
        None,
        json.dumps(
            {
                "v": {
                    "response": {
                        "message_id": 2,
                        "status": "WIP",
                        "search_triggered": True,
                        "fragments": [
                            {
                                "id": 2,
                                "type": "SEARCH",
                                "status": "WIP",
                                "content": None,
                                "queries": [{"query": "上海 2026年10月15日 实时天气"}],
                            }
                        ],
                    }
                }
            },
            ensure_ascii=False,
        ),
    )


def snap_typed(ftype: str, content: str, *, search_triggered: bool = False) -> tuple[str | None, str]:
    """按指定 fragment 类型构造快照（真实字段名取自实测转储）。"""
    resp: dict = {
        "message_id": 2,
        "status": "WIP",
        "fragments": [
            {"id": 2, "type": ftype, "content": content, "references": [], "stage_id": 1}
        ],
    }
    if search_triggered:
        resp["search_triggered"] = True
    return None, json.dumps({"v": {"response": resp}}, ensure_ascii=False)


def search_round_frames() -> list[tuple[str | None, str]]:
    """2026-10-15 实测的完整搜索轮次帧序列（逐字取自原始转储）。"""
    return [
        READY,
        snap_search(),
        # 来源列表
        (
            None,
            json.dumps(
                {
                    "p": "response/fragments/-1/results",
                    "v": [
                        {
                            "url": "https://www.nmc.cn/publish/forecast/ASH/shanghai.html",
                            "title": "上海-天气预报",
                            "snippet": "上海 23℃ 日落17:23",
                            "cite_index": 1,
                            "site_name": "中央气象台",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ),
        # SEARCH 片段收尾 —— BATCH 的子帧路径是**相对**的（裸 `status` / `content`）
        (
            None,
            '{"p":"response/fragments/-1","o":"BATCH","v":[{"p":"status","v":"FINISHED"},'
            '{"p":"content","v":"Found 12 web pages"}]}',
        ),
        (None, '{"p":"response/conversation_mode","o":"SET","v":"SEARCH"}'),
        # RESPONSE 片段以 APPEND 进场，**它的 content 就是正文开头**
        (
            None,
            '{"p":"response","o":"BATCH","v":[{"p":"fragments","o":"APPEND","v":'
            '[{"id":3,"type":"RESPONSE","content":"上海","references":[],"stage_id":2}]},'
            '{"p":"has_pending_fragment","v":true}]}',
        ),
        (None, '{"p":"response/fragments/-1/content","o":"APPEND","v":"今天"}'),
        delta_sticky("多云"),
    ]


def main() -> int:
    print("=" * 70)
    print("SSE 解析自测 —— 快照正文必须走增量通道")
    print("=" * 70)

    # ---------- 1. 只有快照、零增量（14:15 真实复现） ----------
    print("\n[1] 只有快照、零增量 —— 短回复的真实形态")
    frames = [
        READY,
        ("update_session", '{"updated_at":1792044917.673475}'),
        snap("收到"),
        (None, '{"p":"response","o":"BATCH","v":[{"p":"accumulated_token_usage","v":442},'
               '{"p":"quasi_status","v":"FINISHED"}]}'),
        (None, '{"p":"response/status","o":"SET","v":"FINISHED"}'),
        ("close", '{"click_behavior":"none","auto_resume":false}'),
    ]
    acc, rec = drive(frames)
    check("reply_text 拼出正文", acc.reply_text() == "收到", repr(acc.reply_text()))
    check("on_delta 收到快照正文（原来这里是空的）", rec.deltas == ["收到"], repr(rec.deltas))
    check("增量拼接 == reply_text", "".join(rec.deltas) == acc.reply_text())
    check("status 被读到", acc.status == "FINISHED", repr(acc.status))

    # ---------- 2. 快照 + 增量（08:26 真实形态） ----------
    print("\n[2] 快照 + 后续增量 —— 不能重复，也不能漏")
    frames = [
        READY,
        snap("收到了"),
        delta_first("response/fragments/-1/content", "，"),
        delta_sticky("你"),
        delta_sticky("让我"),
        delta_sticky("从"),
        (None, '{"p":"response","o":"BATCH","v":[{"p":"accumulated_token_usage","v":110},'
               '{"p":"quasi_status","v":"FINISHED"}]}'),
        (None, '{"p":"response/status","o":"SET","v":"FINISHED"}'),
    ]
    acc, rec = drive(frames)
    expected = "收到了，你让我从"
    check("reply_text 正确", acc.reply_text() == expected, repr(acc.reply_text()))
    check("增量拼接 == reply_text（无重复无遗漏）", "".join(rec.deltas) == expected, repr(rec.deltas))
    check("快照段只出现一次", rec.deltas.count("收到了") == 1, repr(rec.deltas))

    # ---------- 3. 多次快照递增 ----------
    print("\n[3] 快照多次到达且内容递增 —— 只补差值")
    frames = [
        READY,
        snap("1"),
        snap("1\n2"),
        snap("1\n2\n3"),
    ]
    acc, rec = drive(frames)
    check("reply_text = 最后一次快照", acc.reply_text() == "1\n2\n3", repr(acc.reply_text()))
    check("每次只补差值", rec.deltas == ["1", "\n2", "\n3"], repr(rec.deltas))
    check("拼接与正文一致", "".join(rec.deltas) == acc.reply_text())

    # ---------- 4. 快照 + 增量交替（增量在快照之后） ----------
    print("\n[4] 快照 → 增量 → 再来一帧快照 —— 边界处不漏不重")
    frames = [
        READY,
        snap("AB"),
        delta_first("response/fragments/-1/content", "C"),
        snap("ABC"),  # 快照追上了增量：此时不该再补
        delta_sticky("D"),
    ]
    acc, rec = drive(frames)
    check("reply_text = ABCD", acc.reply_text() == "ABCD", repr(acc.reply_text()))
    check("没有重复补发", rec.deltas == ["AB", "C", "D"], repr(rec.deltas))

    # ---------- 5. 思考通道走 thought ----------
    print("\n[5] THINKING 快照走 thought 通道，不混进正文")
    frames = [
        READY,
        snap("让我想想……", kind="THINKING"),
        snap("收到"),
    ]
    acc, rec = drive(frames)
    check("正文只含 RESPONSE 段", acc.reply_text() == "收到", repr(acc.reply_text()))
    check("思考走 thought", rec.thoughts == ["让我想想……"], repr(rec.thoughts))
    check("正文走 delta", rec.deltas == ["收到"], repr(rec.deltas))

    # ---------- 6. 快照被重写（非追加） ----------
    print("\n[6] 快照被重写 —— 整段补发，不留残影")
    frames = [
        READY,
        snap("abc"),
        snap("xyz"),
    ]
    acc, rec = drive(frames)
    check("reply_text 以最后一次快照为准", acc.reply_text() == "xyz", repr(acc.reply_text()))
    check("整段补发新内容", rec.deltas == ["abc", "xyz"], repr(rec.deltas))

    # ---------- 7. 错误通道不受影响 ----------
    print("\n[7] Server busy 走 hint，仍报 error 而非空回复")
    frames = [
        READY,
        ("hint", '{"type":"error","content":"Server busy, please try again later.",'
                 '"clear_response":true,"finish_reason":"generation_timeout"}'),
        ("close", '{"click_behavior":"retry","auto_resume":false}'),
    ]
    acc, rec = drive(frames)
    check("error 被读到", "Server busy" in acc.error, repr(acc.error))
    check("finish_reason 被读到", acc.finish_reason == "generation_timeout", repr(acc.finish_reason))
    check("无正文", acc.reply_text() == "", repr(acc.reply_text()))

    # ---------- 8. 联网搜索轮次（2026-10-15 实测帧序列） ----------
    print("\n[8] 搜索轮次 —— SEARCH 状态不进正文，RESPONSE 开头不丢")
    acc, rec = drive(search_round_frames())
    check(
        "正文不含 'Found 12 web pages'",
        "Found" not in acc.reply_text(),
        repr(acc.reply_text()),
    )
    check(
        "正文保留了 RESPONSE 片段的开头（原来整个丢了）",
        acc.reply_text() == "上海今天多云",
        repr(acc.reply_text()),
    )
    check("增量拼接 == reply_text", "".join(rec.deltas) == acc.reply_text(), repr(rec.deltas))
    check("页数被记到 search_pages", acc.search_pages == 12, repr(acc.search_pages))
    check("来源数被记到 search_results", len(acc.search_results) == 1, repr(len(acc.search_results)))
    check("search_triggered 被读到", acc.search_triggered is True, repr(acc.search_triggered))
    check("来源标题可用", (acc.search_results[0].get("title") == "上海-天气预报"), "")

    # ---------- 9. 未开搜索时不误伤 ----------
    print("\n[9] 未开搜索的普通轮次不受影响")
    acc, rec = drive([READY, snap("收到")])
    check("正文正常", acc.reply_text() == "收到", repr(acc.reply_text()))
    check("没有搜索痕迹", acc.search_pages == 0 and not acc.search_results, "")
    check("search_triggered 保持 False", acc.search_triggered is False, repr(acc.search_triggered))

    # ---------- 10. 思考片段的实测类型是 THINK（不是 THINKING） ----------
    print("\n[10] 思考片段实测类型是 THINK —— 绝不能当正文")
    acc, rec = drive([READY, snap_typed("THINK", "用户想知道上海今天的实时天气。我需要搜索")])
    check("THINK 不进正文", acc.reply_text() == "", repr(acc.reply_text()))
    check("THINK 进 thought 通道", "我需要搜索" in acc.thought_text(), repr(acc.thought_text()))

    acc, rec = drive(
        [
            READY,
            snap_typed("THINK", "让我想想"),
            snap_typed("RESPONSE", "上海今天多云到阴。"),
        ]
    )
    check(
        "思考 + 正文同时存在时正文干净",
        acc.reply_text() == "上海今天多云到阴。",
        repr(acc.reply_text()),
    )
    check("思考单独归位", acc.thought_text() == "让我想想", repr(acc.thought_text()))

    # ---------- 11. THINK 的增量（粘性帧）也不能漏进正文 ----------
    print("\n[11] THINK 片段的增量帧走 thought")
    acc, rec = drive(
        [
            READY,
            snap_typed("THINK", "想"),
            delta_first("response/fragments/-1/content", "一"),
            delta_sticky("想"),
        ]
    )
    check("正文为空", acc.reply_text() == "", repr(acc.reply_text()))
    check("思考拼完整", acc.thought_text() == "想一想", repr(acc.thought_text()))
    check("没有 delta 事件", rec.deltas == [], repr(rec.deltas))

    # ---------- 12. TOOL_* 类型同样不进正文 ----------
    print("\n[12] TOOL_OPEN / TOOL_SEARCH 不进正文，且类型被记下")
    acc, rec = drive(
        [
            READY,
            snap_typed("TOOL_OPEN", "正在打开网页…"),
            snap_typed("TOOL_SEARCH", "搜索中"),
            snap_typed("RESPONSE", "答案在这里。"),
        ]
    )
    check("工具片段不进正文", acc.reply_text() == "答案在这里。", repr(acc.reply_text()))
    check(
        "工具类型被记录（便于诊断未知类型）",
        {"TOOL_OPEN", "TOOL_SEARCH"} <= acc.skipped_frag_types,
        repr(acc.skipped_frag_types),
    )

    print("\n" + "-" * 70)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        for name in FAIL:
            print(f"    [失败] {name}")
    print("")
    print("  [SSE 快照自测通过]" if not FAIL else "  [SSE 快照自测未通过]")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
