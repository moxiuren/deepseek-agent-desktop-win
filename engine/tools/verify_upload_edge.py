"""真链路验证 · 附件上传的边界与未验证项。

`verify_upload_live.py` 验的是「主干能通」；这个脚本专门问交接文档 §8
剩下的那几个问题 —— 它们都不是「能不能跑」的问题，而是**契约细节**：

  1. 上传 PoW 的 difficulty 是不是也是 144000？
  2. referer 用根路径 `/` 行不行（还是必须用会话 URL）？
  3. 多文件同时挂行不行？`ref_file_ids` 是不是真的按顺序对应？
  4. 非文本文件（二进制）会怎样？
  5. 空文件 / 不存在的文件是否在发请求前就被拦下？
  6. 上传后立刻查 `fetch_files`，`status` 会怎么变？

用法：
    cd engine && ./.venv/Scripts/python.exe tools/verify_upload_edge.py

**会真实上传若干文件、真实发 1-2 条消息。**
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["GAD_ALLOW_LIVE_AUTH"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.identity import load_token  # noqa: E402
from gad_engine.session import SessionMeta  # noqa: E402
from gad_engine.transport.deepseek_rpc import (  # noqa: E402
    BASE,
    COMPLETION_PATH,
    UPLOAD_PATH,
    DeepSeekRpcTransport,
    fetch_account_info_async,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def section(title: str) -> None:
    print(f"\n{'─' * 70}\n{title}\n{'─' * 70}")


async def probe_challenges(token: str, tp: DeepSeekRpcTransport) -> None:
    """直接抓两份 challenge 对比 difficulty —— 这是纯读取，不发消息。"""
    section("[1] 两份 challenge 的 difficulty 对比")

    from curl_cffi import requests as creq
    from gad_engine.transport.deepseek_rpc import IMPERSONATE

    s = creq.Session(impersonate=IMPERSONATE)
    try:
        for label, path in (("completion", COMPLETION_PATH), ("upload_file", UPLOAD_PATH)):
            ch = tp._fetch_challenge(s, token, BASE + "/", target_path=path)
            print(
                f"  {label:12s} difficulty={ch.difficulty:>8d}  "
                f"algorithm={ch.algorithm}  expire_at={ch.expire_at}"
            )
            if label == "upload_file":
                check(
                    "上传 PoW difficulty 与 completion 相同（144000）",
                    ch.difficulty == 144000,
                    f"实际 {ch.difficulty}",
                )
    finally:
        s.close()


async def probe_referer(token: str, tp: DeepSeekRpcTransport, sample: Path) -> None:
    """用会话 URL 作 referer 上传，看是否与根路径有差别。

    第一次验证用的是 `BASE + "/"`，成功了 —— 说明根路径可用。
    这里再试会话 URL，如果也成功，说明 referer **不参与校验**，
    那就不用为「必须先建会话」增加耦合。
    """
    section("[2] referer 换成会话 URL 是否也接受")

    from curl_cffi import requests as creq
    from gad_engine.transport.deepseek_rpc import IMPERSONATE

    s = creq.Session(impersonate=IMPERSONATE)
    try:
        # 直接复用引擎的实现 —— 自己拼一遍只会重复它已经处理好的字段路径
        # （`biz_data.chat_session.id`，不是 `biz_data.id`；我第一次就踩了这个）。
        cid = tp._create_session(s, token)
        print(f"  会话 cid: {cid or '(创建失败)'}")

        if not cid:
            print("  [跳过] 没能建会话")
            return

        referer = f"{BASE}/a/chat/s/{cid}"
        try:
            biz = tp._upload_file(s, token, referer, str(sample))
            print(f"  会话 referer 上传结果: id={biz.get('id')} status={biz.get('status')}")
            check("会话 URL 作 referer 也能上传", bool(biz.get("id")), str(biz))
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {type(exc).__name__}: {exc}")
            check("会话 URL 作 referer 也能上传", False, str(exc)[:120])
    finally:
        s.close()


async def probe_fetch_files(token: str, tp: DeepSeekRpcTransport, file_id: str) -> None:
    """上传后立刻查 fetch_files，看 status 怎么变。"""
    section("[3] fetch_files 轮询：status 的变化")

    from curl_cffi import requests as creq
    from gad_engine.transport.deepseek_rpc import FETCH_FILES_PATH, IMPERSONATE

    s = creq.Session(impersonate=IMPERSONATE)
    try:
        for i in range(4):
            r = s.get(
                BASE + FETCH_FILES_PATH,
                headers=tp._headers(token, BASE + "/"),
                params={"file_ids": file_id},
                timeout=20,
            )
            try:
                body = r.json()
            except ValueError:
                print(f"  第 {i + 1} 次: HTTP {r.status_code} 非 JSON: {r.text[:120]!r}")
                break
            biz = ((body.get("data") or {}).get("biz_data")) or {}
            files = biz.get("files") or biz.get("file_list") or []
            print(f"  第 {i + 1} 次 (HTTP {r.status_code}): {json.dumps(body, ensure_ascii=False)[:400]}")
            if files:
                st = files[0].get("status")
                print(f"    → status={st!r}")
            if i < 3:
                time.sleep(2)
    finally:
        s.close()


async def probe_multi_file(token: str, tp: DeepSeekRpcTransport, dirpath: Path) -> None:
    """多文件 + 二进制 + 边界。"""
    section("[4] 多文件 / 二进制 / 边界")

    a = dirpath / "样本A.txt"
    a.write_text("暗号甲：红色犀牛。", encoding="utf-8")
    b = dirpath / "样本B.txt"
    b.write_text("暗号乙：绿色孔雀。", encoding="utf-8")

    sess = SessionMeta(temporary=True)
    print("  发送两个文本文件，要求同时说出两个暗号…")
    t0 = time.monotonic()
    outcome = await tp.send(
        "请只回答两个文件里各自的暗号，格式：甲=… 乙=…",
        sess,
        temporary=True,
        files=[str(a), str(b)],
    )
    elapsed = (time.monotonic() - t0) * 1000.0
    print(f"  总耗时 {elapsed:.0f} ms / 上传 {outcome.upload_ms:.0f} ms")
    print(f"  正文: {outcome.text[:200]!r}")
    print(f"  附件数: {len(outcome.uploaded_files)}")
    for i, f in enumerate(outcome.uploaded_files):
        print(f"    [{i}] id={f.get('id')} name={f.get('file_name')} status={f.get('status')}")

    check("两个文件都上传了", len(outcome.uploaded_files) == 2, str(outcome.uploaded_files))
    check("两个暗号都出现在正文",
          "红色犀牛" in (outcome.text or "") and "绿色孔雀" in (outcome.text or ""),
          repr(outcome.text[:200]))

    # --- 边界：空文件 ---
    print("\n  [边界] 空文件")
    empty = dirpath / "空.txt"
    empty.write_bytes(b"")
    try:
        await tp.send("x", SessionMeta(temporary=True), files=[str(empty)])
        check("空文件被拦下", False, "竟然发出去了")
    except Exception as exc:  # noqa: BLE001
        print(f"    {type(exc).__name__}: {exc}")
        check("空文件被拦下", True)

    # --- 边界：不存在的文件 ---
    print("\n  [边界] 不存在的文件")
    try:
        await tp.send("x", SessionMeta(temporary=True), files=[str(dirpath / "根本没有.txt")])
        check("不存在的文件被拦下", False, "竟然发出去了")
    except Exception as exc:  # noqa: BLE001
        print(f"    {type(exc).__name__}: {exc}")
        check("不存在的文件被拦下", True)


async def probe_binary(token: str, tp: DeepSeekRpcTransport, dirpath: Path) -> None:
    """二进制文件：服务端只提取文本，那二进制会怎样？"""
    section("[5] 二进制文件")

    png = dirpath / "测试图片.png"
    # 1x1 透明 PNG 的最小合法字节
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c6300010000050001"
            "0d0a2db4"
            "0000000049454e44ae426082"
        )
    )
    print(f"  文件: {png.name} ({png.stat().st_size} 字节)")

    sess = SessionMeta(temporary=True)
    try:
        outcome = await tp.send(
            "这个附件里有什么？一句话。",
            sess,
            temporary=True,
            files=[str(png)],
        )
        print(f"  正文: {outcome.text[:200]!r}")
        for f in outcome.uploaded_files:
            print(f"  id={f.get('id')} status={f.get('status')} size={f.get('file_size')}")
        check("二进制文件也能上传", bool(outcome.uploaded_files), str(outcome.error))
        print("  ★ 注意：能上传 ≠ 能被理解，看正文判断")
    except Exception as exc:  # noqa: BLE001
        print(f"  [失败] {type(exc).__name__}: {exc}")
        check("二进制文件也能上传", False, str(exc)[:150])


async def main() -> int:
    print("=" * 70)
    print("真链路验证 · 附件上传的边界与未验证项")
    print("=" * 70)

    token = load_token()
    if not token:
        print("[错误] 没有 .token")
        return 2

    info = await fetch_account_info_async(token)
    print(f"\n账号: {info.get('email')}  禁言: {info.get('is_muted')}")
    if info.get("is_muted"):
        print("[中止] 账号被禁言，测不了")
        return 2

    tp = DeepSeekRpcTransport(token=token, pow_prefetch=False)
    await tp.start()
    try:
        await probe_challenges(token, tp)

        with tempfile.TemporaryDirectory(prefix="gad-edge-") as tmp:
            d = Path(tmp)
            sample = d / "referer样本.txt"
            sample.write_text("暗号：黄色老虎。", encoding="utf-8")

            await probe_referer(token, tp, sample)
            await probe_multi_file(token, tp, d)
            await probe_binary(token, tp, d)
    finally:
        await tp.stop()

    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
