"""【已迁移】生成器已归档到 `tools/legacy/build_css_20261016.py`。

## 为什么留这个文件

2026-10-18 之前，这个路径下是一个**能一键覆盖 `Assets/web/app.css`** 的生成器。
问题是 `app.css` 早已成为唯一真源（头部注释自己就写着），上面有大量设计稿之后
的手工增量 —— `.seg` 分段控件、`body.dropping` 拖拽落点提示、各类状态色……
实测跑一次会让 522 行掉到 345 行，**直接冲掉 177 行改动**。

归档而不是删除，理由有两个：
  1. 万一要从设计稿重建，它是唯一的起点；
  2. 任何还记得这个路径的人（或脚本）再来找，能立刻知道发生了什么。

## 现在该怎么做

- **改样式** → 直接改 `shell/DeepSeekAgent.Web/Assets/web/app.css`
- **确实要重建** → `python tools/legacy/build_css_20261016.py`（dry-run，
  会告诉你丢多少行）；确认无误再 `--force`，且**先自己备份**

这个文件本身不执行任何操作。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_MOVED = "tools/legacy/build_css_20261016.py"

print(f"[已归档] 生成器已移到 {_MOVED}")
print("         本路径不再生成任何文件，不会覆盖 app.css。")
print()
print("  改样式   -> 直接改 shell/DeepSeekAgent.Web/Assets/web/app.css")
print(f"  要重建   -> python {_MOVED}            # dry-run，报告会丢多少行")
print(f"              python {_MOVED} --force    # 真覆盖（先备份！）")

raise SystemExit(0)
