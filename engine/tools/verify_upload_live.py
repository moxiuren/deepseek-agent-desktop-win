"""真链路验证：附件上传（双 PoW + multipart）。

**这会真的发一条带附件的消息到 DeepSeek**，消耗一次极小的额度。
与 `tests/test_upload.py` 的分工：
  * 那个全用桩，验的是「我们发出的请求形状对不对」
  * **这个真打接口**，验的是「服务端认不认」—— 桩再真也只是桩

为什么必须单独有这个脚本：交接文档 §8 列了 7 项未验证，全都只能靠真实请求
才能问清楚。单元测试全绿证明不了 `x-file-size` 的单位、`ref_file_ids` 的
字段名、`FileParseStatus` 的实际取值 —— 那些是服务端的契约，不是我们的。

用法：
    cd engine && ./.venv/Scripts/python.exe tools/verify_upload_live.py

**注意**：跑之前确认当前 `.token` 属于哪个账号 —— 脚本会打印出来。
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

# 必须先设闸门，再 import 引擎模块 —— 闸门是在 `send()` 里读的，
# 但显式放在最前面能让人一眼看见「这个脚本要打真实接口」。
os.environ["GAD_ALLOW_LIVE_AUTH"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.identity import load_token  # noqa: E402
from gad_engine.session import SessionMeta  # noqa: E402
from gad_engine.transport.deepseek_rpc import (  # noqa: E402
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


def make_sample(dirpath: Path) -> Path:
    """造一个内容确定的测试文件。

    内容里放一句**只有读懂了才能回答**的话，这样模型的回复就能证明
    「文本确实被提取并送进了上下文」—— 否则附件可能只是挂上去没生效。
    """
    p = dirpath / "上传验证样本.txt"
    p.write_text(
        "这是一份用于验证附件上传链路的测试文件。\n"
        "文件里唯一的暗号是：蓝鲸爱吃菠萝包。\n"
        "如果模型能说出这个暗号，说明文本提取与上下文注入都成功了。\n",
        encoding="utf-8",
    )
    return p


async def main() -> int:
    print("=" * 70)
    print("真链路验证 · 附件上传（双 PoW + multipart）")
    print("=" * 70)

    token = load_token()
    if not token:
        print("[错误] 没有 .token，先登录")
        return 2
    print(f"\ntoken: {token[:6]}***  长度 {len(token)}")

    print("\n[0] 确认账号")
    try:
        info = await fetch_account_info_async(token)
        print(f"  邮箱: {info.get('email')}")
        print(f"  账号 id: {info.get('account_id')}")
        print(f"  禁言: {info.get('is_muted')}  {info.get('mute_until_text') or ''}")
        check("账号未被禁言", not info.get("is_muted"), str(info))
    except Exception as exc:  # noqa: BLE001
        check("读到账号信息", False, f"{type(exc).__name__}: {exc}")
        return 2

    with tempfile.TemporaryDirectory(prefix="gad-upload-") as tmp:
        sample = make_sample(Path(tmp))
        size = sample.stat().st_size
        print(f"\n[1] 测试文件：{sample.name}（{size} 字节）")

        tp = DeepSeekRpcTransport(
            token=token,
            # 关掉预取：本轮要看的就是「上传 PoW 真的解了」，
            # 预取只影响 completion 那份，不影响这里，但关掉能让计时更干净。
            pow_prefetch=False,
        )
        await tp.start()
        try:
            sess = SessionMeta(temporary=True)
            print("\n[2] 真发一条带附件的消息（含真实上传 + 真实对话）")
            t0 = time.monotonic()
            try:
                outcome = await tp.send(
                    "请只回答文件里的那句暗号，不要解释。",
                    sess,
                    temporary=True,
                    files=[str(sample)],
                )
            except Exception as exc:  # noqa: BLE001
                print(f"\n  [异常] {type(exc).__name__}: {exc}")
                import traceback

                traceback.print_exc()
                return 1
            elapsed = (time.monotonic() - t0) * 1000.0

            print(f"\n  总耗时: {elapsed:.0f} ms")
            print(f"  上传耗时: {outcome.upload_ms:.0f} ms")
            print(f"  正文: {outcome.text[:200]!r}")
            print(f"  错误: {outcome.error!r}")
            print(f"  cid: {outcome.cid}")

            print("\n[3] 断言")
            check("本轮没有错误", not outcome.error, outcome.error)
            check("上传耗时 > 0（说明真解了上传 PoW）", outcome.upload_ms > 0,
                  f"{outcome.upload_ms}")
            check("uploaded_files 非空", bool(outcome.uploaded_files),
                  str(outcome.uploaded_files))
            if outcome.uploaded_files:
                f = outcome.uploaded_files[0]
                print(f"\n  附件详情: {json.dumps(f, ensure_ascii=False, indent=2)}")
                check("服务端回了 file id", bool(f.get("id")), str(f))
                check("file_name 回显正确", f.get("file_name") == sample.name,
                      f"期望 {sample.name}，得到 {f.get('file_name')!r}")
                check("file_size 与本地一致", f.get("file_size") == size,
                      f"期望 {size}，得到 {f.get('file_size')!r}")
                print(f"\n  ★ status 实际值: {f.get('status')!r}  ← 交接文档 §8 未查清项")
            check("正文非空", bool(outcome.text.strip()), repr(outcome.text[:80]))
            # 暗号命中 = 文本提取 + 上下文注入都成功
            check("正文含暗号「蓝鲸爱吃菠萝包」",
                  "蓝鲸爱吃菠萝包" in (outcome.text or ""),
                  f"实际正文: {outcome.text[:200]!r}")
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
