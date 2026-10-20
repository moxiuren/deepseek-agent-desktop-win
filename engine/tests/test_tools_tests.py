"""run_tests 识别逻辑自测。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tools_tests.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import tools_tests as TT

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


class _FakeResult:
    def __init__(self, code, out):
        self.exit_code = code
        self.output = out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gad-tests-"))
    try:
        # ---------- 1. pytest 识别（有 tests 目录）----------
        print("\n[1] pytest 识别")
        py = tmp / "pyproj"
        # 只有空 tests 目录 —— **不该**判为 pytest（这正是修掉的 bug：
        # 目录里可能是自定义脚本，pytest 收集不到用例会空转挂到超时）
        (py / "tests").mkdir(parents=True)
        check("空 tests 目录 -> 不判 pytest", TT.detect(py)[0] == "unknown", str(TT.detect(py)))

        # 真有 pytest 用例 -> pytest
        (py / "tests" / "test_a.py").write_text(
            "import pytest\n\ndef test_x():\n    assert True\n", encoding="utf-8"
        )
        check("有 def test_ -> pytest", TT.detect(py)[0] == "pytest", str(TT.detect(py)))

        # 只有 pyproject（无 pytest 段）-> 不判 pytest
        py2 = tmp / "py2"
        py2.mkdir()
        (py2 / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        check("pyproject 无 pytest 段 -> unknown", TT.detect(py2)[0] == "unknown", str(TT.detect(py2)))

        # pyproject 里显式配了 pytest -> pytest
        py3 = tmp / "py3"
        py3.mkdir()
        (py3 / "pyproject.toml").write_text(
            "[project]\nname='x'\n\n[tool.pytest.ini_options]\n", encoding="utf-8"
        )
        check("pyproject 有 pytest 段 -> pytest", TT.detect(py3)[0] == "pytest", str(TT.detect(py3)))

        # conftest.py 存在 -> pytest
        py4 = tmp / "py4"
        py4.mkdir()
        (py4 / "conftest.py").write_text("", encoding="utf-8")
        check("conftest.py -> pytest", TT.detect(py4)[0] == "pytest", str(TT.detect(py4)))

        # ---------- 2. npm 识别 ----------
        print("\n[2] npm 识别")
        js = tmp / "jsproj"
        js.mkdir()
        (js / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
        check("package.json 有 test -> npm", TT.detect(js)[0] == "npm", str(TT.detect(js)))

        js2 = tmp / "js2"
        js2.mkdir()
        (js2 / "package.json").write_text('{"scripts":{"build":"tsc"}}', encoding="utf-8")
        check("无 test 脚本 -> 命令空", TT.detect(js2)[1] == "", str(TT.detect(js2)))

        # ---------- 3. cargo / go ----------
        print("\n[3] cargo / go")
        rs = tmp / "rs"
        rs.mkdir()
        (rs / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        check("Cargo.toml -> cargo", TT.detect(rs)[0] == "cargo", str(TT.detect(rs)))

        go = tmp / "go"
        go.mkdir()
        (go / "go.mod").write_text("module x\n", encoding="utf-8")
        check("go.mod -> go", TT.detect(go)[0] == "go", str(TT.detect(go)))

        # ---------- 4. 未知 ----------
        print("\n[4] 未知")
        unk = tmp / "unk"
        unk.mkdir()
        check("空目录 -> unknown", TT.detect(unk)[0] == "unknown", str(TT.detect(unk)))

        # ---------- 5. run_tests 注入式执行 ----------
        print("\n[5] run_tests 执行")
        calls = []
        def fake_run(cmd):
            calls.append(cmd)
            return _FakeResult(0, "5 passed")

        ok, out = TT.run_tests("", py, fake_run)
        check("识别 pytest 并执行", ok and "通过" in out, out[:120])
        check("命令正确", calls and "pytest" in calls[0], str(calls))

        # 失败退出码
        def fail_run(cmd):
            return _FakeResult(1, "2 failed")
        ok, out = TT.run_tests("", py, fail_run)
        check("失败时 ok=False", not ok and "失败" in out, out[:120])
        check("退出码入结果", "退出码 1" in out, out[:160])

        # 显式 cmd
        calls2 = []
        def cap_run(cmd):
            calls2.append(cmd)
            return _FakeResult(0, "ok")
        ok, out = TT.run_tests("cmd=npm run check", py, cap_run)
        check("cmd= 跳过识别", calls2 == ["npm run check"], str(calls2))
        check("类型显示 explicit", "explicit" in out, out[:120])

        # npm 无 test 脚本
        ok, out = TT.run_tests("", js2, fake_run)
        check("npm 无 test 明确提示", (not ok) and "没有" in out and "test" in out, out[:160])

        # 未知项目
        ok, out = TT.run_tests("", unk, fake_run)
        check("未知项目报错", (not ok) and "无法识别" in out, out[:160])

        # path= 参数
        calls3 = []
        def cap3(cmd):
            calls3.append(cmd)
            return _FakeResult(0, "ok")
        ok, out = TT.run_tests(f"path={go}", py, cap3)
        check("path= 指定目录", calls3 and "go test" in calls3[0], str(calls3))

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
