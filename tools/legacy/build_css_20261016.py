"""【已归档 · 2026-10-18】一次性生成器，**不要直接跑**。

## 为什么归档

这个脚本是 2026-10-16 用来「从设计稿生成第一版 app.css」的。
但从那天之后，`app.css` 上有了大量**手工增量**（`.seg` 分段控件、
`body.dropping` 拖拽落点提示、各类状态色……），而 `app.css` 头部注释
也明确写着「生成之后 app.css 就是唯一真源」。

于是这个脚本**已经和真源脱钩**：2026-10-18 实测跑一次，产物从 522 行
掉到 345 行 —— 直接冲掉 177 行手工改动。当时是靠备份恢复的。

## 现在的约束

保留它只为两件事：① 记录第一版是怎么来的 ② 万一要从设计稿重建时当起点。
真跑必须显式 `--force`，且**先自己备份 `app.css`**。

    python tools/legacy/build_css_20261016.py            # 只做 dry-run，打印会丢什么
    python tools/legacy/build_css_20261016.py --force    # 真的覆盖（危险）

---

原说明（保留作史料）：

把设计稿的 <style> 块清洗成唯一一份 app.css。

做三件事：
  1. 删掉 @media 断点 —— 设计稿是给浏览器看的，带「<=1180px 藏右栏、
     <=880px 再藏左栏」两条断点。桌面窗口里这是灾难（150% DPI 的 1920 屏，
     窗口内容宽约 1191 CSS px，离 1180 只差 11px，稍微拉窄右栏就整块消失）。
  2. 删掉完全重复的规则块（选择器 + 声明都一样）。
  3. 追加应用层补充（设计稿没有的那部分，见下面 APP 常量）。

**刻意不做**「同名选择器合并」：合并会把靠后的声明提到靠前的位置，
在「特异性相同、中间还夹着别的选择器」时会悄悄改变层叠结果。
设计稿的层叠顺序是验证过的，不动它。重复但不同内容的规则（比如 .shell
先 height:calc(100% - 48px)、后被 flex 覆盖）保留原样，靠顺序生效。

生成之后 app.css 就是唯一真源，改样式直接改 app.css。
"""
import os
import re

ROOT = r"C:\Users\Admin\Documents\Projects\deepseek-agent-desktop"
SRC = os.path.join(ROOT, r"docs\design\20261016-界面设计-v2.html")
OUT = os.path.join(ROOT, r"shell\DeepSeekAgent.Web\Assets\web\app.css")

APP = """
/* ============================================================================
   应用层补充（设计稿里没有的部分）
   ----------------------------------------------------------------------------
   本文件由 tools/build_css.py 生成：设计稿 <style>（已删断点）+ 下面这段。
   生成之后 **app.css 就是唯一真源** —— 改样式直接改这里，不要再回头改设计稿。
   ============================================================================ */

/* --- 1) 纵向三段式：顶栏 / 三栏主体 / 输入区 --------------------------------
   设计稿的 .shell 是 height:calc(100% - 48px)，只减了顶栏 48px；但 body 里
   除 .shell 还有 .composer，于是输入区被挤出视口、被 body{overflow:hidden}
   吞掉（截图里底部整块空白就是这个原因）。改成 flex 列。 */
body{display:flex;flex-direction:column;height:100vh}
.titlebar{flex:none}
.shell{flex:1 1 auto;min-height:0;height:auto}
.composer{flex:none}

/* --- 2) 桌面窗口不该有浏览器断点 -------------------------------------------
   断点已在生成时删除。这里再给一个下限：窄到放不下就让外层横向滚动，
   也不让任何一栏凭空消失。窗口自身的 MinWidth 是第一道闸。 */
body{overflow-x:auto}
.shell{min-width:820px}

/* --- 3) 输入区：和对话列共用一条中线 ---------------------------------------
   设计稿 .composer .inner 只给了 max-width，漏了 margin:0 auto
   （.inner 的通用规则里有），于是输入框和正文不在一条中线上。 */
.composer .inner{max-width:760px;margin:0 auto}
.box textarea{resize:none;overflow-y:auto}

/* --- 4) 屏外块跳过渲染 ------------------------------------------------------
   实测 600 块首屏插入 134ms → 4.7ms。代价是高度估算偏差约 10%，滚动条会轻微
   跳动；contain-intrinsic-size 的 auto 关键字会在滚动过后用真实尺寸校正。 */
.msg,.tool,.turn,.sys{content-visibility:auto;contain-intrinsic-size:auto 110px}

/* --- 5) 细滚动条（WebView2 是 Chromium，::-webkit-scrollbar 直接可用） ---- */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{
  background:#C9D2DE;border-radius:5px;
  border:2px solid transparent;background-clip:content-box;
}
::-webkit-scrollbar-thumb:hover{background:#AEB6C2;background-clip:content-box}

/* --- 6) 设计稿没定义的状态色 ------------------------------------------------ */
[hidden]{display:none !important}
.pill.idle{background:var(--surface-3);color:var(--ink-2)}
.pill.idle .dot{background:var(--ink-4)}
.badge.bad{background:var(--bad-soft);color:var(--bad-ink)}
.badge.run{background:var(--brand-soft);color:var(--brand-ink)}

/* --- 7) 思考块（设计稿没画这一类）------------------------------------------
   左侧竖线 + 淡字，让「这是旁注、不是回答」在视觉上就成立。 */
.thought{margin:4px 0 10px;padding-left:11px;border-left:2px solid var(--line)}
.thought-head{
  display:flex;align-items:center;gap:6px;
  font-size:11.5px;color:var(--ink-3);cursor:pointer;user-select:none;
}
.thought-head:hover{color:var(--ink-2)}
.thought-head .ttext{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.thought-head .tcaret{
  margin-left:auto;color:var(--ink-4);flex:none;
  transition:transform .18s;transform:rotate(180deg);
}
.thought.collapsed .thought-head .tcaret{transform:rotate(0)}
.thought-body{
  margin-top:6px;font-size:12px;color:var(--ink-2);line-height:1.72;
  white-space:pre-wrap;word-break:break-word;max-height:340px;overflow:auto;
}
.thought.collapsed .thought-body{display:none}

/* --- 8) 监视器日志行（设计稿没有监视器视图）------------------------------- */
.mon-line{
  display:flex;gap:8px;align-items:baseline;
  font-family:var(--mono);font-size:11.5px;
  padding:3px 0;border-bottom:1px solid var(--line-2);
}
.mon-line .at{color:var(--ink-4);flex:none}
.mon-line .lv{flex:none;color:var(--ink-3);text-transform:lowercase}
.mon-line .lv.warn{color:var(--warn-ink)}
.mon-line .lv.error{color:var(--bad-ink)}
.mon-line .tx{color:var(--ink-2);white-space:pre-wrap;word-break:break-word}

/* --- 9) 空状态 -------------------------------------------------------------- */
.empty-state{padding:56px 16px;text-align:center;color:var(--ink-3);font-size:12.5px}

/* --- 10) 任务列表失败态（设计稿只有 done / doing / pending）--------------- */
.td-item.fail .tdt,.td-item.fail .tds{color:var(--bad-ink)}

/* --- 11) 自绘标题栏：拖拽区不选中文字 ------------------------------------- */
.titlebar{user-select:none;-webkit-user-select:none;cursor:default}

/* --- 12) 状态行（.sys）------------------------------------------------------
   设计稿只画了三种底色：默认（琥珀 = 警告）、.ok（绿）、.bad（红），
   结构是「图标 + span<b>标题</b> · 正文」。但「信息 / 摘要 / 思考中」这类
   中性事件不该用警告色，补一个 .idle。图标与标题的排版也在这里补齐，
   免得每条状态行都要写行内样式。 */
.sys.idle{background:var(--surface-3);color:var(--ink-2)}
.sys>svg{flex:none;margin-top:2px}
.sys b{font-weight:600}

/* --- 13) 从右栏跳到对话流时的高亮 ------------------------------------------
   右栏点一条工具，中间栏滚到对应那张卡并闪一下。两个挂点：右栏条目自己
   （.titem）和对话流里的工具卡（.tool）。 */
.titem.flash,.tool.flash{box-shadow:0 0 0 2px var(--brand-soft-2);border-color:var(--brand)}

/* --- 14) 左栏列表的空态 / 加载态 --------------------------------------------
   .empty-state 是给主区用的（padding 56px），塞进 220px 宽的侧栏里会顶出一大块
   空白，看着像坏了。侧栏用自己的一份。 */
.side-empty{padding:18px 8px;text-align:center;color:var(--ink-3);font-size:11.5px;line-height:1.6}
"""


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def split_blocks(css: str):
    """按顶层花括号切块 -> [(prelude, body)]。@keyframes 的内层花括号靠深度计数吞掉。"""
    out, depth, start, prelude, body_start = [], 0, 0, "", 0
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                prelude = css[start:i].strip()
                body_start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append((prelude, css[body_start:i]))
                start = i + 1
    return out


def norm(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip()


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = sys.argv[1:] if argv is None else argv
    force = "--force" in argv

    with open(SRC, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"<style>(.*?)</style>", html, re.S)
    assert m, "设计稿里没找到 <style> 块"
    css = strip_comments(m.group(1))

    kept, seen, dropped_media, dropped_dup = [], set(), 0, 0
    for prelude, body in split_blocks(css):
        if prelude.startswith("@media"):
            dropped_media += 1
            continue
        if prelude.startswith("@"):                 # @keyframes 等原样保留
            kept.append(prelude + "{" + body + "}")
            continue
        key = (prelude, norm(body))
        if key in seen:
            dropped_dup += 1
            continue
        seen.add(key)
        kept.append(prelude + "{" + norm(body) + "}")

    base = "\n".join(kept)
    out = base + APP

    # ---- 归档闸门：默认只 dry-run，绝不覆盖 ----
    #
    # 这一闸是 2026-10-18 加的。加之前这个脚本可以随手跑、随手把
    # `app.css` 冲掉 177 行手工增量 —— 我本人就这么踩过一次。
    existing = ""
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            existing = f.read()

    cur_lines = existing.count("\n") + (1 if existing else 0)
    new_lines = out.count("\n") + 1
    lost = cur_lines - new_lines

    print("设计稿 CSS :", len(css), "字符")
    print("删掉 @media :", dropped_media, "块")
    print("删掉重复块 :", dropped_dup, "块")
    print("保留规则   :", len(kept), "条")
    print(f"当前 app.css: {cur_lines} 行 / {len(existing)} 字符")
    print(f"本次将生成  : {new_lines} 行 / {len(out)} 字符")

    if lost > 0:
        print(f"\n[警告] 覆盖会丢掉 {lost} 行 —— 那些是设计稿之后的手工增量。")

    if not force:
        print("\n[已归档] 未写入。要真覆盖请加 --force，并先自行备份 app.css。")
        return 2

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # 用 with 而不是 `open(...).write(...)`：后者把文件对象交给 CPython 的
    # 引用计数去关，在 PyPy / 异常路径上不保证立刻 flush —— 这里写的是生成物，
    # 半截文件会让下游构建读到残缺 CSS。
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print(f"\n[已写入] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
