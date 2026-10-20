"""阶段 0.3 —— 存活观测探针。

**目的**：回答「补上四缺口后，DeepSeek 的自动风控还会不会把账号临时封掉」。

设计要点（每一条都是为了让这次观测**可信**且**不伤害账号**）
----------------------------------------------------------

1. **只用历史数据当 A 组基线，不前瞻跑 A 组。**
   原版（裸 TLS + 写死 UA 133）「跑没多久就封号」是既有事实，重跑一遍等于
   **故意把账号封掉** —— 没必要。本探针只跑 B 组（本方案：`curl_cffi` chrome150
   + 对齐 UA/locale + 正确回传），与历史结果对照。
   > 代价要说清：这是**非同期对照**，不是严格 A/B。若 B 组存活而 A 组历史被封，
   > 只能说明「本方案至少没重现那个故障」，不能证明差异全部来自指纹。

2. **发现异常立即停。** 一旦命中封禁信号（连续 401/403），写 `halt` 标志，
   此后每次运行**直接退出**，不再碰账号 —— 不做「多试几次看会不会好」。

3. **节律像人，不像工具环。** 每次只发一条极短消息，间隔由外部调度控制（默认 2 小时）。
   原版限流来自「工具环 2~5 秒连发」，这里完全不是那个形态。

4. **复用会话，减少痕迹。** 默认在同一个会话里连续追问（更接近真人），
   每 12 轮或遇错才换新会话 —— 72 小时只会在账号里留下个位数会话。

用法
----
    cd engine
    ./.venv/Scripts/python.exe tools/survival_probe.py --once        # 跑一次（自动化用）
    ./.venv/Scripts/python.exe tools/survival_probe.py --status      # 看累计状态
    ./.venv/Scripts/python.exe tools/survival_probe.py --reset-halt  # 人工确认后解除 halt

退出码：0 正常 / 3 命中封禁信号（已 halt）/ 4 凭据失效 / 5 其它错误 / 6 已 halt 跳过
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.identity import IdentityError, resolve_token
from gad_engine.session import SessionMeta
from gad_engine.transport import DeepSeekRpcTransport, TransportError

ROOT = Path(__file__).resolve().parent.parent.parent
EVID = ROOT / "docs" / "evidence"
STATE_FILE = EVID / "survival-state.json"
LOG_FILE = EVID / "survival-log.jsonl"

MAX_TURNS_PER_SESSION = 12
AUTH_FAIL_HALT_THRESHOLD = 2   # 连续 2 次认证失败 → halt
BUSY_BACKOFF_THRESHOLD = 3     # 连续 3 次「服务端忙」→ 冷却
BUSY_COOLDOWN_HOURS = 6        # 冷却时长

# 轮换的短提示词 —— 固定单句重复 36 次本身就是机器特征，轮换更像真人
_PROMPTS = (
    "1+1=?",
    "今天星期几？",
    "用一句话解释什么是递归",
    "3 的平方是多少？",
    "100 减 37 等于多少？",
    "水的沸点是多少度？",
    "帮我写一句简短的祝福语",
    "推荐一个学英语的方法",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def _is_busy(err: str) -> bool:
    """服务端忙/生成超时 —— **不是封禁**，但说明该退避了。

    实测（2026-10-15 08:35~08:39）：`event: hint` 回
    `{"type":"error","content":"Server busy, please try again later.",
      "finish_reason":"generation_timeout"}`，固定 ~68s 后触发。
    同一份代码 08:26 尚可、08:39 复现失败 ⇒ 与客户端无关，是服务端侧状态。
    """
    e = err or ""
    return "Server busy" in e or "generation_timeout" in e or "服务端错误" in e


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(st, dict):
                st.setdefault("halt", False)
                st.setdefault("halt_reason", "")
                st.setdefault("consecutive_auth_fail", 0)
                st.setdefault("consecutive_busy", 0)
                st.setdefault("cooldown_until", "")
                st.setdefault("session", {})
                st.setdefault("runs", 0)
                st.setdefault("ok", 0)
                st.setdefault("fail", 0)
                st.setdefault("first_run_at", "")
                st.setdefault("last_run_at", "")
                return st
        except ValueError:
            pass
    return {
        "halt": False,
        "halt_reason": "",
        "consecutive_auth_fail": 0,
        "consecutive_busy": 0,
        "cooldown_until": "",
        "session": {},
        "runs": 0,
        "ok": 0,
        "fail": 0,
        "first_run_at": "",
        "last_run_at": "",
    }


def save_state(st: dict) -> None:
    EVID.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(rec: dict) -> None:
    EVID.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def classify_error(exc: Exception) -> tuple[str, bool]:
    """返回 (类别, 是否命中封禁信号)。"""
    if isinstance(exc, TransportError):
        if exc.auth:
            return "auth_or_blocked", True
        if exc.fatal:
            return "fatal", False
        msg = str(exc)
        if "429" in msg or "限流" in msg:
            return "rate_limited", False
        return "transport_error", False
    return "unexpected", False


async def probe_once(token: str, st: dict) -> tuple[dict, int]:
    """跑一次探测。返回 (记录, 退出码)。"""
    sess_state = st.get("session") or {}
    turns = int(sess_state.get("turns", 0))
    # 轮次到顶或上次出错 → 换新会话
    if turns >= MAX_TURNS_PER_SESSION or sess_state.get("needs_rotate"):
        log(f"  [会话] 轮换（已 {turns} 轮）")
        sess_state = {"turns": 0, "needs_rotate": False}

    session = SessionMeta(
        cid=sess_state.get("cid", ""),
        rid=sess_state.get("rid", ""),
        rcid=sess_state.get("rcid", ""),
        temporary=False,  # 观测要留痕，便于事后在网页版核对
        turns=turns,
    )
    prompt = _PROMPTS[st["runs"] % len(_PROMPTS)]

    tp = DeepSeekRpcTransport(token=token)
    await tp.start()
    started = time.monotonic()
    rec: dict = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "run": st["runs"],  # run() 里已自增过，别再加一
        "prompt": prompt,
        "new_session": not session.cid,
    }
    try:
        out = await tp.send(prompt, session)
        rec.update(
            {
                "kind": "ok" if out.ok else "empty_reply",
                "http": 200,
                "elapsed_s": round(time.monotonic() - started, 1),
                "chars": len(out.text),
                "cid": out.cid,
                "rid": out.rid,
                "error": out.error,
            }
        )
        # 会话状态回写
        sess_state.update(
            {
                "cid": out.cid,
                "rid": out.rid,
                "rcid": out.rcid,
                "turns": session.turns,
                "needs_rotate": bool(out.error),
            }
        )
        st["session"] = sess_state
        code = 0 if out.ok else 5
        return rec, code
    except Exception as exc:  # noqa: BLE001
        kind, is_block = classify_error(exc)
        rec.update(
            {
                "kind": kind,
                "elapsed_s": round(time.monotonic() - started, 1),
                "error": str(exc)[:400],
            }
        )
        sess_state["needs_rotate"] = True
        st["session"] = sess_state
        if is_block:
            return rec, 3
        if kind == "rate_limited":
            return rec, 5
        return rec, 5
    finally:
        await tp.stop()


async def run(args) -> int:
    st = load_state()

    if args.reset_halt:
        st["halt"] = False
        st["halt_reason"] = ""
        st["consecutive_auth_fail"] = 0
        save_state(st)
        log("[halt] 已人工解除。")
        return 0

    if args.status:
        return show_status(st)

    if st.get("halt"):
        log(f"[已 halt] {st.get('halt_reason')} —— 本次跳过，不触碰账号。")
        log("         确认账号恢复后用 --reset-halt 解除。")
        return 6

    # 冷却期：连续「服务端忙」后主动退避，别在对方过载时继续打
    cd_until = st.get("cooldown_until") or ""
    if cd_until:
        try:
            until = datetime.fromisoformat(cd_until)
        except ValueError:
            until = None
        if until and datetime.now() < until:
            left = (until - datetime.now())
            mins = int(left.total_seconds() // 60)
            log(f"[冷却中] 到 {cd_until}（还剩约 {mins} 分钟）—— 本次跳过。")
            return 6
        st["cooldown_until"] = ""
        st["consecutive_busy"] = 0
        log("[冷却] 已到期，恢复观测。")

    log("=" * 68)
    log(f"阶段 0.3 存活观测 —— 第 {st['runs'] + 1} 次")
    log("=" * 68)

    try:
        token, src = resolve_token(explicit=args.token, cdp_port=args.cdp_port)
    except IdentityError as exc:
        log(f"[错误] 取 token 失败：{exc}")
        return 4
    log(f"[凭据] 来源 {src} · len={len(token)}")

    st["runs"] += 1
    st["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    if not st["first_run_at"]:
        st["first_run_at"] = st["last_run_at"]

    rec, code = await probe_once(token, st)

    # 认证失败 → 立即重试一次，排除瞬时抖动；仍失败才 halt
    if code == 3 and not args.no_retry:
        log("  [!] 认证类失败，立即重试一次以排除瞬时抖动 …")
        await asyncio.sleep(3)
        rec2, code2 = await probe_once(token, st)
        if code2 == 3:
            st["consecutive_auth_fail"] += 1
            rec2["retry_of"] = rec["run"]
            append_log(rec2)
            log(f"  [!!] 重试仍失败（{rec2.get('kind')}）→ 命中封禁信号")
        else:
            log(f"  [ok] 重试成功（{rec2.get('kind')}）—— 判定为瞬时抖动，不 halt")
            rec = rec2
            code = code2

    if code == 3:
        if st["consecutive_auth_fail"] >= AUTH_FAIL_HALT_THRESHOLD:
            st["halt"] = True
            st["halt_reason"] = (
                f"{datetime.now().isoformat(timespec='seconds')} "
                f"连续 {st['consecutive_auth_fail']} 次认证失败（第 {rec.get('run')} 次观测）"
            )
            log(f"  [HALT] {st['halt_reason']}")
    elif code == 0:
        st["consecutive_auth_fail"] = 0

    # 服务端忙（生成超时）—— 不是封禁，但说明该退避了，别硬打
    if _is_busy(str(rec.get("error") or "")):
        rec["kind"] = "server_busy"
        st["consecutive_busy"] += 1
        log(f"  [!] 服务端忙（连续第 {st['consecutive_busy']} 次）")
        if st["consecutive_busy"] >= BUSY_BACKOFF_THRESHOLD:
            until = datetime.now() + timedelta(hours=BUSY_COOLDOWN_HOURS)
            st["cooldown_until"] = until.isoformat(timespec="seconds")
            log(f"  [退避] 触发阈值 → 冷却到 {st['cooldown_until']}，其间不再打扰账号")
    elif code == 0:
        st["consecutive_busy"] = 0

    if code == 0:
        st["ok"] += 1
    else:
        st["fail"] += 1

    append_log(rec)
    save_state(st)

    log(f"[结果] kind={rec.get('kind')} · {rec.get('elapsed_s')}s · {rec.get('chars', '-')} 字")
    if rec.get("error"):
        log(f"       error={rec['error'][:200]}")
    log(f"[累计] 共 {st['runs']} 次 · 成功 {st['ok']} · 失败 {st['fail']} · halt={st['halt']}")
    log(f"       日志 {LOG_FILE.name}")
    return code


def show_status(st: dict) -> int:
    log("=" * 68)
    log("阶段 0.3 存活观测 —— 累计状态")
    log("=" * 68)
    log(f"  起止     : {st.get('first_run_at') or '-'} → {st.get('last_run_at') or '-'}")
    log(f"  运行次数 : {st['runs']}（成功 {st['ok']} / 失败 {st['fail']}）")
    log(f"  halt     : {st['halt']}  {st.get('halt_reason') or ''}")
    log(f"  连续认证失败: {st.get('consecutive_auth_fail')}")
    log(f"  连续服务端忙: {st.get('consecutive_busy')}  冷却到: {st.get('cooldown_until') or '-'}")
    log(f"  当前会话 : cid={((st.get('session') or {}).get('cid') or '-')[:8]}… "
        f"turns={(st.get('session') or {}).get('turns', 0)}")

    if LOG_FILE.exists():
        lines = [json.loads(x) for x in LOG_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
        log("")
        log(f"  最近 10 条（共 {len(lines)} 条）：")
        for r in lines[-10:]:
            log(
                f"    [{r.get('ts')}] #{r.get('run')} {r.get('kind'):<16} "
                f"{r.get('elapsed_s', '-'):>6}s {r.get('chars', '-')} 字"
                + (f"  err={str(r.get('error'))[:60]}" if r.get("error") else "")
            )
        kinds: dict[str, int] = {}
        for r in lines:
            k = str(r.get("kind"))
            kinds[k] = kinds.get(k, 0) + 1
        log("")
        log(f"  类别分布 : {kinds}")
    else:
        log("  （还没有日志）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default="")
    ap.add_argument("--cdp-port", type=int, default=9333)
    ap.add_argument("--once", action="store_true", help="跑一次就退出（自动化用，默认行为）")
    ap.add_argument("--status", action="store_true", help="只看累计状态")
    ap.add_argument("--reset-halt", action="store_true", help="人工解除 halt")
    ap.add_argument("--no-retry", action="store_true", help="认证失败时不重试")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log("\n[中断]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
