"""孤儿 profile 目录清理（`cdp.cleanup_stale_profiles`）自测。

## 这个测试在防什么

`cleanup_stale_profiles` 删的是**磁盘上的真实目录**。它最危险的失效方式
不是「没删干净」，而是**误删正在使用的 profile** —— 那会让正在跑的 Chrome
当场崩溃，而且是间歇性的、极难复现的那种崩。

所以这里的断言重心在**否定面**：
  * 形状不匹配的目录（`gad-chrome-engine-xxxx` 是引擎的 LOCALAPPDATA）不能被删
  * 命令行里还带着的 profile 不能被删
  * 拿不到进程表时一个都不能删
  * 保护期内的目录不能被删

「该删的确实删了」只占一条。

## 为什么用真目录而不是 mock

被删的东西就是目录，`shutil.rmtree` 的行为（权限、被占用、软链接）没法
用 mock 有意义地替代。测试在系统临时目录里造几个一次性目录，跑完自己清。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_profile_cleanup.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine.transport import cdp

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [通过] {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))
        print(f"  [失败] {name}" + (f" —— {detail}" if detail else ""))


def fresh_sandbox() -> Path:
    """每步一个独立 TEMP 根。

    **不能所有步骤共用一个**：`cleanup_stale_profiles` 扫的是整个根目录，
    上一步故意留着没删的目录（「占用中」「形状不匹配」）会在下一步变成
    可删的，`removed` 计数就被污染了 —— 第一版就是这么误判出两个失败的。
    """
    return Path(tempfile.mkdtemp(prefix="gad-cleanup-test-"))


def make_profile(root: Path, name: str, age_sec: float = 3600.0) -> Path:
    """造一个「看起来像 profile」的目录，并把 mtime 拨到过去。"""
    p = root / name
    p.mkdir()
    (p / "Default").mkdir()
    (p / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    if age_sec:
        old = time.time() - age_sec
        import os

        os.utime(p, (old, old))
    return p


def main() -> int:
    print("=" * 68)
    print("孤儿 profile 目录清理自测")
    print("=" * 68)

    # 每步一个独立 TEMP 根，通过 patch `tempfile.gettempdir` 指过去 ——
    # 绝不碰真实 %TEMP%（那里面有用户真实数据）。
    made: list[Path] = []

    def run_cleanup(**kw) -> int:
        """在**上一步刚建的沙箱**里跑一次清理。"""
        with mock.patch.object(tempfile, "gettempdir", return_value=str(root)), \
             mock.patch.object(cdp, "_process_command_lines", return_value=procs):
            return cdp.cleanup_stale_profiles(**kw)

    try:
        print("\n[1] 正常清理：形状匹配且无进程占用 -> 删")
        root = fresh_sandbox()
        made.append(root)
        procs = {1: "explorer.exe"}
        p1 = make_profile(root, "gad-chrome-aaaaaaaa")
        p2 = make_profile(root, "gad-chrome-bbbbbbbb")
        removed = run_cleanup()
        check("删掉了 2 个孤儿目录", removed == 2, f"removed={removed}")
        check("目录确实消失", not p1.exists() and not p2.exists())

        print("\n[2] 形状不匹配的目录绝不能被删")
        # 这是真实存在过的冲突：测试脚本用 `gad-chrome-engine-<8位>` 当引擎的
        # LOCALAPPDATA，前缀与 profile 相同。若用 `gad-chrome-*` 通配就会误删。
        root = fresh_sandbox()
        made.append(root)
        procs = {1: "explorer.exe"}
        engine_dir = make_profile(root, "gad-chrome-engine-c4s_3b3e")
        orphan_dir = make_profile(root, "gad-chrome-orphan-xyz")
        target = make_profile(root, "gad-chrome-cccccccc")
        removed = run_cleanup()
        check("只删了形状匹配的那 1 个", removed == 1, f"removed={removed}")
        check("engine 目录幸存", engine_dir.exists(), str(engine_dir.name))
        check("orphan 目录幸存", orphan_dir.exists(), str(orphan_dir.name))
        check("profile 目录被删", not target.exists())

        print("\n[3] 有进程正在用 -> 不能删")
        root = fresh_sandbox()
        made.append(root)
        busy = make_profile(root, "gad-chrome-dddddddd")
        procs = {42: f'"C:\\chrome.exe" --user-data-dir={busy} --remote-debugging-port=9222'}
        removed = run_cleanup()
        check("占用中的目录没被删", busy.exists() and removed == 0, f"removed={removed}")

        print("\n[4] 拿不到进程表 -> 一个都不删（宁留垃圾不误删）")
        root = fresh_sandbox()
        made.append(root)
        procs = {}
        p4 = make_profile(root, "gad-chrome-eeeeeeee")
        removed = run_cleanup()
        check("进程表为空时不删任何东西", p4.exists() and removed == 0, f"removed={removed}")

        print("\n[5] 保护期内的目录不删")
        root = fresh_sandbox()
        made.append(root)
        procs = {1: "explorer.exe"}
        fresh = make_profile(root, "gad-chrome-ffffffff", age_sec=0.0)
        removed = run_cleanup(min_age_sec=60.0)
        check("刚建的目录被保护", fresh.exists() and removed == 0, f"removed={removed}")
        # 同一目录，关掉保护期 -> 应被删
        removed = run_cleanup()
        check("保护期过后被删", not fresh.exists() and removed == 1, f"removed={removed}")

        print("\n[6] 路径大小写不敏感（Windows 语义）")
        # 进程表里的命令行是 Chrome 自己写进去的，大小写未必与 mkdtemp 一致。
        root = fresh_sandbox()
        made.append(root)
        c1 = make_profile(root, "gad-chrome-11111111")
        procs = {7: f'chrome.exe --user-data-dir={str(c1).upper()}'}
        removed = run_cleanup()
        check("大小写不同也认得出占用", c1.exists() and removed == 0, f"removed={removed}")

        print("\n[7] 空目录 / 无候选 -> 返回 0 且不抛")
        root = fresh_sandbox()
        made.append(root)
        procs = {1: "explorer.exe"}
        removed = run_cleanup()
        check("无候选时返回 0", removed == 0, f"removed={removed}")

    finally:
        import shutil

        for d in made:
            shutil.rmtree(d, ignore_errors=True)

    print("\n" + "-" * 68)
    print(f"  通过 {len(PASS)} · 失败 {len(FAIL)}")
    for name in FAIL:
        print(f"    [失败] {name}")
    print("-" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
