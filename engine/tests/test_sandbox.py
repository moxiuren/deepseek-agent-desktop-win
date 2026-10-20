"""沙箱执行环境（`sandbox.py`）自测。

用真实子进程跑，验证三层防护：进程隔离 / 资源限制 / 路径白名单。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_sandbox.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import sandbox as SB

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-sandbox-test-"))
    work = tmp / "work"
    work.mkdir()
    try:
        # ---------- 1. 基本执行 ----------
        print("\n[1] 基本执行")
        r = SB.run_python("print('hello from sandbox')", work_dir=work)
        check("成功", r.ok, r.output[:120])
        check("输出正确", "hello from sandbox" in r.output, r.output[:200])

        # 能读工作目录外的文件（读放宽）
        outside = tmp / "readable.txt"
        outside.write_text("secret read ok", encoding="utf-8")
        r = SB.run_python(
            f"print(open(r'{outside}', encoding='utf-8').read())", work_dir=work
        )
        check("读范围外文件放行", "secret read ok" in r.output, r.output[:200])

        # ---------- 2. 写工作目录内 ----------
        print("\n[2] 写工作目录内")
        target = work / "out.txt"
        r = SB.run_python(
            f"open(r'{target}', 'w', encoding='utf-8').write('written')", work_dir=work
        )
        check("工作目录内写成功", r.ok, r.output[:160])
        check("文件已写", target.exists() and target.read_text(encoding="utf-8") == "written",
              repr(target.read_text(encoding="utf-8") if target.exists() else "不存在"))

        # ---------- 3. 写工作目录外被拒 ----------
        print("\n[3] 写范围外被拒")
        evil = tmp / "evil.txt"
        r = SB.run_python(
            f"open(r'{evil}', 'w', encoding='utf-8').write('pwned')", work_dir=work
        )
        check("范围外写失败", not r.ok, r.output[:200])
        check("含 PermissionError", "PermissionError" in r.output or "沙箱拒绝" in r.output,
              r.output[:200])
        check("evil.txt 未创建", not evil.exists(), str(evil))

        # ---------- 4. 删除范围外被拒 ----------
        print("\n[4] 删除范围外被拒")
        victim = tmp / "victim.txt"
        victim.write_text("x", encoding="utf-8")
        r = SB.run_python(
            f"import os; os.remove(r'{victim}')", work_dir=work
        )
        check("范围外删除失败", not r.ok, r.output[:200])
        check("victim 仍在", victim.exists())

        # ---------- 5. 起子进程被拒 ----------
        print("\n[5] 起子进程被拒")
        r = SB.run_python(
            "import subprocess; subprocess.run(['echo', 'x'])", work_dir=work
        )
        check("起子进程失败", not r.ok, r.output[:200])
        check("含 PermissionError", "PermissionError" in r.output or "沙箱" in r.output,
              r.output[:200])

        # ---------- 6. 超时 ----------
        print("\n[6] 超时")
        r = SB.run_python("import time; time.sleep(30)", work_dir=work, timeout=2.0)
        check("超时被终止", r.timed_out, r.output[:120])
        check("退出码 124", r.exit_code == 124, str(r.exit_code))
        check("耗时约 2 秒", 1500 < r.duration_ms < 8000, f"{r.duration_ms:.0f}ms")

        # ---------- 7. 异常传播 ----------
        print("\n[7] 异常传播")
        r = SB.run_python("raise ValueError('boom')", work_dir=work)
        check("非零退出码", not r.ok, r.output[:120])
        check("含异常信息", "ValueError" in r.output and "boom" in r.output, r.output[:200])

        # ---------- 8. 输出截断 ----------
        print("\n[8] 输出截断")
        r = SB.run_python("print('x' * 50000)", work_dir=work)
        check("输出被截断", len(r.output) < 30000, str(len(r.output)))
        check("含截断标记", "截断" in r.output, r.output[-80:])

        # ---------- 9. allow_paths 额外允许 ----------
        print("\n[9] allow_paths")
        extra = tmp / "extra"
        extra.mkdir()
        efile = extra / "ok.txt"
        r = SB.run_python(
            f"open(r'{efile}', 'w', encoding='utf-8').write('ok')",
            work_dir=work, allow_paths=[extra],
        )
        check("额外允许路径可写", r.ok and efile.exists(), r.output[:160])

        # ---------- 10. 中文编码 ----------
        # 子进程用 -I 启动，-I 隐含 -E（忽略 PYTHON* 环境变量），所以父进程设的
        # PYTHONIOENCODING 不生效 —— 实测子进程按系统 GBK 输出，中文全乱码。
        # wrapper 里显式 reconfigure stdout 修复。这条测试锁住它。
        print("\n[10] 中文编码")
        r = SB.run_python('print("中文测试 OK")', work_dir=work)
        check("中文输出不乱码", "中文测试" in r.output, repr(r.output))

        # ---------- 11. format_result ----------
        print("\n[11] 格式化")
        ok_r = SB.SandboxResult(exit_code=0, output="hi", duration_ms=10)
        check("成功格式含成功", "成功" in SB.format_result(ok_r, str(work)))
        to_r = SB.SandboxResult(exit_code=124, output="", timed_out=True, duration_ms=2000)
        check("超时格式含超时", "超时" in SB.format_result(to_r, str(work)))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "-" * 70)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("  失败项：")
        for name in FAIL:
            print(f"    - {name}")
    print("-" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
