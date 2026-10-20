"""网络工具 —— web_fetch（读指定 URL）/ web_search（主动搜索）。

为什么需要它
------------
本客户端已能「联网」—— 但那是**服务端搜索**：DeepSeek 模型在服务端搜，
结果以文本回到模型面前。它的局限：
  * Agent **无法指定要读哪个具体 URL**
  * 结果进不了本地文件、存不进对话流
  * 与账号风控绑定（走 completion 接口）

本模块是**本地** HTTP：由这台电脑直接发请求，读回网页正文。
它与账号无关（走普通 HTTP），能精确指定 URL，结果可直接落盘。

设计约束
--------
* **零新依赖** —— 用标准库 urllib + html.parser。项目已实测「多一个依赖
  多一处风险」，而这两个工具不需要 TLS 指纹伪装（那是对抗 DeepSeek 风控用的，
  普通网站不需要）。
* **HTML → 文本** —— 剥离 script/style，保留可读正文。不追求完美渲染，
  够模型读懂即可。
* **体积上限** —— 网页可能巨大，截断到合理长度，防止一次 fetch 挤爆上下文。
* **超时与 UA** —— 有些站点对无 UA 请求返回 403，带一个正常的 UA。

诚实边界
--------
* 不做 JS 渲染 —— 纯静态 HTML。SPA 站点可能拿不到内容。
* 不做反爬对抗 —— 被 Cloudflare 等挡住就是挡住，不假装能过。
* `web_search` 用一个**无需 API key** 的公共端点（DuckDuckGo HTML 版），
  它的可用性不由我们保证 —— 拿不到结果时明确报错，不编造。
"""

from __future__ import annotations

import contextlib
import gzip
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

#: 单次 fetch 的正文上限（字符）。
MAX_FETCH_CHARS = 20000
#: 请求超时（秒）。
FETCH_TIMEOUT = 20.0
#: 搜索返回的结果条数上限。
MAX_SEARCH_RESULTS = 10

#: 带一个正常的 UA —— 有些站点对空 UA 直接 403。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

#: 只允许 http/https —— 防止 file:// / ftp:// 之类被当 fetch 目标。
_ALLOWED_SCHEMES = ("http", "https")

#: 跳过的标签（它们的内容不是可读正文）。
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})

#: 视为块级的标签（前后加换行，避免所有文字挤成一行）。
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "table",
})


class _TextExtractor(HTMLParser):
    """从 HTML 里抽可读文本。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
        if self._skip_depth > 0:
            return
        text = data.strip()
        if text:
            self._parts.append(text + " ")

    def text(self) -> str:
        raw = "".join(self._parts)
        # 折叠空白：多个空行 → 一个，行内多空格 → 一个
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


def _decompress(raw: bytes, encoding: str) -> bytes:
    """处理 gzip/deflate 响应体。"""
    enc = (encoding or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if "deflate" in enc:
        try:
            import zlib
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return raw
    return raw


def _http_get(url: str, timeout: float = FETCH_TIMEOUT) -> tuple[bytes, str, str]:
    """发 GET，返回 (响应体, content-type, 错误信息)。

    **不自动跟随重定向到非 http(s)** —— urllib 默认跟随，但我们要防
    「http → file://」这类跳转被利用。所以手动限制 scheme。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return b"", "", f"只支持 http/https，不接受 {parsed.scheme or '(空)'} 协议"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = resp.geturl()
            if urllib.parse.urlparse(final).scheme.lower() not in _ALLOWED_SCHEMES:
                return b"", "", f"重定向到了非 http(s) 地址：{final}"
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            raw = _decompress(raw, resp.headers.get("Content-Encoding", ""))
            return raw, ctype, ""
    except urllib.error.HTTPError as exc:
        return b"", "", f"HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        return b"", "", f"连接失败：{exc.reason}"
    except (OSError, ValueError) as exc:
        return b"", "", f"请求错误：{exc}"


def fetch(spec: str) -> tuple[bool, str]:
    """读一个 URL，返回可读正文。

    spec 形如 `https://example.com [max=5000]`。
    """
    url, max_chars = _split_url_max(spec)
    if not url:
        return False, "[web_fetch 失败] 缺少 URL。用法：web_fetch:<url> [max=字符数]"

    raw, ctype, err = _http_get(url)
    if err:
        return False, f"[web_fetch 失败] {url}\n{err}"

    if not raw:
        return False, f"[web_fetch 失败] {url}\n服务器返回空响应。"

    # 解码：优先用响应头里的 charset，退回 utf-8，再退回 latin-1
    text = _decode(raw, ctype)

    low_ctype = (ctype or "").lower()
    if "html" in low_ctype or text.lstrip().lower().startswith(("<!doctype", "<html")):
        parser = _TextExtractor()
        with contextlib.suppress(Exception):
            parser.feed(text)
        title = parser.title.strip()
        body = parser.text()
        head = f"[web_fetch 成功] {url}"
        if title:
            head += f"\n标题：{title}"
    else:
        body = text
        head = f"[web_fetch 成功] {url}\n类型：{ctype or '未知'}"

    truncated = False
    if len(body) > max_chars:
        body = body[:max_chars].rstrip()
        truncated = True

    head += f"\n正文 {len(body)} 字符"
    if truncated:
        head += f"（已截断至 {max_chars}，用 max=N 放宽）"
    if not body.strip():
        return True, f"{head}\n\n(未提取到可读正文 —— 可能是纯 JS 渲染的页面)"
    return True, f"{head}\n\n{body}"


def _decode(raw: bytes, ctype: str) -> str:
    charset = ""
    m = re.search(r"charset=([\w-]+)", ctype or "", re.IGNORECASE)
    if m:
        charset = m.group(1).strip()
    # 也从 HTML meta 里嗅探
    if not charset:
        head = raw[:2048].decode("ascii", errors="ignore")
        m2 = re.search(r'charset=["\']?([\w-]+)', head, re.IGNORECASE)
        if m2:
            charset = m2.group(1).strip()

    for enc in (charset, "utf-8", "gbk", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _split_url_max(spec: str) -> tuple[str, int]:
    """从 `url [max=N]` 里拆出 URL 与 max。"""
    s = (spec or "").strip()
    if not s:
        return "", MAX_FETCH_CHARS
    parts = s.split()
    url = parts[0]
    max_chars = MAX_FETCH_CHARS
    for p in parts[1:]:
        if p.lower().startswith("max="):
            with contextlib.suppress(ValueError):
                max_chars = max(200, min(int(p[4:]), 200000))
    return url, max_chars


# --------------------------------------------------------------------------
# web_search
# --------------------------------------------------------------------------

#: 用 DuckDuckGo 的 HTML 版（无需 API key）。**可用性不由我们保证**。
_DDG_URL = "https://html.duckduckgo.com/html/?q="

_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(s: str) -> str:
    """去标签 + 解码 HTML 实体 + 折叠空白。

    实体解码是必须的：搜索结果里 `&#x27;` / `&amp;` / `&quot;` 很常见，
    不解码的话模型看到的是 `Python&#x27;s` 这种噪声。
    """
    import html as _html

    text = _TAG_RE.sub("", s or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def search(spec: str) -> tuple[bool, str]:
    """搜索网页，返回结果列表。

    spec 形如 `关键词 [max=5]`。

    **依赖 DuckDuckGo HTML 版** —— 无需 API key，但可用性不由我们保证。
    拿不到结果时明确报错，不编造结果。
    """
    query, max_results = _split_query_max(spec)
    if not query:
        return False, "[web_search 失败] 缺少搜索词。用法：web_search:<关键词> [max=N]"

    url = _DDG_URL + urllib.parse.quote_plus(query)
    raw, ctype, err = _http_get(url)
    if err:
        return False, (
            f"[web_search 失败] 搜索请求出错：{err}\n"
            "（本工具依赖 DuckDuckGo HTML 版，可能被网络环境阻挡）"
        )

    html = _decode(raw, ctype)
    links = _RESULT_RE.findall(html)
    snippets = _SNIPPET_RE.findall(html)

    if not links:
        return False, (
            "[web_search 失败] 没解析到结果。\n"
            "可能：DuckDuckGo 返回了验证页 / 网络被挡 / 页面结构变了。\n"
            "可以改用 `web_fetch` 直接读某个已知 URL。"
        )

    lines: list[str] = []
    for i, (href, title_html) in enumerate(links[:max_results]):
        title = _clean_html(title_html)
        # DDG 的跳转链接：/l/?uddg=<真实URL>；能解出真实 URL 就显示它
        real = _unwrap_ddg(href)
        snippet = _clean_html(snippets[i]) if i < len(snippets) else ""
        lines.append(f"{i + 1}. {title}\n   {real}")
        if snippet:
            lines.append(f"   {snippet[:200]}")

    head = f"[web_search 成功] {query!r} · 返回 {min(len(links), max_results)} 条"
    return True, f"{head}\n\n" + "\n".join(lines)


def _unwrap_ddg(href: str) -> str:
    """把 DuckDuckGo 的跳转链接解成真实 URL。

    三种形态（实测）：
      1. `/l/?uddg=<URL编码的真实地址>` —— 普通结果
      2. `/y.js?...&u3=<URL编码的真实地址>` —— **广告**（u3 里才是真实地址）
      3. 直接就是 http(s) 链接

    广告链接如果解不出来，**明确标注为广告**而不是把一长串追踪 URL
    丢给模型 —— 那串 URL 没有任何信息价值，还占满上下文。
    """
    if "uddg=" in href:
        try:
            q = urllib.parse.urlparse(href).query
            real = urllib.parse.parse_qs(q).get("uddg", [""])[0]
            if real:
                return real
        except (ValueError, KeyError):
            pass

    if "u3=" in href:
        try:
            q = urllib.parse.urlparse(href).query
            real = urllib.parse.parse_qs(q).get("u3", [""])[0]
            if real:
                return real
        except (ValueError, KeyError):
            pass

    # 解不出来的广告链接（y.js / aclick 等）—— 标注而非原文输出
    if "y.js" in href or "aclick" in href or "ad_provider" in href:
        return "(广告链接，已省略追踪 URL)"

    if href.startswith("//"):
        return "https:" + href
    return href


def _split_query_max(spec: str) -> tuple[str, int]:
    s = (spec or "").strip()
    if not s:
        return "", MAX_SEARCH_RESULTS
    max_results = MAX_SEARCH_RESULTS
    m = re.search(r"\s+max=(\d+)\s*$", s)
    if m:
        with contextlib.suppress(ValueError):
            max_results = max(1, min(int(m.group(1)), MAX_SEARCH_RESULTS))
        s = s[: m.start()].strip()
    return s, max_results
