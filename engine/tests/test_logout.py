"""`identity.logout` 的 profile 清理逻辑自测（全程离线）。

钉死三件事：

1. 目录可删时整删，返回 `(True, [])`。
2. 目录里有关不掉的文件（实测 `BrowserMetrics/*.pma` 会被 Chrome 的
   crashpad 句柄占住）时，**不能整个失败** —— 必须降级清掉登录态关键项。
3. 登录态文件本身删不掉（例如 Cookies 被占用）时，必须把它报出来，
   让界面告诉用户「先关掉浏览器」，而不是假装成功。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_logout.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gad_engine.__main__ import _clear_chrome_login_state

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "通过" if cond else "失败"
    extra = f" — {detail}" if detail and not cond else ""
    print(f"  [{mark}] {name}{extra}")


def make_profile(root: Path) -> None:
    (root / "Default" / "Local Storage" / "leveldb").mkdir(parents=True)
    (root / "Default" / "Cookies").write_text("cookie", encoding="utf-8")
    (root / "Default" / "Local Storage" / "leveldb" / "000003.log").write_text(
        "token", encoding="utf-8"
    )
    (root / "BrowserMetrics").mkdir()
    (root / "BrowserMetrics" / "metrics.pma").write_text("metrics", encoding="utf-8")


def main() -> int:
    print("[1] 目录可删 → 整删")
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "chrome-profile"
        make_profile(profile)
        removed, failed = _clear_chrome_login_state(profile)
        check("返回整删标记", removed is True)
        check("无失败项", failed == [], str(failed))
        check("目录已不存在", not profile.exists())

    print("\n[2] BrowserMetrics 被占用 → 降级清登录态，不整体失败")
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "chrome-profile"
        make_profile(profile)
        locked = profile / "BrowserMetrics" / "metrics.pma"
        # Windows 上保持读句柄会让 unlink 失败 —— 正是实测里 Chrome 的行为。
        # 用 `with` 是为了异常路径也一定释放句柄；块内不关它，
        # 「被占用」这个前提在整个被测调用期间都成立。
        with open(locked, encoding="utf-8"):
            removed, failed = _clear_chrome_login_state(profile)
            check("未整删（符合预期）", removed is False)
            check("降级路径无失败项", failed == [], str(failed))
            check("Cookies 已清", not (profile / "Default" / "Cookies").exists())
            check(
                "Local Storage 已清",
                not (profile / "Default" / "Local Storage").exists(),
            )

    print("\n[3] Cookies 本身删不掉 → 必须报出来")
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "chrome-profile"
        make_profile(profile)
        cookies = profile / "Default" / "Cookies"
        with open(cookies, encoding="utf-8"):
            removed, failed = _clear_chrome_login_state(profile)
            check("未整删（符合预期）", removed is False)
            check("失败项非空", bool(failed), str(failed))
            check("失败项指向 Cookies", any("Cookies" in f for f in failed), str(failed))

    print("\n" + "-" * 60)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("  [退出登录清理自测未通过]")
        return 1
    print("  [退出登录清理自测通过]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
