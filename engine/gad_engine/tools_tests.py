"""测试入口 —— run_tests。

它解决什么
----------
「跑一下测试」是编程 agent 最高频的动作之一。用 `local_cmd` 手搓要：
先探项目类型（pytest? npm? cargo? go?）、再拼对应命令、再从输出里读通过率。
每次都要做对这三步，不该压在模型身上。

本模块只做**识别 + 拼命令 + 解析结果**三件事，不自己执行 ——
执行交给 agent 已有的 executor（它有常驻进程、超时、输出路由）。
这样 run_tests 复用了全部执行层的能力，不引入第二条执行路径。

识别顺序
--------
按「项目根目录的标记文件」判断，命中即停。同时有多个标记时，
按 pyproject/setup.py → package.json → Cargo.toml → go.mod 的顺序 ——
一个 Python 仓库里混个 package.json（前端工具链）是常态，不能见 json 就当成 JS 项目。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: 识别结果：kind -> 命令模板
_DETECTORS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    # 注意：**pytest 不在这里**。它的判据复杂（要真看见用例或 pytest 配置，
    # 不能光凭 pyproject.toml 存在），由 `_looks_like_pytest()` 单独负责，
    # 在 detect() 里更早地判掉。
    ("npm", ("package.json",), "npm test"),
    ("cargo", ("Cargo.toml",), "cargo test"),
    ("go", ("go.mod",), "go test ./..."),
    ("dotnet", ("*.sln", "*.csproj"), "dotnet test"),
)


def detect(root: Path) -> tuple[str, str]:
    """返回 (项目类型, 建议命令)。识别不出返回 ("unknown", "")。

    优先看「测试目录 / 测试文件」是否存在 —— 光有 pyproject.toml 不代表有测试，
    直接跑 pytest 可能一个用例都收集不到，那不是「测试失败」而是「没有测试」。
    """
    if not root.is_dir():
        return "unknown", ""

    # pytest 的判据不能只看「有没有 tests/ 目录」—— 目录里可能是**自定义脚本**
    # （`raise SystemExit(main())` 形态，没有 `def test_`）。那时 pytest 收集不到
    # 用例，会空转挂住直到超时（实测：本项目就是这样，180 秒超时）。
    # 所以要看：要么有 pytest 配置文件，要么真的存在 `def test_` / `import pytest`。
    if _looks_like_pytest(root):
        return "pytest", "python -m pytest -q"

    for kind, markers, cmd in _DETECTORS:
        for marker in markers:
            if marker.startswith("*"):
                if any(root.glob(marker)):
                    return kind, cmd
            elif (root / marker).exists():
                if kind == "npm":
                    return kind, _npm_test_command(root)
                return kind, cmd

    return "unknown", ""


#: pytest 配置文件 —— 有这些就足以判定。
_PYTEST_CONFIGS = ("pytest.ini", "conftest.py", "tox.ini")

#: 判定「真有 pytest 用例」的迹象。要真的看见函数或导入，不能只凭目录名。
_PYTEST_SIGNS = re.compile(r"^\s*(def\s+test_|import\s+pytest|from\s+pytest\b)", re.MULTILINE)


def _looks_like_pytest(root: Path) -> bool:
    """判断这是不是一个 pytest 项目。

    **不能只看有没有 tests/ 目录** —— 目录里可能是自定义脚本
    （`raise SystemExit(main())` 形态，没有 `def test_`），pytest 收集不到用例
    会空转挂到超时（实测本项目就是如此，180 秒）。

    所以判据是：有 pytest 配置文件，**或**在测试文件里真的看见
    `def test_` / `import pytest`。扫描量有上限（前若干个文件），
    避免在大仓库里遍历过久。
    """
    for cfg in _PYTEST_CONFIGS:
        if (root / cfg).exists():
            return True
    # pyproject.toml 里显式配了 pytest 段也算
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            if "[tool.pytest" in pyproject.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            pass

    candidates: list[Path] = []
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        candidates.extend(sorted(tests_dir.glob("test_*.py"))[:8])
    candidates.extend(sorted(root.glob("test_*.py"))[:8])
    candidates.extend(sorted(root.glob("*_test.py"))[:8])

    for fp in candidates:
        try:
            if fp.stat().st_size > 512 * 1024:
                continue
            text = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _PYTEST_SIGNS.search(text):
            return True
    return False


def _npm_test_command(root: Path) -> str:
    """读 package.json，确认有没有 test 脚本。

    没有 test 脚本时 `npm test` 会报错退出，看起来像「测试失败」——
    实际是「没配测试」。这种情况明确说出来，别让模型去 debug 一个不存在的失败。
    """
    pkg = root / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
        scripts = data.get("scripts") or {}
        if "test" in scripts:
            return "npm test"
        return ""
    except (OSError, ValueError, AttributeError):
        return ""


#: run_tests 认识的键。值可以含空格，所以不能用 tools_fs.split_path_args
#: （那个只认末尾无空格的 kv，`cmd=npm run check` 会整段被当成路径）。
_TEST_ARG_RE = re.compile(r"(?:^|\s)(path|cmd)=")


def parse_test_args(spec: str) -> dict[str, str]:
    """解析 `path=... cmd=...`，值可含空格、键可出现在开头。

    没有已知键时，把整段当成裸路径（`run_tests:C:/proj` 这种写法）。
    """
    s = (spec or "").strip()
    if not s:
        return {}
    marks = list(_TEST_ARG_RE.finditer(s))
    if not marks:
        return {"path": s}
    args: dict[str, str] = {}
    for i, m in enumerate(marks):
        key = m.group(1)
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(s)
        args[key] = s[start:end].strip()
    return args


def run_tests(spec: str, root: Path, run_command) -> tuple[bool, str]:
    """识别项目并跑测试。

    spec 形如 `run_tests [path=... cmd=...]`：
      * `path`  项目根目录（默认当前工作目录）
      * `cmd`   显式指定命令，跳过识别（用户知道自己在干嘛时用）

    `run_command(command) -> CommandResult` 由调用方注入（agent 传 executor.run）。
    这样本函数不碰执行细节，能被单测用假 run_command 覆盖。
    """
    args = parse_test_args(spec or "")
    target = Path(args.get("path", "")).expanduser() if args.get("path") else root
    explicit = args.get("cmd", "").strip()

    if explicit:
        kind = "explicit"
        command = explicit
    else:
        kind, command = detect(target)

    if not command:
        if kind == "npm":
            return False, (
                f"[run_tests 失败] {target} 是 npm 项目，但 package.json 里没有 `test` 脚本。\n"
                "可能测试命令叫别的名字（如 `npm run check`）。请用 cmd= 显式指定。"
            )
        return False, (
            f"[run_tests 失败] 无法识别 {target} 的项目类型。\n"
            "支持的标记：pytest / package.json / Cargo.toml / go.mod / *.sln。\n"
            "也可以用 cmd= 显式指定命令。"
        )

    # 交给注入的 run_command 执行
    result = run_command(command)
    exit_code = getattr(result, "exit_code", 1)
    output = str(getattr(result, "output", "") or "")

    ok = exit_code == 0
    head = f"[run_tests {'通过' if ok else '失败'}] 项目类型 {kind} · 命令 `{command}` · 退出码 {exit_code}"
    return ok, f"{head}\n\n{output if output.strip() else '(无输出)'}"
