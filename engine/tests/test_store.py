"""对话仓库（`store.py`）自测。

覆盖三件事，按重要性排：

1. **持久化闭环** —— 关掉再打开，对话与**服务端会话三元组**都要在。
   这是「多对话」与「重启不丢上下文」的共同底座，坏了整个功能就是假的。
2. **安全边界** —— 对话 id 从 RPC 面回来，是外部输入。路径穿越必须被挡住。
3. **容错** —— 一个文件坏掉不该让整个列表读不出来。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_store.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.session import SessionMeta
from gad_engine.store import (
    INDEX_NAME,
    ConversationStore,
    Message,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def main() -> int:
    print("=" * 70)
    print("对话仓库（store.py）自测")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="gad-store-") as tmp:
        root = Path(tmp) / "conversations"

        # ---------- 1. 基本 CRUD ----------
        print("\n[1] 基本 CRUD")
        st = ConversationStore(root)
        conv = st.create()
        check("create 返回 id", bool(conv.id), conv.id)
        check("实体文件已落盘", (root / f"{conv.id}.json").exists())
        check("索引文件已落盘", (root / INDEX_NAME).exists())
        check("新对话即当前对话", st.active_id() == conv.id)
        check("空对话标题为「新对话」", conv.title == "新对话", conv.title)

        st.append(conv, Message(role="user", text="帮我看看 downloads 里有什么"))
        check("首条用户消息自动生成标题",
              conv.title == "帮我看看 downloads 里有什么", conv.title)
        check("turn_count = 1", conv.turn_count == 1, str(conv.turn_count))

        st.append(conv, Message(role="assistant", text="里面有 3 个文件。"))
        check("turn_count 仍为 1（只数用户轮）", conv.turn_count == 1, str(conv.turn_count))
        check("preview 取最后一条", conv.preview().startswith("里面有 3 个文件"), conv.preview())

        renamed = st.rename(conv.id, "下载目录检查")
        check("rename 生效", renamed is not None and renamed.title == "下载目录检查")
        check("rename 落盘", st.get(conv.id).title == "下载目录检查")

        # ---------- 2. 持久化闭环（核心） ----------
        print("\n[2] 持久化闭环 · 关掉再打开")
        # 必须用 rename 返回的新对象继续。rename 是「读-改-写」，攥着改名之前的
        # 旧引用再 save 会把这次改名冲掉 —— 这正是本测试第一版踩到的坑。
        assert renamed is not None
        meta = SessionMeta(cid="srv-session-abc", rid="resp-42", rcid="req-41", turns=1)
        renamed.session = meta
        renamed.settings = {"extended_thinking": True, "search_enabled": True}
        st.save(renamed)

        # 模拟进程重启：换一个全新的 store 实例，指向同一个目录。
        st2 = ConversationStore(root)
        restored = st2.ensure_active()
        check("重启后拿回同一个对话", restored.id == conv.id,
              f"{restored.id} != {conv.id}")
        check("消息条数完好", len(restored.messages) == 2, str(len(restored.messages)))
        check("消息正文完好",
              restored.messages[0].text == "帮我看看 downloads 里有什么",
              restored.messages[0].text)
        check("会话 cid 完好", restored.session.cid == "srv-session-abc",
              restored.session.cid)
        check("会话 rid 完好（parent_message_id 能接上）",
              restored.session.rid == "resp-42", restored.session.rid)
        check("parent_message_id 取到 rid",
              restored.session.parent_message_id == "resp-42",
              str(restored.session.parent_message_id))
        check("形态快照完好", restored.settings.get("search_enabled") is True,
              str(restored.settings))
        check("标题完好", restored.title == "下载目录检查", restored.title)

        # ---------- 3. 多对话与列表 ----------
        print("\n[3] 多对话与列表")
        conv2 = st2.create()
        st2.append(conv2, Message(role="user", text="第二个对话"))
        time.sleep(0.02)  # 让 updated_at 拉开，排序才稳定
        conv3 = st2.create()
        st2.append(conv3, Message(role="user", text="第三个对话"))

        items = st2.list_summaries()
        check("列表含 3 个对话", len(items) == 3, str(len(items)))
        check("列表按最近更新倒序",
              items[0]["id"] == conv3.id, f"首条是 {items[0]['id']}")
        check("列表项含 preview", bool(items[0].get("preview")), str(items[0]))
        check("列表项含 turn_count", items[0].get("turn_count") == 1, str(items[0]))
        check("切换当前对话", st2.set_active(conv.id) and st2.active_id() == conv.id)

        # ---------- 4. 安全边界 ----------
        print("\n[4] 安全边界 · id 是外部输入")
        for bad in ("../../config", "..\\..\\config", "a/b", "", "x" * 200):
            try:
                st2.get(bad)
                leaked = st2._path_exists(bad)
                check(f"非法 id 被拒：{bad[:20]!r}", not leaked)
            except ValueError:
                check(f"非法 id 抛错：{bad[:20]!r}", True)
            except Exception as exc:  # noqa: BLE001
                check(f"非法 id 被拒：{bad[:20]!r}", False, repr(exc))

        check("get 对非法 id 返回 None", st2.get("../../etc/passwd") is None)
        check("delete 对非法 id 返回 False", st2.delete("../../etc/passwd") is False)

        # ---------- 5. 容错 ----------
        print("\n[5] 容错 · 一个坏文件不该拖垮列表")
        (root / "20260101-000000-dead.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
        items2 = st2.list_summaries()
        check("坏文件被跳过，其余仍可读", len(items2) == 3, str(len(items2)))
        check("坏文件 get 返回 None", st2.get("20260101-000000-dead") is None)

        # 索引损坏 → 仍能从实体恢复
        (root / INDEX_NAME).write_text("坏掉的索引", encoding="utf-8")
        items3 = st2.list_summaries()
        check("索引损坏后仍能列出对话", len(items3) == 3, str(len(items3)))
        n = st2.rebuild_index()
        check("rebuild_index 重建出 3 条", n == 3, str(n))
        check("重建后 active 仍指向存在的对话", bool(st2.active_id()))

        # ---------- 6. 删除语义 ----------
        print("\n[6] 删除语义")
        st2.set_active(conv3.id)
        check("删除当前对话", st2.delete(conv3.id) is True)
        check("删除后文件消失", not (root / f"{conv3.id}.json").exists())
        check("删除后 active 自动切到别的对话",
              st2.active_id() and st2.active_id() != conv3.id, st2.active_id())
        check("列表剩 2 条", len(st2.list_summaries()) == 2, str(len(st2.list_summaries())))

        # 删到只剩 0 个时，必须自动补一个 —— 界面不必处理「零对话」这个态
        for item in list(st2.list_summaries()):
            st2.delete(str(item["id"]))
        check("删空后自动新建一个对话", len(st2.list_summaries()) == 1,
              str(len(st2.list_summaries())))
        check("删空后 active 有效", bool(st2.active_id()), st2.active_id())

        # ---------- 7. 边界输入 ----------
        print("\n[7] 边界输入")
        check("auto_title 空串", ConversationStore.auto_title("") == "新对话")
        check("auto_title 取首行", ConversationStore.auto_title("第一行\n第二行") == "第一行")
        long_title = ConversationStore.auto_title("很" * 100)
        check("auto_title 截断", len(long_title) <= 25, str(len(long_title)))
        check("new_id 唯一",
              len({ConversationStore.new_id() for _ in range(50)}) == 50)

        # 消息里带畸形数据 → 跳过该条，不炸
        conv4 = st.create()
        path4 = root / f"{conv4.id}.json"
        raw = json.loads(path4.read_text(encoding="utf-8"))
        raw["messages"] = [{"role": "user", "text": "正常"}, {"role": "user"}, "不是字典"]
        path4.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        got = st.get(conv4.id)
        check("畸形消息被跳过而非报错",
              got is not None and len(got.messages) == 1,
              str(len(got.messages) if got else -1))

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
