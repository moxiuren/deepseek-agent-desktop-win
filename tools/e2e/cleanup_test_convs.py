#!/usr/bin/env python3
"""批量清理测试会话 —— 走界面真实链路，拟人节奏。

## 为什么不用 `engine/tools/cleanup_test_sessions.py`

那个脚本直接打 `session/delete` 接口，绕过界面。它快，但**测不到东西**：
界面上的确认框、菜单禁用态、列表刷新、解绑提示、错误兜底全都绕过了。
而当前任务正是「以用户身份做端到端测试」，所以必须走界面。

## 拟人约束（用户明确要求）

  · 逐条删，**不并发** —— 一次一个菜单、一次一个确认框
  · 间隔随机 1.6–3.4 秒，不是固定值（固定间隔本身就是机器特征）
  · 每删一条先滚动到该行 —— 真实用户不会点看不见的东西
  · 每 10 条读一次风控状态，出现 `accountSuspended` 或熔断升级立刻停

## 安全闸门

  · 待删清单**先落盘**（`_cleanup_plan.json`），删了什么可回溯
  · 只删清单内的 `remote_id`，绝不按索引删（索引会因刷新而漂移）
  · 清单里不含「保留」「存疑」两类 —— 那 4 条不动
  · `--dry-run` 只开菜单不开删，验证定位逻辑

用法：
    python tools/e2e/cleanup_test_convs.py --dry-run
    python tools/e2e/cleanup_test_convs.py --execute
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ws_cdp import CdpPage  # noqa: E402

CDP_PORT = 9333
PLAN_FILE = Path(__file__).resolve().parent / "_cleanup_plan.json"
RUN_LOG = Path(__file__).resolve().parent / "_cleanup_run.log"


# ---------------------------------------------------------------- 分类

def classify(title: str, active: bool) -> tuple[str, str]:
    """返回 (桶, 理由)。只有「测试」桶会被删。

    **白名单式判定**：只有明确命中已知测试模式的才进「测试」桶，
    其余一律「存疑/保留」。反过来写（命中「不像测试」才保留）会在
    列表变化时静默扩大删除面 —— 而删除是不可逆的。

    当前已识别的测试族（按出现时间倒序）：
      · 数数 / 求和 / 一加一  —— 早期连通性与流式输出测试
      · 暗号族（读取暗号 / 日志暗号 / 桥验证暗号 / 暗号确认 / 暗号提取）
        —— 桥接链路验证，注入固定暗号后回读
      · 空白图片族 —— 附件上传测试（空图/无内容图）
      · 单字回复族（单字回复行 / 回复好 / 喵）—— 最小响应测试
    """
    if active:
        return "保留", "当前正在看的对话"

    # ---- 明确保留：疑似真实内容 ----
    if title in ("Dry ice 1.5 ton price", "只回好字", "Live2D提示词生成指南"):
        return "存疑", "疑似真实内容，保留待人工判断"

    # ---- 已知测试族 ----
    if title in ("城市光污染文章", "城市夜间光污染文章", "城市夜间光污染"):
        return "测试", "光污染测试（多轮）"
    if title in ("列出目录并读首行", "列出目录并读取首行"):
        return "测试", "工具调用测试"
    if title == "附件校验码读取":
        return "测试", "附件上传测试"
    if title == "哈希表一句话解释":
        return "测试", "普通问答测试"

    # 暗号族：桥接链路验证（10-18 10:58–11:51 一批）
    if "暗号" in title:
        return "测试", "桥接暗号验证测试"
    # 空白图片族：附件上传边界测试
    if title.startswith("空白图片"):
        return "测试", "空白图片上传测试"
    # 蓝鲸爱吃菠萝包：暗号族同一批的注入内容（10:58，紧邻「暗号提取」）
    if title == "蓝鲸爱吃菠萝包":
        return "测试", "暗号注入内容（与暗号族同批）"
    # 单字回复族
    if title in ("单字回复行", "回复好", "喵"):
        return "测试", "最小响应测试"

    if re.match(r"^(数|1到40|输出数字|只输出数字|数数|数到|一加一|9\.9|"
                r"数字42|确认收到|内部代号|Say again|打招呼|模型已切换|"
                r"日期查询|执行echo|从1到|从 1 到)", title):
        return "测试", "早期连通性测试"
    if "旅程" in title:
        return "测试", "旅程测试"
    return "存疑", "无法归类，保留"


# ---------------------------------------------------------------- 探针

JS_SNAPSHOT = """
() => Array.from(document.querySelectorAll('#chatList .chat')).map(r => ({
  title: ((r.querySelector('.t') || {}).textContent || '').trim(),
  sub:   ((r.querySelector('.m') || {}).textContent || '').trim(),
  remoteId: r.dataset.id || null,
  localId:  r.dataset.localId || null,
  active:   r.classList.contains('on'),
}))
"""

JS_OPEN_MENU = """
(rid) => {
  const rows = Array.from(document.querySelectorAll('#chatList .chat'));
  const row = rows.find(r => r.dataset.id === rid);
  if (!row) return { ok: false, why: 'row-gone' };
  // 真实用户会先让目标进视野再点
  row.scrollIntoView({ block: 'center' });
  const more = row.parentElement.querySelector('.chat-more');
  if (!more) return { ok: false, why: 'no-more-btn' };
  more.click();
  return { ok: true };
}
"""

JS_CLICK_DELETE = """
() => {
  const m = document.querySelector('[role="menu"]');
  if (!m) return { ok: false, why: 'no-menu' };
  const it = Array.from(m.querySelectorAll('[role="menuitem"], button'))
    .find(e => (e.textContent || '').includes('从账号里删除'));
  if (!it) return { ok: false, why: 'no-delete-item' };
  if (it.disabled) return { ok: false, why: 'disabled' };
  it.click();
  return { ok: true };
}
"""

JS_DIALOG_STATE = """
() => {
  const mask = document.getElementById('dlgMask');
  return {
    open: mask ? !mask.hidden : false,
    title: (document.getElementById('dlgTitle') || {}).textContent || '',
    text: (document.getElementById('dlgText') || {}).innerText || '',
    okText: (document.getElementById('dlgOk') || {}).textContent || '',
  };
}
"""

JS_CONFIRM = """
() => {
  const b = document.getElementById('dlgOk');
  if (!b) return { ok: false };
  b.click();
  return { ok: true };
}
"""

JS_STATUS = """
() => {
  // 状态条上的风控读数。没有就是「未知」，不硬编一个假值。
  const t = document.body.innerText || '';
  const susp = t.includes('账号被处置') || t.includes('账号异常');
  const halt = t.includes('熔断');
  return { suspended: susp, halted: halt };
}
"""

JS_CANCEL = """
() => {
  const b = document.getElementById('dlgCancel');
  if (b) { b.click(); return { ok: true }; }
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  return { ok: true, via: 'esc' };
}
"""


def human_pause(lo: float = 1.6, hi: float = 3.4) -> float:
    """拟人停顿。固定间隔本身就是机器特征，所以取随机。"""
    d = random.uniform(lo, hi)
    time.sleep(d)
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真的删除")
    ap.add_argument("--dry-run", action="store_true", help="只开菜单，不删")
    ap.add_argument("--max", type=int, default=0, help="最多删几条（0=全部）")
    a = ap.parse_args()
    if not a.execute and not a.dry_run:
        print("必须指定 --dry-run 或 --execute")
        return 2

    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3))
    pages = [t for t in d if t.get("type") == "page"]
    if not pages:
        print(f"[错误] CDP {CDP_PORT} 上没有页面")
        return 2

    log_lines: list[str] = []

    def log(s: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} {s}"
        print(line)
        log_lines.append(line)

    with CdpPage(pages[0]["webSocketDebuggerUrl"]) as p:
        def ev(src: str, *args):
            if args:
                return p.evaluate(f"({src})({json.dumps(args[0])})")
            return p.evaluate(f"({src})()")

        rows = ev(JS_SNAPSHOT)
        log(f"当前 {len(rows)} 条会话")

        # ---- 建计划 ----
        plan: list[dict] = []
        keep: list[dict] = []
        for r in rows:
            bucket, why = classify(r["title"], r["active"])
            item = {**r, "bucket": bucket, "why": why}
            (plan if bucket == "测试" else keep).append(item)

        PLAN_FILE.write_text(json.dumps({
            "created": datetime.now().isoformat(timespec="seconds"),
            "to_delete": plan,
            "kept": keep,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

        log(f"计划：删 {len(plan)} 条 / 保留 {len(keep)} 条 -> {PLAN_FILE.name}")
        for k in keep:
            log(f"  保留 [{k['bucket']}] {k['title'][:36]} · {k['why']}")

        if a.dry_run:
            log("=== dry-run：只验证定位，不删除 ===")
            ok = fail = 0
            for it in plan:
                r = ev(JS_OPEN_MENU, it["remoteId"])
                if r.get("ok"):
                    ok += 1
                else:
                    fail += 1
                    log(f"  [定位失败] {it['title'][:30]} · {r.get('why')}")
                ev(JS_CANCEL)
                time.sleep(0.15)
            log(f"dry-run 结果：定位成功 {ok} / 失败 {fail}")
            RUN_LOG.write_text("\n".join(log_lines), encoding="utf-8")
            return 0 if fail == 0 else 1

        # ---- 真删 ----
        target = plan[:a.max] if a.max > 0 else plan
        log(f"=== 开始删除 {len(target)} 条（拟人间隔 1.6-3.4s）===")

        done = failed = 0
        for i, it in enumerate(target, 1):
            rid = it["remoteId"]
            title = it["title"][:30]

            r = ev(JS_OPEN_MENU, rid)
            if not r.get("ok"):
                log(f"  [{i}/{len(target)}] [跳过] {title} · {r.get('why')}")
                failed += 1
                continue

            time.sleep(random.uniform(0.25, 0.55))   # 菜单展开的人类反应时间

            r = ev(JS_CLICK_DELETE)
            if not r.get("ok"):
                log(f"  [{i}/{len(target)}] [跳过] {title} · {r.get('why')}")
                failed += 1
                ev(JS_CANCEL)
                continue

            time.sleep(random.uniform(0.35, 0.8))    # 读确认文案

            dlg = ev(JS_DIALOG_STATE)
            if not dlg.get("open"):
                log(f"  [{i}/{len(target)}] [跳过] {title} · 确认框没出现")
                failed += 1
                continue

            ev(JS_CONFIRM)
            done += 1
            log(f"  [{i}/{len(target)}] 已确认删除 · {title}")

            # 等列表刷新（服务端往返 + 重绘）
            time.sleep(random.uniform(1.0, 2.0))
            human_pause()

            # 每 10 条查一次风控
            if i % 10 == 0:
                st = ev(JS_STATUS)
                log(f"  --- 风控自检 @{i}：{st} ---")
                if st.get("suspended") or st.get("halted"):
                    log("  [中止] 检测到风控信号，停止删除")
                    break

        rows_after = ev(JS_SNAPSHOT)
        log(f"=== 完成：确认 {done} / 跳过 {failed} · 剩余 {len(rows_after)} 条 ===")

    RUN_LOG.write_text("\n".join(log_lines), encoding="utf-8")
    log(f"日志 -> {RUN_LOG.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
