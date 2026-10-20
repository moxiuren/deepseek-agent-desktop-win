"""工具权限分级 + dry-run 自测。

守护的两条契约
--------------
1. **默认拒绝比默认放行安全**：`_TOOL_LEVEL` 查不到的工具按 `EXEC` 算，
   而不是静默放行。新增工具忘了登记，会被最严档拦住。
2. **`local_cmd` 是万能壳，必须做命令级判定**：`Get-Content x` 只读，
   `Remove-Item x` 写。只按工具名分级等于「要么全禁要么全开」。

为什么白名单宁缺毋滥
--------------------
误判成只读的代价是「只读模式下执行了写操作」，误判成 EXEC 的代价只是
「只读模式下多拒一条命令」。两者不对称，所以只有**明确无副作用**的
命令才进白名单，任何带重定向、管道写、命令串联的一律按 EXEC。

为什么权限检查放在 `_execute` 分发之前
--------------------------------------
`local_cmd` 与 `local_cmd:async` 各自在 `_run_command` / `_run_async` 里
已经调过一次 `_screen_command`（危险命令检测）。权限检查若也分散到各个
`_run_*` 里，以后新增一个工具就得记得加一处 —— 漏掉一处就是一个绕过口。
收敛到分发入口，结构上不可能漏。本文件第 [7] 段专门守这一点。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_permissions.py
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import permissions
from gad_engine.config import EngineConfig
from gad_engine.permissions import (
    EXEC,
    READ_ONLY,
    WRITE,
    PermissionConfig,
)
from gad_engine.protocol import ToolCall, ToolKind

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" —— {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


# --------------------------------------------------------------------------
# [1] 工具 kind -> 权限档位
# --------------------------------------------------------------------------


def run_tool_level() -> None:
    print("\n[1] 工具 kind -> 权限档位")
    lvl = permissions.tool_level

    check("read_file 只需只读", lvl("read_file") == READ_ONLY, lvl("read_file"))
    check("grep 只需只读", lvl("grep") == READ_ONLY, lvl("grep"))
    check("web_search 只需只读", lvl("web_search") == READ_ONLY, lvl("web_search"))
    check("write_file 需要写", lvl("write_file") == WRITE, lvl("write_file"))
    check("apply_patch 需要写", lvl("apply_patch") == WRITE, lvl("apply_patch"))
    check("git 需要写", lvl("git") == WRITE, lvl("git"))
    check("local_cmd 需要执行", lvl("local_cmd", payload="rm -rf /") == EXEC)
    check("run_code 需要执行", lvl("run_code") == EXEC, lvl("run_code"))
    check("mcp 需要执行", lvl("mcp") == EXEC, lvl("mcp"))
    check("agent-attach 只需只读", lvl("agent-attach") == READ_ONLY,
          lvl("agent-attach"))
    check("agent-screenshot 需要执行", lvl("agent-screenshot") == EXEC,
          lvl("agent-screenshot"))

    # 默认拒绝：查不到的一律 EXEC
    check(
        "未知工具默认按 EXEC（最严）",
        lvl("some_future_tool") == EXEC,
        lvl("some_future_tool"),
    )

    # job 的读/取消分离
    check("job list 只读", lvl("job", target="list") == READ_ONLY, lvl("job", target="list"))
    check("job status 只读", lvl("job", target="status 3") == READ_ONLY)
    check("job cancel 需要执行", lvl("job", target="cancel 3") == EXEC,
          lvl("job", target="cancel 3"))

    # 表完整性：新增 ToolKind 忘了登记时，这里会红。
    # 默认 EXEC 是安全网（默认拒绝），但它是**静默**的 —— 只读模式下
    # 会莫名其妙拒掉一个本该只读的新工具。用测试把它变成显式的。
    declared = {k.value for k in ToolKind}
    special = {"job", "local_cmd", "local_cmd:async"}  # 由 tool_level() 特判
    missing = sorted(declared - set(permissions._TOOL_LEVEL) - special)
    check("所有 ToolKind 都已登记权限档位", not missing, f"未登记：{missing}")


# --------------------------------------------------------------------------
# [2] 只读命令识别
# --------------------------------------------------------------------------


def run_readonly_detection() -> None:
    print("\n[2] 只读命令识别")
    ro = permissions.is_readonly_command

    # 该判只读的
    for cmd in (
        "Get-Content C:\\tmp\\a.txt",
        "get-childitem .",
        "ls -la",
        "cat README.md",
        "git status",
        "git log --oneline -5",
        "python --version",
        "ruff check gad_engine",
        "C:\\Windows\\System32\\where.exe python",
        "  Get-Date  ",
    ):
        check(f"只读：{cmd}", ro(cmd), "被误判为 EXEC")

    # 该判 EXEC 的
    for cmd, why in (
        ("Get-Content a.txt > b.txt", "输出重定向"),
        ("Get-Content a.txt >> b.txt", "追加重定向"),
        ("Get-Content a.txt | Set-Content b.txt", "管道写"),
        ("Get-Content a.txt; Remove-Item b.txt", "命令串联"),
        ("Get-Content a.txt && Remove-Item b.txt", "命令串联"),
        ("git commit -m x", "git 写子命令"),
        ("git push --force", "git 写子命令"),
        ("git add .", "git 写子命令"),
        ("Remove-Item -Recurse C:\\x", "不在白名单"),
        ("python evil.py", "跑脚本等于执行任意代码"),
        ("python -c \"open('a','w').write('x')\"", "python -c 可写盘"),
        ("python", "裸调用进 REPL，交互式不算只读"),
        ("npm install", "会写 node_modules"),
        ("npm uninstall x", "会改 node_modules"),
        ("npm", "裸调用打帮助，不算只读"),
        ("pip install requests", "会写 site-packages"),
        ("ruff format .", "会改文件"),
        ("ruff check --fix .", "自动修复会改文件"),
        ("", "空命令"),
    ):
        check(f"EXEC：{cmd}（{why}）", not ro(cmd), "被误判为只读")

    # 万能壳程序的只读参数要能认出来 —— 别为了保守把所有 npm 都判 EXEC
    for cmd in ("npm ls", "npm list --depth=0", "pip list", "ruff check ."):
        check(f"只读：{cmd}", ro(cmd), "被误判为 EXEC")

    # 2>&1 不该被当成写重定向
    check("2>&1 不算写重定向", ro("python --version 2>&1"), "把 2>&1 误判为写")


# --------------------------------------------------------------------------
# [3] 档位判定（包含关系）
# --------------------------------------------------------------------------


def run_level_check() -> None:
    print("\n[3] 档位判定（EXEC ⊇ WRITE ⊇ READ_ONLY）")

    def allowed(level: str, kind: str, target: str = "", payload: str = "") -> bool:
        cfg = PermissionConfig(level=level)
        return permissions.check(cfg, kind, target, payload) is None

    # read_only：只放只读
    check("read_only 放 read_file", allowed(READ_ONLY, "read_file"))
    check("read_only 拒 write_file", not allowed(READ_ONLY, "write_file"))
    check("read_only 拒 local_cmd", not allowed(READ_ONLY, "local_cmd", payload="rm x"))
    check("read_only 放只读命令",
          allowed(READ_ONLY, "local_cmd", payload="Get-Content a.txt"))
    check("read_only 拒写命令",
          not allowed(READ_ONLY, "local_cmd", payload="Set-Content a.txt x"))

    # write：放只读 + 写，拒执行
    check("write 放 write_file", allowed(WRITE, "write_file"))
    check("write 放 read_file（包含关系）", allowed(WRITE, "read_file"))
    check("write 拒 local_cmd", not allowed(WRITE, "local_cmd", payload="rm x"))
    check("write 拒 run_code", not allowed(WRITE, "run_code"))

    # exec：全放
    check("exec 放 write_file", allowed(EXEC, "write_file"))
    check("exec 放 local_cmd", allowed(EXEC, "local_cmd", payload="rm x"))

    # 拒绝理由必须可读且带下一步
    msg = permissions.check(PermissionConfig(level=READ_ONLY), "write_file")
    check("拒绝理由含 [权限不足]", msg is not None and "[权限不足]" in msg, str(msg))
    check("拒绝理由提示如何放开",
          msg is not None and "调高权限档位" in msg, str(msg))
    check("拒绝理由警告不要绕过",
          msg is not None and "绕过" in msg, str(msg))


# --------------------------------------------------------------------------
# [4] 配置归一化
# --------------------------------------------------------------------------


def run_config_normalize() -> None:
    print("\n[4] 配置归一化")

    check("默认 exec", PermissionConfig().level == EXEC)
    check("默认不开 dry-run", PermissionConfig().dry_run is False)

    check("大写归一", PermissionConfig(level="READ_ONLY").normalized().level == READ_ONLY)
    check("横线归一", PermissionConfig(level="read-only").normalized().level == READ_ONLY)
    check("前后空格归一",
          PermissionConfig(level="  write  ").normalized().level == WRITE)

    # 配错时**收紧**到 read_only，而不是放宽到 exec
    bad = PermissionConfig(level="readonly").normalized()
    check("配错收紧到 read_only", bad.level == READ_ONLY, bad.level)
    empty = PermissionConfig(level="").normalized()
    check("空值收紧到 read_only", empty.level == READ_ONLY, empty.level)


# --------------------------------------------------------------------------
# [5] dry-run 预览
# --------------------------------------------------------------------------


def run_dry_run() -> None:
    print("\n[5] dry-run 预览")

    cfg = PermissionConfig(level=EXEC, dry_run=True)
    ro = PermissionConfig(level=EXEC, dry_run=False)

    check("dry-run 关：写文件不预览",
          not permissions.needs_preview(ro, "write_file", "a.txt", "x"))
    check("dry-run 开：写文件要预览",
          permissions.needs_preview(cfg, "write_file", "a.txt", "x"))
    check("dry-run 开：命令要预览",
          permissions.needs_preview(cfg, "local_cmd", "", "rm x"))
    # 只读工具在 dry-run 下照常执行 —— 否则预览拿不到信息
    check("dry-run 开：read_file 照常执行",
          not permissions.needs_preview(cfg, "read_file", "a.txt", ""))
    check("dry-run 开：只读命令照常执行",
          not permissions.needs_preview(cfg, "local_cmd", "", "Get-Content a.txt"))

    # 预览内容
    p = permissions.preview("write_file", "C:\\tmp\\a.txt", "hello world")
    check("写文件预览标明未执行", "未执行" in p, p[:120])
    check("写文件预览含目标路径", "C:\\tmp\\a.txt" in p, p[:120])
    check("写文件预览含正文字符数", "11 字符" in p, p[:200])

    p = permissions.preview("local_cmd", "", "Remove-Item -Recurse C:\\x")
    check("命令预览含安全检查结论",
          "安全检查" in p and ("硬拒绝" in p or "需 #danger-ok" in p), p[:200])

    p = permissions.preview("local_cmd:async", "", "Start-Sleep 30")
    check("异步预览提示后台泳道", "后台异步" in p, p[:200])

    # 超长正文要截断，不能把工具结果撑爆
    p = permissions.preview("write_file", "a.txt", "x" * 5000)
    check("超长正文截断", len(p) < 1200, f"预览长度 {len(p)}")


# --------------------------------------------------------------------------
# [6] EngineConfig 往返
# --------------------------------------------------------------------------


def run_config_roundtrip() -> None:
    print("\n[6] EngineConfig 往返")

    cfg = EngineConfig()
    check("默认 permissions 是 exec",
          cfg.permissions.level == EXEC, cfg.permissions.level)
    check("默认 dry_run 关闭", cfg.permissions.dry_run is False)

    data = asdict(cfg)
    check("asdict 含 permissions 段", "permissions" in data)
    check("asdict 里 level 正确", data["permissions"]["level"] == EXEC)

    data["permissions"] = {"level": "read_only", "dry_run": True}
    back = EngineConfig.from_dict(data)
    check("往返后 level 保留", back.permissions.level == READ_ONLY,
          back.permissions.level)
    check("往返后 dry_run 保留", back.permissions.dry_run is True)

    # 老配置文件没有 permissions 段 —— 必须能加载，且默认 exec（不打断现有用户）
    legacy = {k: v for k, v in data.items() if k != "permissions"}
    old = EngineConfig.from_dict(legacy)
    check("老配置无 permissions 段仍可加载", old.permissions.level == EXEC,
          old.permissions.level)


# --------------------------------------------------------------------------
# [7] 接线：权限闸门在 _execute 分发之前
# --------------------------------------------------------------------------


class _FakeExecutor:
    """只提供 snapshot()。`_screen_permission` 只需要这一个方法。"""

    def __init__(self, work_dir: str) -> None:
        self._wd = work_dir

    def snapshot(self) -> dict:
        return {"work_dir": self._wd}


def _make_runtime(level: str, dry_run: bool):
    """不跑 `__init__`，只塞 `_screen_permission` 用得到的两个属性。

    刻意不构造真 executor：这一段测的是**闸门本身**，与执行器无关。
    """
    from gad_engine.agent import AgentRuntime

    rt = AgentRuntime.__new__(AgentRuntime)
    rt.cfg = EngineConfig(permissions=PermissionConfig(level=level, dry_run=dry_run))
    rt.executor = _FakeExecutor(str(Path.home()))  # type: ignore[assignment]
    return rt


def run_wiring() -> None:
    print("\n[7] 接线：权限闸门在 _execute 分发之前")

    # read_only 下写类工具被拦
    rt = _make_runtime(READ_ONLY, False)
    call = ToolCall(kind=ToolKind.WRITE_FILE, target="C:\\tmp\\a.txt", payload="hello")
    ex = rt._screen_permission(call)
    check("read_only 拦 write_file", ex is not None, "没拦住")
    if ex is not None:
        check("拦下时标记 rejected", ex.rejected is True)
        check("拦下时 exit_code 非 0", ex.exit_code != 0, str(ex.exit_code))
        check("拦下时输出含 [权限不足]", "[权限不足]" in ex.output, ex.output[:120])

    # read_only 下只读工具放行
    call = ToolCall(kind=ToolKind.READ_FILE, target="C:\\tmp\\a.txt", payload="")
    check("read_only 放 read_file", rt._screen_permission(call) is None)

    # read_only 下只读命令放行
    call = ToolCall(kind=ToolKind.LOCAL_CMD, payload="Get-Content a.txt")
    check("read_only 放只读命令", rt._screen_permission(call) is None)

    # read_only 下写命令被拦
    call = ToolCall(kind=ToolKind.LOCAL_CMD, payload="Remove-Item a.txt")
    ex = rt._screen_permission(call)
    check("read_only 拦写命令", ex is not None and ex.rejected, "没拦住")

    # dry-run 下写文件变预览
    rt = _make_runtime(EXEC, True)
    call = ToolCall(kind=ToolKind.WRITE_FILE, target="C:\\tmp\\a.txt", payload="hello")
    ex = rt._screen_permission(call)
    check("dry-run 拦下 write_file", ex is not None, "没拦住")
    if ex is not None:
        check("dry-run 不算拒绝（exit_code=0）", ex.exit_code == 0, str(ex.exit_code))
        check("dry-run 不标记 rejected", ex.rejected is False, str(ex.rejected))
        check("dry-run 输出是预览", "dry-run 预演" in ex.output, ex.output[:120])

    # dry-run 下只读工具照常执行（返回 None = 放行）
    call = ToolCall(kind=ToolKind.READ_FILE, target="C:\\tmp\\a.txt", payload="")
    check("dry-run 放 read_file", rt._screen_permission(call) is None)
    call = ToolCall(kind=ToolKind.LOCAL_CMD, payload="Get-Content a.txt")
    check("dry-run 放只读命令", rt._screen_permission(call) is None)

    # exec + 非 dry-run：全部放行（与升级前行为一致）
    rt = _make_runtime(EXEC, False)
    for kind, target, payload in (
        (ToolKind.WRITE_FILE, "a.txt", "x"),
        (ToolKind.LOCAL_CMD, "", "Remove-Item a.txt"),
        (ToolKind.RUN_CODE, "python", "print(1)"),
        (ToolKind.MCP, "some_tool", ""),
    ):
        call = ToolCall(kind=kind, target=target, payload=payload)
        check(f"exec 放 {kind.value}", rt._screen_permission(call) is None)


# --------------------------------------------------------------------------
# [8] 运行时接口：set_permissions 立刻生效，且非法值被收紧并如实回报
# --------------------------------------------------------------------------


def run_setter() -> None:
    print("\n[8] set_permissions 运行时接口")

    import asyncio
    import tempfile

    from gad_engine.agent import AgentRuntime

    # ⚠️ 必须用 EngineConfig.sandbox()：`set_permissions` 内部会 `cfg.save()`，
    # 裸 EngineConfig() 没有 source_path，会写到用户真实的 config.json。
    # 这个坑 2026-10-16 在 test_thinking.py 上真踩过（见 config.py 的注释）。
    with tempfile.TemporaryDirectory() as tmp:
        rt = AgentRuntime.__new__(AgentRuntime)
        rt.cfg = EngineConfig.sandbox(tmp)
        rt.executor = _FakeExecutor(str(Path.home()))  # type: ignore[assignment]

        snap = rt.permissions_snapshot()
        check("默认档位 exec", snap["level"] == EXEC, snap["level"])
        check("默认 dry_run 关", snap["dry_run"] is False)
        check("档位清单含三档", len(snap["levels"]) == 3, str(snap["levels"]))
        check("档位清单含中文标签",
              all(item.get("label") for item in snap["levels"]), str(snap["levels"]))

        out = asyncio.run(rt.set_permissions(level="read_only"))
        check("切到 read_only 回执正确", out["level"] == READ_ONLY, str(out))
        check("无警告", "warning" not in out, str(out))

        # 改完**立刻**生效 —— 不需要重启，因为 _execute 每次实时读配置
        call = ToolCall(kind=ToolKind.WRITE_FILE, target="a.txt", payload="x")
        check("切换后立刻拦下 write_file", rt._screen_permission(call) is not None)

        out = asyncio.run(rt.set_permissions(level="exec", dry_run=True))
        check("切回 exec", out["level"] == EXEC, str(out))
        check("dry_run 打开", out["dry_run"] is True)
        ex = rt._screen_permission(call)
        check("dry_run 下 write_file 变预览",
              ex is not None and "dry-run 预演" in ex.output,
              ex.output[:80] if ex else "None")

        # 非法档位：收紧 + 如实回报，不静默当成成功
        out = asyncio.run(rt.set_permissions(level="readonly"))
        check("非法档位收紧为 read_only", out["level"] == READ_ONLY, str(out))
        check("非法档位给出警告", "warning" in out, str(out))
        check("警告里说明原始值", "readonly" in out.get("warning", ""), str(out))

        # 落盘往返：存进去的值能被读回来
        saved = EngineConfig.load(rt.cfg.source_path)
        check("档位已落盘", saved.permissions.level == READ_ONLY,
              saved.permissions.level)
        check("dry_run 已落盘", saved.permissions.dry_run is True)


# --------------------------------------------------------------------------
# [9] dry-run 预览要报「会碰到哪些文件」
# --------------------------------------------------------------------------


def run_preview_targets() -> None:
    print("\n[9] dry-run 预览的写目标解析")

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        existing = base / "exists.txt"
        existing.write_text("0123456789", encoding="utf-8")

        rt = _make_runtime(EXEC, True)
        rt.executor = _FakeExecutor(str(base))  # type: ignore[assignment]

        # write_file：直接路径
        call = ToolCall(
            kind=ToolKind.WRITE_FILE, target=str(existing), payload="new"
        )
        ex = rt._screen_permission(call)
        check("write_file 预览含目标路径",
              ex is not None and "exists.txt" in ex.output,
              ex.output[:200] if ex else "None")
        check("write_file 预览报告会覆盖",
              ex is not None and "会覆盖" in ex.output,
              ex.output[:200] if ex else "None")
        check("write_file 预览带字节数",
              ex is not None and "10 字节" in ex.output,
              ex.output[:200] if ex else "None")

        newf = base / "brand-new.txt"
        call = ToolCall(kind=ToolKind.WRITE_FILE, target=str(newf), payload="x")
        ex = rt._screen_permission(call)
        check("write_file 预览报告会新建",
              ex is not None and "会新建" in ex.output,
              ex.output[:200] if ex else "None")

        # replace_in_file：target 是 `路径 count=1`，必须只取路径部分
        call = ToolCall(
            kind=ToolKind.REPLACE_IN_FILE,
            target=f"{existing} count=1",
            payload="<<<OLD\n0\nOLD\n<<<NEW\n9\nNEW\n",
        )
        ex = rt._screen_permission(call)
        check("replace_in_file 预览剥离 count 参数",
              ex is not None and "count=" not in ex.output.split("目标：")[-1][:20],
              ex.output[:200] if ex else "None")
        check("replace_in_file 预览认到真实文件",
              ex is not None and "会覆盖" in ex.output,
              ex.output[:200] if ex else "None")

        # apply_patch：真实路径在 diff 里，不在 target
        diff = (
            "--- a/one.txt\n"
            "+++ b/one.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "--- a/two.txt\n"
            "+++ b/two.txt\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        call = ToolCall(kind=ToolKind.APPLY_PATCH, target="", payload=diff)
        ex = rt._screen_permission(call)
        check("apply_patch 预览报文件数",
              ex is not None and "2 个文件" in ex.output,
              ex.output[:300] if ex else "None")
        check("apply_patch 预览列出 diff 里的路径",
              ex is not None and "one.txt" in ex.output and "two.txt" in ex.output,
              ex.output[:300] if ex else "None")

        # 坏 diff 要说清楚「真跑会报错」，而不是假装能预览
        call = ToolCall(kind=ToolKind.APPLY_PATCH, target="", payload="not a diff")
        ex = rt._screen_permission(call)
        check("坏 diff 预览如实报告解析失败",
              ex is not None and "解析失败" in ex.output,
              ex.output[:200] if ex else "None")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    run_tool_level()
    run_readonly_detection()
    run_level_check()
    run_config_normalize()
    run_dry_run()
    run_config_roundtrip()
    run_wiring()
    run_setter()
    run_preview_targets()

    print(f"\n{'=' * 46}")
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("[通过] 全部检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
