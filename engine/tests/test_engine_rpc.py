"""引擎入口自测 —— 真起子进程，走 stdio JSON-RPC。

这是**唯一能验证「外壳接得上」的方式**：单独 import 模块只能证明代码没语法错，
证明不了消息格式、事件流、方法名对不对。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_engine_rpc.py
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
PY = str(Path(sys.executable))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'通过' if cond else '失败'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


class Engine:
    """把引擎当子进程驱动。"""

    def __init__(self, config_path: Path | None = None) -> None:
        env = dict(os.environ)
        if config_path is not None:
            # 把子进程的配置读写指到临时文件。
            #
            # **必须这么做**：这个测试要验 `chat.set` 的解析（`"true"` 字符串
            # 要变成 `True`），而 `chat.set` 内部会 `self.cfg.save()` ——
            # 子进程默认加载的是**用户的真实 config.json**，于是「验一下解析」
            # 就把用户的开关真的拨了。2026-10-16 实测踩到：用户的
            # `extended_thinking` 被写成 `true`、`account_label` 被写成
            # 「测试账号」、`pacing`/`circuit` 被写成测试用的 0 值。
            # 入口处认 `GAD_CONFIG_PATH`，见 `config.default_config_path()`。
            env["GAD_CONFIG_PATH"] = str(config_path)
        self.proc = subprocess.Popen(
            [PY, "-m", "gad_engine"],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._id = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        self._id += 1
        msg_id = self._id
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

        deadline = time.monotonic() + timeout
        assert self.proc.stdout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("引擎已退出")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "event":
                self.events.append(msg)
                continue
            if msg.get("id") == msg_id:
                return msg
        raise TimeoutError(f"{method} 超时")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.call("shutdown", timeout=5)
        try:
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def main() -> int:
    print("=" * 70)
    print("引擎入口自测（stdio JSON-RPC）")
    print("=" * 70)

    # 子进程的配置读写全部落到这个临时目录，跑完即删 —— 用户配置零改动。
    # 见 `Engine.__init__` 与 `config.default_config_path()` 的注释。
    sandbox = Path(tempfile.mkdtemp(prefix="gad-rpc-"))
    eng = Engine(config_path=sandbox / "config.json")
    try:
        # ---------- 1. 握手 ----------
        print("\n[1] initialize")
        r = eng.call("initialize")
        res = r.get("result") or {}
        check("返回 result", r.get("type") == "result", str(r)[:200])
        check("协议版本", res.get("protocol") == "1", str(res.get("protocol")))
        check("传输档位 = deepseek_rpc", res.get("transport") == "deepseek_rpc", str(res.get("transport")))
        check("带 identity 快照", isinstance(res.get("identity"), dict), str(res.get("identity")))
        check("identity 不含 token 本体", "token" not in (res.get("identity") or {}), str(res.get("identity")))

        # ---------- 2. 配置 ----------
        print("\n[2] config.*")
        r = eng.call("config.get")
        cfg = (r.get("result") or {}).get("config") or {}
        check("配置含 identity", "identity" in cfg, str(list(cfg.keys()))[:120])
        check("配置无 guest_mode", "guest_mode" not in (cfg.get("identity") or {}), str(cfg.get("identity")))
        r = eng.call("config.path")
        check("config.path 返回路径", str((r.get("result") or {}).get("path", "")).endswith(".json"))

        # ---------- 3. 提示词 ----------
        print("\n[3] prompt.*")
        r = eng.call("prompt.get")
        pres = r.get("result") or {}
        check("模板非空", bool(pres.get("template")))
        check("预览含工具围栏", "```" in str(pres.get("preview", "")))
        check("占位符列表非空", bool(pres.get("placeholders")))
        r = eng.call("prompt.set", {"template": "   "})
        check("空模板被拒（返回 error）", r.get("type") == "error", str(r)[:160])
        r = eng.call("prompt.get")
        check("拒空后模板未被破坏", bool((r.get("result") or {}).get("template")))

        # ---------- 4. 身份 ----------
        print("\n[4] identity.*")
        r = eng.call("identity.status")
        ident = r.get("result") or {}
        check("有 token 状态字段", "has_token" in ident, str(ident))
        check("身份快照不含 token 本体", "token" not in ident)
        check("报告 impersonate", bool(ident.get("impersonate")), str(ident.get("impersonate")))

        # ---------- 5. 状态 ----------
        print("\n[5] status（未启动运行时）")
        r = eng.call("status")
        st = r.get("result") or {}
        check("status 返回 started 字段", "started" in st, str(list(st.keys()))[:120])
        check("未启动时为 False", st.get("started") is False, str(st.get("started")))

        # ---------- 6. 模型与会话形态 ----------
        print("\n[6] models.list / chat.set")
        r = eng.call("models.list")
        mres = r.get("result") or {}
        check("模型列表可返回", isinstance(mres.get("models"), list), str(mres)[:160])
        r = eng.call("chat.set", {"extended_thinking": "true", "temporary": "false"})
        cres = r.get("result") or {}
        check("字符串 'true' 被正确解析", cres.get("extended_thinking") is True, str(cres))
        check("标记 temporary 不受支持", cres.get("temporary_supported") is False, str(cres))

        # ---------- 7. 未知方法 ----------
        print("\n[7] 未知方法")
        r = eng.call("nope.nope")
        check("返回 error 而非崩溃", r.get("type") == "error", str(r)[:160])

        # ---------- 8. 启动运行时 ----------
        print("\n[8] start（真启动：传输层 + 本地执行器）")
        r = eng.call("start", timeout=60)
        sres = r.get("result") or {}
        check("start 返回快照", isinstance(sres, dict) and "transport" in sres, str(sres)[:200])
        tr = sres.get("transport") or {}
        check("传输层已就绪（有 token）", tr.get("ready") is True, str(tr)[:200])
        check("四层闸门快照在", all(k in sres for k in ("identity", "circuit", "pacing", "session")), str(list(sres.keys()))[:160])
        check("熔断快照标注无遥测", (sres.get("circuit") or {}).get("telemetry_available") is False)
        check("执行器在跑", (sres.get("executor") or {}).get("running") is True, str(sres.get("executor")))

        # ---------- 9. 闸门与熔断 ----------
        print("\n[9] breaker.reset")
        r = eng.call("breaker.reset")
        bres = r.get("result") or {}
        check("熔断可重置", bres.get("level") == "closed", str(bres)[:160])

        # ---------- 10. 停止 ----------
        print("\n[10] stop / shutdown")
        r = eng.call("stop")
        check("stop 成功", (r.get("result") or {}).get("stopped") is True)
        r = eng.call("shutdown")
        # `__shutdown__` 是**服务端内部标志**：`serve()` 会把它 pop 掉再回
        # `result: {}`，客户端看不到它。所以这里验的是「回了 result」+
        # 「进程真的退出了」这两件事。
        check("shutdown 返回 result", r.get("type") == "result", str(r)[:160])
        try:
            code = eng.proc.wait(timeout=8)
            check("进程已退出", True, f"exit={code}")
        except Exception:  # noqa: BLE001
            check("进程已退出", False, "8 秒内没退出")
    finally:
        eng.close()
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\n" + "-" * 70)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  失败：{f}")
    print("  [引擎入口自测通过]" if not FAIL else "  [引擎入口自测未通过]")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
