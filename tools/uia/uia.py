#!/usr/bin/env python3
"""DeepSeek Agent Desktop 的 UIA 驱动包装器。

本机 PowerShell 5.1 的 ExecutionPolicy 模块加载不了（Get-ExecutionPolicy 报
CommandNotFound），所以 `-ExecutionPolicy Bypass -File xxx.ps1` 这条路走不通。
改用 `powershell -Command "<脚本文本>"` 内联执行：执行策略只约束 .ps1 文件的
加载，不约束命令行文本。

参数通过环境变量传给 PowerShell（内联模式下 param 块拿不到命令行参数）。

用法：
    python tools/uia/uia.py dump
    python tools/uia/uia.py text
    python tools/uia/uia.py find 运行
    python tools/uia/uia.py click 运行
    python tools/uia/uia.py set TxtInput "你好"

退出码：0 成功；1 失败（错误信息已写进 stdout）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ui.ps1"


def run(action: str, target: str = "", value: str = "") -> tuple[int, str]:
    src = SCRIPT.read_text(encoding="utf-8-sig")
    out_file = Path(tempfile.gettempdir()) / "uia_out.txt"

    env = dict(os.environ)
    env["UIA_ACTION"] = action
    env["UIA_TARGET"] = target
    env["UIA_VALUE"] = value
    env["UIA_OUT"] = str(out_file)
    # 让 Python 侧能正确处理中文输出
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", src],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )

    if out_file.exists():
        body = out_file.read_text(encoding="utf-8")
    else:
        body = ""

    detail = ""
    if proc.returncode != 0 and not body:
        detail = f"\n[PowerShell stderr]\n{proc.stderr.strip()[:2000]}"
    return proc.returncode, body + detail


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    action = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else ""
    value = sys.argv[3] if len(sys.argv) > 3 else ""

    rc, out = run(action, target, value)
    sys.stdout.write(out)
    if not out.endswith("\n"):
        sys.stdout.write("\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
