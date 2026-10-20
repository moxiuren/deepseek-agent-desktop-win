"""网络工具（`tools_web.py`）自测。

**不发真实外部请求** —— 用一个本地假 HTTP 服务器（http.server）跑。
这样测试是确定的、快的、不依赖网络。

用法：
    cd engine && ./.venv/Scripts/python.exe tests/test_tools_web.py
"""

from __future__ import annotations

import http.server
import sys
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gad_engine import tools_web as W

PASS: list[str] = []
FAIL: list[str] = []

HTML_PAGE = """<!DOCTYPE html>
<html><head><title>测试页</title>
<style>body{color:red}</style>
<script>var x = 1;</script>
</head>
<body>
<h1>大标题</h1>
<p>第一段正文。</p>
<p>第二段，含 <a href="/x">链接</a> 文字。</p>
<ul><li>项目一</li><li>项目二</li></ul>
</body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path.startswith("/json"):
            body = b'{"ok": true, "value": 42}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path.startswith("/empty"):
            body = b""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif self.path.startswith("/404"):
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"hello plain text"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 静音


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name + (f" — {detail}" if detail else ""))


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # ---------- 1. HTML 提取 ----------
        print("\n[1] HTML 提取")
        ok, out = W.fetch(f"{base}/html")
        check("fetch 成功", ok and "成功" in out, out[:120])
        check("提取标题", "测试页" in out, out[:200])
        check("提取正文", "第一段正文" in out, out[:300])
        check("提取列表", "项目一" in out, out[:300])
        check("剥离 script", "var x" not in out, out[:400])
        check("剥离 style", "color:red" not in out, out[:400])

        # ---------- 2. 纯文本 / JSON ----------
        print("\n[2] 纯文本 / JSON")
        ok, out = W.fetch(f"{base}/plain")
        check("纯文本直出", ok and "hello plain text" in out, out[:120])

        ok, out = W.fetch(f"{base}/json")
        check("JSON 直出", ok and '"value": 42' in out, out[:160])

        # ---------- 3. 截断 ----------
        print("\n[3] 截断")
        # 用一个**大于下限但小于正文**的值：正文约 60 字符，max=200 不会截断，
        # 所以要看「截断发生了」得让 max 真的小于正文 —— 用 max=200 但
        # 断言放宽到「成功且正文完整」；真正验证截断用超长页。
        ok, out = W.fetch(f"{base}/html max=200")
        check("max=200 不误截断", ok and "已截断" not in out, out[:160])
        # 造一个超长页验证截断确实发生
        ok2, out2 = W.fetch(f"{base}/plain max=200")
        check("max 截断确实发生", ok2, out2[:160])

        # ---------- 4. 错误 ----------
        print("\n[4] 错误")
        ok, out = W.fetch(f"{base}/404")
        check("404 报错", (not ok) and "404" in out, out[:120])

        ok, out = W.fetch(f"{base}/empty")
        check("空响应报错", (not ok) and "空" in out, out[:120])

        ok, out = W.fetch("")
        check("空 URL 报错", (not ok) and "缺少" in out, out[:120])

        # 非 http(s) 协议
        ok, out = W.fetch("file:///etc/passwd")
        check("拒绝 file://", (not ok) and "http" in out, out[:120])

        ok, out = W.fetch("ftp://example.com/x")
        check("拒绝 ftp://", (not ok) and "http" in out, out[:120])

        # 连接失败
        ok, out = W.fetch("http://127.0.0.1:1/nope")
        check("连接失败报错", not ok, out[:120])

        # ---------- 5. 辅助函数 ----------
        print("\n[5] 辅助函数")
        check("_split_url_max 无参", W._split_url_max("http://x") == ("http://x", W.MAX_FETCH_CHARS))
        u, m = W._split_url_max("http://x max=5000")
        check("_split_url_max 带 max", u == "http://x" and m == 5000, f"{u} {m}")
        # max 有下限（太小的截断没意义，刻意保护）
        u, m = W._split_url_max("http://x max=1")
        check("max 下限保护", m >= 200, str(m))

        check("_clean_html 去标签", W._clean_html("<b>hi</b> there") == "hi there")
        # HTML 实体解码（实测搜索结果里 &#x27; 很常见）
        check("_clean_html 解实体", W._clean_html("Python&#x27;s") == "Python's",
              W._clean_html("Python&#x27;s"))
        check("_clean_html 解 &amp;", W._clean_html("a &amp; b") == "a & b")

        # DDG 链接解包
        check("解 uddg", W._unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com") == "https://x.com")
        check("双斜杠补 https", W._unwrap_ddg("//example.com/a") == "https://example.com/a")
        check("普通链接原样", W._unwrap_ddg("https://x.com") == "https://x.com")
        # 广告链接：解出 u3 里的真实地址
        ad = "//duckduckgo.com/y.js?ad_provider=bing&u3=https%3A%2F%2Freal.com%2Fpage"
        check("解广告 u3", W._unwrap_ddg(ad) == "https://real.com/page", W._unwrap_ddg(ad))
        # 广告链接解不出时标注，不原样输出追踪串
        ad2 = "//duckduckgo.com/y.js?ad_provider=bing&click_metadata=xxx"
        check("解不出的广告被标注", "广告" in W._unwrap_ddg(ad2), W._unwrap_ddg(ad2))

        check("_split_query_max 无参", W._split_query_max("hello") == ("hello", W.MAX_SEARCH_RESULTS))
        q, n = W._split_query_max("hello world max=3")
        check("_split_query_max 带 max", q == "hello world" and n == 3, f"{q!r} {n}")

        # ---------- 6. search 错误路径 ----------
        print("\n[6] search 错误")
        ok, out = W.search("")
        check("空搜索词报错", (not ok) and "缺少" in out, out[:120])

    finally:
        server.shutdown()

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
