"""工具权限分级 + 命令 dry-run。

为什么需要它
------------
`safety.py` 回答的是「这条命令**本身**危不危险」—— 它看的是命令文本
（`Remove-Item -Recurse` / `format c:`）。但它回答不了另一个问题：

> **模型现在该不该有这个能力？**

两个问题正交。一条 `Write-Output 'hi'` 一点都不危险，但如果用户把 agent
设成「只读模式」，它就不该跑。反之 `git push --force` 在 `#danger-ok`
下可以跑，但用户可能只想让它「看看会发生什么」—— 那就是 dry-run。

本模块补的就是这两个维度。

三档权限
--------
按**副作用**分，不按工具名分：

  * `READ_ONLY` —— 只读。`read_file` / `grep` / `list_dir` / `web_search`……
    以及 `local_cmd` 里被判定为只读的命令。
  * `WRITE`     —— 改本机文件。`write_file` / `replace_in_file` / `apply_patch` /
    `git` / `todo:write`。
  * `EXEC`      —— 跑命令、跑代码、访问外部。`local_cmd` / `run_code` /
    `local_cmd:async` / `mcp`。

三档是**包含关系**：`EXEC ⊇ WRITE ⊇ READ_ONLY`。用户设 `write` 就同时允许读。
这比「逐工具勾选」更少心智负担，也更难配错。

为什么 `local_cmd` 单独要「命令级判定」
--------------------------------------
`local_cmd` 是一个**万能壳** —— 同一个工具既能 `Get-Content x.txt`（只读），
也能 `Remove-Item x`（写）。只按工具名分级，等于「要么全禁要么全开」。

所以对 `local_cmd` 多做一次**命令级**判定：命中只读命令白名单 → 按 READ_ONLY
算，否则按 EXEC 算。白名单是**显式枚举**而不是「没命中危险模式就当只读」——
后者会把 `python evil.py` 当成只读。

**为什么白名单宁缺毋滥**：误判成只读的代价是「在只读模式下执行了写操作」，
误判成 EXEC 的代价只是「只读模式下多拒一条命令」。两者不对称，所以只有
明确无副作用的命令才进白名单。

dry-run
-------
`dry_run` 打开时，**写类和执行类**工具不执行，只回一份「会发生什么」的预览：

  * 文件写入 → 目标路径、新内容长度、是否覆盖已有文件
  * 命令     → 命令原文、工作目录、会被 safety 判成什么档
  * git      → 子命令与参数

只读工具在 dry-run 下**照常执行** —— 否则预览本身就失去了信息来源。

与 `safety.py` 的分工
---------------------
`safety` 是**不可豁免的硬底线**（灾难性命令），`permissions` 是**用户可调的
能力范围**。顺序上先过 safety（在 `_run_command` 里），再过 permissions
（在 `_execute` 里）—— 因为 safety 的 BLOCK 连 `#danger-ok` 都不认，
不该被权限设置绕过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# 权限档位
# --------------------------------------------------------------------------

READ_ONLY = "read_only"
WRITE = "write"
EXEC = "exec"

#: 档位从低到高。用于「用户设了 write，read_only 的工具也放行」这种包含关系。
_ORDER = {READ_ONLY: 0, WRITE: 1, EXEC: 2}

#: 每个工具 kind 需要的**最低**权限。
#:
#: 默认值取 `EXEC`（最严）—— 新增工具若忘了在这里登记，会被最严档拦住，
#: 而不是静默放行。这是刻意的：**默认拒绝比默认放行安全**。
_TOOL_LEVEL: dict[str, str] = {
    # 只读
    "read_file": READ_ONLY,
    "list_dir": READ_ONLY,
    "grep": READ_ONLY,
    "glob": READ_ONLY,
    "web_fetch": READ_ONLY,
    "web_search": READ_ONLY,
    "todo:read": READ_ONLY,
    "job": READ_ONLY,          # job:list / status 只读；cancel 见 tool_level()
    "ask_user": READ_ONLY,
    # attach 只是把**已有**文件的路径记进下一轮附件，自己不写盘。
    # 它与 read_file 同级：都是「把本地内容交给上游」，没有副作用。
    "agent-attach": READ_ONLY,
    # 写
    "write_file": WRITE,
    "replace_in_file": WRITE,
    "multi_edit": WRITE,
    "apply_patch": WRITE,
    "todo:write": WRITE,
    "git": WRITE,
    # 执行
    "local_cmd": EXEC,
    "local_cmd:async": EXEC,
    "run_code": EXEC,
    "run_tests": EXEC,
    "mcp": EXEC,
    "agent-screenshot": EXEC,
}

#: 只读命令白名单 —— 首词命中即算只读。
#:
#: **只收「明确不写盘、不联网、不改状态」的命令**。任何带 `-Force` / 重定向 /
#: 管道的都**不**在这里 —— 交给上层按 EXEC 处理。宁可多拒，不可误放。
_READONLY_COMMANDS: frozenset[str] = frozenset(
    {
        # PowerShell 读取类
        "get-content", "get-childitem", "get-item", "get-location",
        "get-process", "get-service", "get-date", "get-command",
        "get-help", "get-member", "get-variable", "get-host",
        "test-path", "select-object", "where-object", "sort-object",
        "measure-object", "format-table", "format-list", "out-string",
        "write-output", "write-host", "echo",
        # 跨平台
        "cat", "type", "ls", "dir", "pwd", "whoami", "hostname",
        "head", "tail", "wc", "which", "where", "file", "stat",
        "uname", "id", "date",
        # git 单独处理（见 _GIT_WRITE_SUBCOMMANDS）
        "git",
    }
)

#: 「万能壳」程序 —— 首词命中**不代表**只读，必须再看第一个参数。
#:
#: 为什么单列出来：`npm install` 和 `npm ls` 的首词一模一样，`python x.py`
#: 和 `python --version` 也是。只按首词判，`npm install` 会被当成只读，
#: 而它会把整个 node_modules 写进磁盘 —— 只读模式下静默写盘，是最坏的
#: 那种失败（用户以为没动过东西）。
#:
#: 值是**允许的只读参数**（第一个 token，小写）。不在表里的一律按 EXEC。
#: 裸调用（`python` 进 REPL、`npm` 打帮助）也按 EXEC —— 交互式不算只读。
_SHELL_READONLY_ARGS: dict[str, frozenset[str]] = {
    # 解释器：只有版本/帮助查询无副作用。`python -c` 能写盘，不算。
    "python": frozenset({"--version", "-v", "--help", "-h"}),
    "python3": frozenset({"--version", "-v", "--help", "-h"}),
    "node": frozenset({"--version", "-v", "--help", "-h"}),
    # 包管理器：install / uninstall / update / publish 全在写盘。
    "npm": frozenset({"--version", "-v", "list", "ls", "view", "outdated"}),
    "pip": frozenset({"--version", "-v", "list", "show", "freeze", "check"}),
    # ruff：`check` 只读（`--fix` 由 _WRITE_INDICATORS 拦），`format` 会改文件。
    "ruff": frozenset({"check", "--version", "-v", "--help", "-h"}),
}

#: 会写盘的子命令 —— 即使首词在只读白名单里，这些也**必须**按 EXEC 处理。
#: 例：`git status` 只读，`git commit` 写。
_GIT_WRITE_SUBCOMMANDS = frozenset(
    {
        "add", "commit", "push", "pull", "fetch", "merge", "rebase",
        "reset", "checkout", "switch", "restore", "clean", "rm", "mv",
        "init", "clone", "tag", "stash", "cherry-pick", "revert", "apply",
    }
)

#: 命令里出现这些就**不**算只读，不论首词是什么。
#: 重定向和管道能把只读命令变成写命令（`Get-Content a | Set-Content b`）。
_WRITE_INDICATORS = re.compile(
    r"(?:^|[^<>])(?:>|>>)(?![>&])"     # 输出重定向（排除 2>&1 这种）
    r"|\|\s*(?:set-content|add-content|out-file|tee-object|remove-item|del|rm)\b"
    r"|--output\b|--out-file\b|-OutFile\b"
    r"|--fix\b|-w\b|--write\b",        # 工具的「自动修复」类开关会改文件
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PermissionConfig:
    """权限设置。默认 `exec` —— 与升级前行为一致，不打断现有用户。"""

    #: 用户允许的最高档位：read_only / write / exec。
    level: str = EXEC
    #: 打开后写类与执行类工具只预览、不执行。
    dry_run: bool = False

    def normalized(self) -> PermissionConfig:
        # ⚠️ 用 `is None` 而不是 `or EXEC`：空字符串是**配错**，不是「没配」。
        # 「没配」由 dataclass 默认值处理（`from_dict` 里 `_filter` 会丢掉
        # 缺失的键，于是这里是默认的 `EXEC`）。显式写了 `level: ""` 属于
        # 配置错误 —— 收紧到 read_only，别因为一个假值就放宽到 exec。
        lv = (self.level if self.level is not None else EXEC)
        lv = lv.strip().lower().replace("-", "_")
        if lv not in _ORDER:
            # 配错时**收紧**到 read_only 而不是放宽到 exec。
            lv = READ_ONLY
        return PermissionConfig(level=lv, dry_run=bool(self.dry_run))


def tool_level(kind: str, target: str = "", payload: str = "") -> str:
    """这个工具调用需要哪一档权限。

    对 `local_cmd` 做命令级细化：只读命令按 READ_ONLY 算。
    其余工具按 `_TOOL_LEVEL` 查表，查不到按 EXEC（最严）。
    """
    k = (kind or "").strip()
    if k == "job":
        # `job:cancel` 会 restart 执行器（丢会话变量），不是只读操作。
        sub = (target or "").strip().split(None, 1)[0].lower() if target else ""
        return EXEC if sub in ("cancel",) else READ_ONLY
    if k in ("local_cmd", "local_cmd:async"):
        return READ_ONLY if is_readonly_command(payload) else EXEC
    return _TOOL_LEVEL.get(k, EXEC)


def is_readonly_command(command: str) -> bool:
    """这条 shell 命令是否**确定**无副作用。

    判据全部满足才算只读，任一不满足按 EXEC：
      1. 非空
      2. 首词在 `_READONLY_COMMANDS` 里
      3. 没有写重定向 / 写管道
      4. 若首词是 `git`，子命令不在 `_GIT_WRITE_SUBCOMMANDS` 里
      5. 没有命令串联（`;` `&&` `||` 之后可能藏着写命令）

    第 5 条是刻意的保守：`Get-Content a; Remove-Item b` 首词是只读的，
    但整条命令不是。要精确解析 PowerShell 语法超出本层职责，直接判 EXEC。
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    # `#scan-ok` / `#danger-ok` 这类注释标记不影响判定，但 `#` 之后的内容
    # 可能是注释也可能是字符串 —— 保守起见不做剥离。

    # 命令串联：`;` 在 PowerShell 里是语句分隔符。
    # 注意不要误伤路径里的分号（Windows 路径不含分号，Unix 也不常见）。
    if re.search(r";|&&|\|\|", cmd):
        return False

    if _WRITE_INDICATORS.search(cmd):
        return False

    first = cmd.split(None, 1)[0].lower()
    # 去掉可能的路径前缀（`C:\Windows\System32\where.exe`）
    first = first.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    # 去掉 `.exe` / `.cmd` / `.ps1` 后缀
    first = re.sub(r"\.(exe|cmd|bat|ps1)$", "", first)

    # 「万能壳」程序：首词命中还不够，必须再看第一个参数。
    # 裸调用（`python` 进 REPL）也按 EXEC —— 交互式会话不算只读。
    if first in _SHELL_READONLY_ARGS:
        parts = cmd.split(None, 1)
        if len(parts) < 2:
            return False
        arg = parts[1].split(None, 1)[0].lower()
        # `--version` / `-v` 之类大小写敏感度低，统一小写比。
        return arg in _SHELL_READONLY_ARGS[first]

    if first not in _READONLY_COMMANDS:
        return False

    if first == "git":
        parts = cmd.split(None, 2)
        if len(parts) < 2:
            return True
        sub = parts[1].lower().lstrip("-")
        if sub in _GIT_WRITE_SUBCOMMANDS:
            return False

    return True


def check(
    cfg: PermissionConfig, kind: str, target: str = "", payload: str = ""
) -> str | None:
    """权限检查。返回拒绝理由，None 表示放行。

    只做「用户允许的档位够不够」这一件事。危险命令由 `safety` 管，
    本函数不重复。
    """
    c = cfg.normalized()
    need = tool_level(kind, target, payload)
    if _ORDER[need] <= _ORDER[c.level]:
        return None
    return (
        f"[权限不足] `{kind}` 需要 `{need}` 权限，当前设置是 `{c.level}`。\n"
        "如需放开，请让用户在设置里调高权限档位（read_only → write → exec）。\n"
        "不要尝试用别的工具绕过这个限制。"
    )


# --------------------------------------------------------------------------
# dry-run 预览
# --------------------------------------------------------------------------

#: 预览里正文最多展示多少字符。太长会把工具结果撑爆。
_PREVIEW_CHARS = 400


def _clip(text: str, n: int = _PREVIEW_CHARS) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n…（共 {len(text)} 字符，此处截断）"


def preview(kind: str, target: str, payload: str, extra: str = "") -> str:
    """生成 dry-run 预览文本。

    `extra` 供调用方补充上下文（例如工作目录、文件是否已存在）。
    """
    k = (kind or "").strip()
    head = "[dry-run 预演] 以下操作**未执行**，仅为预览。"

    if k in ("local_cmd", "local_cmd:async"):
        from . import safety  # 局部导入：避免 safety 与 permissions 循环依赖

        sev, why = safety.check_command(payload)
        sev_label = {"ok": "放行", "warn": "需 #danger-ok", "block": "硬拒绝"}.get(
            sev, sev
        )
        lines = [
            head,
            "",
            f"命令：{_clip(payload)}",
            f"安全检查：{sev_label}" + (f" —— {why}" if why else ""),
        ]
        if extra:
            lines.append(extra)
        if k == "local_cmd:async":
            lines.append("泳道：后台异步（真跑时会返回 job id）")
        lines.append("")
        lines.append("确认无误后，让用户关闭 dry-run 或显式要求执行。")
        return "\n".join(lines)

    if k in ("write_file", "replace_in_file", "multi_edit", "apply_patch"):
        lines = [
            head,
            "",
            f"工具：{k}",
            f"目标：{target}",
        ]
        if payload:
            lines.append(f"正文（{len(payload)} 字符）：")
            lines.append(_clip(payload))
        if extra:
            lines.append(extra)
        lines.append("")
        lines.append("确认无误后，让用户关闭 dry-run 或显式要求执行。")
        return "\n".join(lines)

    if k == "git":
        lines = [head, "", f"git 参数：{target or '（空）'}"]
        if extra:
            lines.append(extra)
        lines.append("")
        lines.append("确认无误后，让用户关闭 dry-run 或显式要求执行。")
        return "\n".join(lines)

    # 其它写/执行类工具：给一份通用预览
    lines = [head, "", f"工具：{k}", f"目标：{target}"]
    if payload:
        lines.append(f"载荷（{len(payload)} 字符）：")
        lines.append(_clip(payload))
    if extra:
        lines.append(extra)
    lines.append("")
    lines.append("确认无误后，让用户关闭 dry-run 或显式要求执行。")
    return "\n".join(lines)


def needs_preview(cfg: PermissionConfig, kind: str, target: str, payload: str) -> bool:
    """dry-run 下这个调用是否该被拦成预览。

    只读工具照常执行 —— 否则预览拿不到真实信息（比如「这个文件存不存在」）。
    """
    c = cfg.normalized()
    if not c.dry_run:
        return False
    return tool_level(kind, target, payload) != READ_ONLY
