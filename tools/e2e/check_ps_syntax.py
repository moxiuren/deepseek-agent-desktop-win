#!/usr/bin/env python3
"""静态检查：把驱动脚本里的 PowerShell 段抽出来做语法解析，不执行。

## 为什么需要这个

`run_ps` 把整段 PowerShell 塞进 `powershell -NoProfile -Command "<文本>"`。
这意味着：

  · 换行**不构成续行** —— 跨行的 `+` 拼接直接 ParserError
  · ParserError 发生在解析阶段，**整段一行都不执行**
  · 但错误信息是 GBK 乱码，肉眼很难看出是哪一行

结果就是：花了一次真实运行（消耗额度、可能留下半截会话），
只换来一句 `Missing closing ')' in expression.`。

`[System.Management.Automation.Language.Parser]::ParseInput()` 只解析不执行，
可以零成本抓到这类错误。

## 用法

    python tools/e2e/check_ps_syntax.py tools/e2e/journey3_cancel_web.py
    python tools/e2e/check_ps_syntax.py tools/e2e/*.py     # 批量

做法：读源文件，用 `ast` 找出所有名字以 `BODY` 或 `PS` 开头的模块级
字符串常量（这些就是被塞进 PowerShell 的段），逐段交给 Parser 解析。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PARSE_PS = r"""
Add-Type -AssemblyName System.Management.Automation
$src = [System.IO.File]::ReadAllText($env:PS_SRC, [System.Text.Encoding]::UTF8)
$tokens = $null
$errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
if ($errors -and $errors.Count -gt 0) {
    foreach ($e in $errors) {
        'ERR|line=' + $e.Extent.StartLineNumber + '|col=' + $e.Extent.StartColumnNumber + '|' + $e.Message
    }
} else {
    'OK'
}
"""


def extract_ps_blocks(path: Path) -> list[tuple[str, str]]:
    """返回 [(常量名, 文本), ...]。只取模块级 Str 常量，名字以 BODY / PS 开头。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, ast.Name):
            continue
        if not (tgt.id.startswith("BODY") or tgt.id.startswith("PS")):
            continue
        val = node.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            out.append((tgt.id, val.value))
        # ps_body(...) 这类包装调用：取它的字面量参数
        elif isinstance(val, ast.Call) and val.args:
            a = val.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                out.append((tgt.id, a.value))
    return out


def check_block(name: str, text: str) -> list[str]:
    # `__PID__` 是 run_ps 的占位符，运行时会被替换成真实 PID。
    # 静态检查必须先替换，否则会把占位符当成语法错误 ——
    # 这是检查器自己的假阳性，不是被检查代码的问题。
    text = text.replace("__PID__", "12345")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", PARSE_PS],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PS_SRC": _write_tmp(text)},
        timeout=60,
    )
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return [f"{name}: [无输出] {proc.stderr.strip()[:300]}"]
    if lines[0] == "OK":
        return []
    return [f"{name}: {ln}" for ln in lines]


def _write_tmp(text: str) -> str:
    import tempfile
    f = Path(tempfile.gettempdir()) / "_ps_syntax_check.ps1"
    f.write_text(text, encoding="utf-8")
    return str(f)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: check_ps_syntax.py <文件> [文件...]")
        return 1

    files: list[Path] = []
    for a in argv[1:]:
        p = Path(a)
        if p.is_dir():
            files += sorted(p.glob("*.py"))
        elif p.exists():
            files.append(p)

    total_blocks = 0
    total_err = 0
    for f in files:
        try:
            blocks = extract_ps_blocks(f)
        except SyntaxError as e:
            print(f"[跳过] {f.name}: Python 语法错误 {e}")
            continue
        if not blocks:
            continue
        print(f"\n=== {f.name} ===")
        for name, text in blocks:
            total_blocks += 1
            errs = check_block(name, text)
            if errs:
                total_err += len(errs)
                for e in errs:
                    print(f"  [错误] {e}")
            else:
                print(f"  [通过] {name}")

    print(f"\n共检查 {total_blocks} 段，错误 {total_err} 条")
    return 0 if total_err == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
