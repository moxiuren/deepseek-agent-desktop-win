"""安全护栏 —— 危险命令检测 + 路径逃逸检测。

为什么需要它
------------
`executor` 现在以**当前用户全权限**跑命令，唯一的护栏是扫描纪律。这意味着
模型（或一次提示词注入）输出 `Remove-Item -Recurse -Force C:\\` 就会被执行。
本层是那个缺失的刹车。

设计原则：**分级，不是一刀切**
----------------------------
三档，而不是「禁止 / 允许」两态：

  * `BLOCK`  —— 明确灾难性的（格式化、全盘删除、磁盘分区）。**硬拒绝**，
                连豁免标记都不认。这类命令没有「我知道我在干嘛」的正当场景。
  * `WARN`   —— 有破坏性但可能合法（递归删除、强制推送、注册表写入）。
                默认拒绝，但命令里带 `#danger-ok` 时放行 —— 与 `#scan-ok`
                同一个心智模型，模型已经熟悉这个模式。
  * `OK`     —— 放行。

**为什么不做交互式确认**：引擎跑在子进程里，工具执行是同步的，没有弹窗通道。
真要做确认，得让模型先 `ask_user` 再执行 —— 那是模型的责任，护栏只做
「拦下明显不该跑的」，并在拒绝理由里**建议**它去问用户。
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

#: 豁免标记 —— 与 executor 的 `#scan-ok` 保持一致的心智模型。
DANGER_OK = "#danger-ok"

#: 硬拒绝：明确灾难性，不可豁免。
_BLOCK_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bformat\s+[a-z]:", re.I), "格式化磁盘分区"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I), "创建文件系统（会清空分区）"),
    (re.compile(r"\bdiskpart\b", re.I), "磁盘分区操作"),
    (re.compile(r"Remove-Item\s+.*-Recurse.*\b[A-Z]:\\?\s*$", re.I), "递归删除整个盘符"),
    (re.compile(r"\brm\s+-rf?\s+/\s*$", re.I), "递归删除根目录"),
    (re.compile(r"\bClear-Disk\b", re.I), "清除磁盘"),
    (re.compile(r"\bInitialize-Disk\b", re.I), "初始化磁盘"),
    (re.compile(r":\(\)\s*\{.*\};\s*:", re.I), "fork 炸弹"),
)

#: 需确认：有破坏性但可能合法。
_WARN_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"Remove-Item\b.*-Recurse", re.I), "递归删除"),
    (re.compile(r"\brmdir\s+/s", re.I), "递归删除目录"),
    (re.compile(r"\brd\s+/s", re.I), "递归删除目录"),
    (re.compile(r"\bdel\s+/[sf]", re.I), "强制/递归删除"),
    (re.compile(r"Stop-Process\b.*-Force", re.I), "强制结束进程"),
    (re.compile(r"Stop-Computer|Restart-Computer", re.I), "关机/重启"),
    (re.compile(r"\bgit\s+push\b.*(--force|-f)\b", re.I), "强制推送（会覆盖远端历史）"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.I), "硬重置（丢弃未提交改动）"),
    (re.compile(r"\bgit\s+clean\b.*-[a-z]*f", re.I), "清除未跟踪文件"),
    (re.compile(r"Set-ItemProperty\b.*HKLM", re.I), "写系统注册表"),
    (re.compile(r"New-ItemProperty\b.*HKLM", re.I), "写系统注册表"),
    (re.compile(r"reg\s+delete\b", re.I), "删除注册表项"),
    (re.compile(r"\bSet-ExecutionPolicy\b", re.I), "修改执行策略"),
    (re.compile(r"\b(Invoke-Expression|iex)\b\s*\(", re.I), "动态执行代码（可能被注入）"),
    (re.compile(r"Invoke-WebRequest\b.*\|\s*(iex|Invoke-Expression)", re.I), "下载并执行（远程代码执行）"),
    (re.compile(r"\bcurl\b.*\|\s*(sh|bash|pwsh)", re.I), "下载并执行脚本"),
    (re.compile(r"\b(chmod|chown)\b.*-R\b", re.I), "递归改权限/属主"),
    (re.compile(r"\btakeown\b", re.I), "夺取文件属主"),
    (re.compile(r"\bicacls\b.*/grant", re.I), "修改文件 ACL"),
)

#: 严重程度
BLOCK = "block"
WARN = "warn"
OK = "ok"


def check_command(command: str) -> tuple[str, str]:
    """检查一条命令。返回 (severity, reason)。

    severity ∈ {ok, warn, block}；reason 仅在非 ok 时有意义。
    """
    if not command or not command.strip():
        return OK, ""

    low = command

    for rx, why in _BLOCK_PATTERNS:
        if rx.search(low):
            return BLOCK, why

    # 豁免标记只对 WARN 档有效，对 BLOCK 无效 —— 见模块头。
    if DANGER_OK in command:
        return OK, ""

    for rx, why in _WARN_PATTERNS:
        if rx.search(low):
            return WARN, why

    return OK, ""


def format_rejection(severity: str, reason: str, command: str) -> str:
    """生成给模型看的拒绝文本。要说清「为什么」和「怎么办」。"""
    if severity == BLOCK:
        return (
            f"[已拒绝 · 不可执行] 检测到灾难性命令：{reason}。\n"
            "这类命令没有正当使用场景，豁免标记也不生效。\n"
            "如果你确实需要用户决定，用 `ask_user:` 把情况说明给用户。"
        )
    return (
        f"[已拒绝] 检测到有破坏性的命令：{reason}。\n"
        f"如果这确实是你想要的，在命令里加上 `{DANGER_OK}` 重新发送。\n"
        "加之前请确认：这条命令改的东西是用户明确要改的，且不可逆。"
    )


# --------------------------------------------------------------------------
# 路径逃逸
# --------------------------------------------------------------------------


def check_path_escape(
    target: str,
    work_dir: str,
    allow: tuple[str, ...] = (),
) -> str | None:
    """检查路径是否逃出工作目录。返回拒绝理由，None 表示放行。

    `allow` 是额外的允许根目录（用户可配）。工作目录本身总是允许的。

    **为什么用「前缀 + 路径边界」而不是字符串 startswith**：
    `C:\\work-evil` 的字符串前缀是 `C:\\work`，但它是另一个目录。必须比较
    resolve 后的 Path 是否真的在允许根**之下**。
    """
    if not target or not work_dir:
        return None

    try:
        target_path = Path(target).expanduser().resolve()
    except (OSError, ValueError):
        return None  # 解析不了就交给底层报错，不在这一层拦

    roots: list[Path] = []
    with contextlib.suppress(OSError, ValueError):
        roots.append(Path(work_dir).expanduser().resolve())
    for a in allow:
        try:
            roots.append(Path(a).expanduser().resolve())
        except (OSError, ValueError):
            continue

    if not roots:
        return None  # 没有可判定的根，不拦

    for root in roots:
        try:
            target_path.relative_to(root)
            return None  # 在某个允许根之下
        except ValueError:
            continue

    return (
        f"路径 {target_path} 在工作目录 {roots[0]} 之外。"
        "如果确实需要访问，请让用户在设置里把它加进允许列表。"
    )
