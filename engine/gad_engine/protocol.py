"""协议层 —— 把聊天协议硬造成函数调用协议。

网页版没有 function calling，只有文本。所以必须在聊天协议之上「伪造」一个
工具调用通道：约定代码围栏 + 解析 + 结果回传。这是本方案的本质，不可省。

契约沿用 deepseek-agent-desktop（该契约已过实战）。本层唯一的实质变化是
**解析路径**：从「页面渲染成 HTML 后再扫 DOM」改为「直接读 SSE 文本流」。

原版必须靠 banner 文本 / language-* class 反推语言，因为 markdown 一旦渲染成
HTML，反引号围栏就不存在了 —— 这是一个「信息已丢失、只能靠猜」的架构，也是
原版 36 个补丁里约 15 个 DOM 适配补丁的根源。直读文本后这类问题整体消失。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

FENCE = "```"
MAX_MARKER_LEN = 25

_OPENERS: tuple[str, ...] = (
    "local_cmd:async",
    "local_cmd",
    "write_file:",
    "replace_in_file:",
    "multi_edit:",
    "apply_patch:",
    "apply_patch",
    "read_file:",
    "list_dir:",
    "grep:",
    "glob:",
    "todo:",
    "job:",
    "ask_user:",
    "run_tests:",
    "run_tests",
    "mcp:",
    "mcp",
    "web_fetch:",
    "web_search:",
    "run_code:",
    "run_code",
    "agent-screenshot",
    "agent-attach",
)
_FENCE_OPENERS: tuple[str, ...] = tuple(FENCE + o for o in _OPENERS)

_WRITE_RE = re.compile(r"^write_file\s*:\s*(.+?)\s*$", re.IGNORECASE)

_THINK_BLOCK_RE = re.compile(
    r"<(?:thinking|thought|reasoning|think)\b[^>]*>.*?</(?:thinking|thought|reasoning|think)>",
    re.IGNORECASE | re.DOTALL,
)

#: 开标记：**行首**（只允许前导空白）+ ``` + 至少一个非空白字符（info 串）。
#: 「行首」是承重条件，不是美化 —— 见 `ToolCallParser._next_block` 的注释。
_FENCE_OPEN_RE = re.compile(r"^[ \t]*```(?=[ \t]*\S)", re.MULTILINE)

#: 闭标记：**整行只有三反引号**（前后允许空白）。这是 CommonMark 对「闭合围栏」
#: 的定义，也是这里唯一能自洽的判据：`X = "```local_cmd"` 这种内联的三反引号
#: 不是闭标记。
#: `[ \t\r]` 里那个 `\r` 是为 CRLF 留的 —— 少了它，`\n```\r\n` 会因为 `\r`
#: 没被吃掉而判不出闭标记，块永远不闭合。
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*```[ \t\r]*$", re.MULTILINE)


class ToolKind(StrEnum):
    LOCAL_CMD = "local_cmd"
    LOCAL_CMD_ASYNC = "local_cmd:async"
    WRITE_FILE = "write_file"
    REPLACE_IN_FILE = "replace_in_file"
    MULTI_EDIT = "multi_edit"
    APPLY_PATCH = "apply_patch"
    READ_FILE = "read_file"
    LIST_DIR = "list_dir"
    GREP = "grep"
    GLOB = "glob"
    TODO_WRITE = "todo:write"
    TODO_READ = "todo:read"
    JOB = "job"
    ASK_USER = "ask_user"
    RUN_TESTS = "run_tests"
    MCP = "mcp"
    WEB_FETCH = "web_fetch"
    WEB_SEARCH = "web_search"
    RUN_CODE = "run_code"
    GIT = "git"
    SCREENSHOT = "agent-screenshot"
    ATTACH = "agent-attach"


@dataclass(frozen=True)
class ToolCall:
    kind: ToolKind
    payload: str
    target: str = ""
    raw: str = ""

    @property
    def is_async(self) -> bool:
        return self.kind is ToolKind.LOCAL_CMD_ASYNC


def classify(info: str) -> tuple[ToolKind, str] | None:
    """把围栏 info 行分类成工具调用。非工具围栏返回 None。"""
    s = info.strip()
    low = s.lower()
    if low.startswith("local_cmd:async"):
        return ToolKind.LOCAL_CMD_ASYNC, ""
    if low == "local_cmd" or low.startswith("local_cmd:"):
        return ToolKind.LOCAL_CMD, ""
    m = _WRITE_RE.match(s)
    if m:
        return ToolKind.WRITE_FILE, m.group(1)
    if low.startswith("agent-screenshot"):
        return ToolKind.SCREENSHOT, ""
    if low.startswith("agent-attach"):
        rest = s[len("agent-attach") :].strip().lstrip(":").strip()
        return ToolKind.ATTACH, rest
    # 文件工具：单行参数走 info 行（如 `read_file:C:/x.py offset=10`）。
    # 顺序上 replace_in_file 必须先于其它判断 —— 它名字里有 `_file:`
    # 这种易与 write_file 混淆的子串，但前缀本身不重叠，放这里只为可读。
    for _name, _kind in (
        ("replace_in_file", ToolKind.REPLACE_IN_FILE),
        ("multi_edit", ToolKind.MULTI_EDIT),
        ("apply_patch", ToolKind.APPLY_PATCH),
        ("read_file", ToolKind.READ_FILE),
        ("list_dir", ToolKind.LIST_DIR),
        ("grep", ToolKind.GREP),
        ("glob", ToolKind.GLOB),
        ("job", ToolKind.JOB),
        ("ask_user", ToolKind.ASK_USER),
        ("run_tests", ToolKind.RUN_TESTS),
        ("mcp", ToolKind.MCP),
        ("web_fetch", ToolKind.WEB_FETCH),
        ("web_search", ToolKind.WEB_SEARCH),
        ("run_code", ToolKind.RUN_CODE),
        ("git", ToolKind.GIT),
    ):
        # 名字后必须紧跟 `:` 或到行尾 —— 否则 `read_file_x` 会被误判成
        # `read_file`（target 变成 `_x:...`）。这是实测发现的边界。
        if low == _name or low.startswith(_name + ":"):
            _rest = s[len(_name) :].strip().lstrip(":").strip()
            return _kind, _rest
    # 任务清单：`todo:write` / `todo:read` 是子命令，不是「todo: 参数」。
    # 必须精确匹配子命令 —— 否则 `todo:whatever` 会被当成未知调用静默丢弃。
    if low == "todo:write":
        return ToolKind.TODO_WRITE, ""
    if low == "todo:read":
        return ToolKind.TODO_READ, ""
    return None


def hold_tail_len(text: str, max_len: int = MAX_MARKER_LEN) -> int:
    """尾部后缀保持（供原始文本流路径使用）。

    若 text 末尾是任一开标记的前缀，返回需要扣住的字节数，否则返回 0。
    典型场景：缓冲区末尾是 "```loca"，它可能是 "```local_cmd" 的前半截，
    此时不能判定「这不是工具标记」，必须等下一块到达再合并判断。
    """
    limit = min(len(text), max_len)
    for n in range(limit, 0, -1):
        tail = text[-n:]
        if any(o.startswith(tail) and len(o) > n for o in _FENCE_OPENERS):
            return n
    return 0


def strip_think(text: str) -> str:
    """剥掉思维链草稿，防止模型把推理过程写成工具调用被误执行。

    原版靠排除 .ds-think-content 选择器实现；这里直接对文本处理。
    """
    return _THINK_BLOCK_RE.sub("", text)


class ThinkFilter:
    """跨分块剥离思维链块（纵深防御）。

    **本档位这道防线比 Gemini 侧更要紧**，原因是确定的：Gemini 的底层库
    `gemini_webapi` 把思考内容放在独立的 `thoughts_delta` 通道，属于**已知结构**；
    而 DeepSeek 的思考通道 SSE 路径**尚未实测确认**（没抓到 thinking 开启的真实流），
    `transport/deepseek_rpc.py` 只能按「路径含 `thinking`」做启发式分流。
    **启发式就会漏** —— 漏掉的那部分会混进正文。

    所以这里不是「万一」，是**必须的第二道防线**：一旦思维链草稿里的工具围栏
    被当成真指令执行，后果是直接在本机跑命令。
    """

    _OPEN = ("<thinking>", "<think>", "<thought>", "<reasoning>")
    _CLOSE = ("</thinking>", "</think>", "</thought>", "</reasoning>")
    _MAX_TAG = 12

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        out: list[str] = []
        while self._buf:
            if self._inside:
                idx = self._buf.find("</")
                if idx < 0:
                    # 可能藏着半个闭合标记，扣住尾巴
                    self._buf = self._buf[-self._MAX_TAG :]
                    break
                matched = next(
                    (t for t in self._CLOSE if self._buf.startswith(t, idx)), None
                )
                if matched is not None:
                    self._inside = False
                    self._buf = self._buf[idx + len(matched) :]
                    continue
                if any(t.startswith(self._buf[idx:]) for t in self._CLOSE):
                    # 半个闭合标记 —— 扣住，等下一块
                    self._buf = self._buf[idx:]
                    break
                self._buf = self._buf[idx + 2 :]
                continue

            idx = self._buf.find("<")
            if idx < 0:
                out.append(self._buf)
                self._buf = ""
                break
            matched = next(
                (t for t in self._OPEN if self._buf.startswith(t, idx)), None
            )
            if matched is not None:
                out.append(self._buf[:idx])
                self._inside = True
                self._buf = self._buf[idx + len(matched) :]
                continue
            if any(t.startswith(self._buf[idx:]) for t in self._OPEN):
                out.append(self._buf[:idx])
                self._buf = self._buf[idx:]
                break
            out.append(self._buf[: idx + 1])
            self._buf = self._buf[idx + 1 :]
        return "".join(out)


class ToolCallParser:
    """增量解析器。

    以「完整围栏块」为判定单位：只有开标记和闭标记都到齐，才认为一次工具
    调用成型。这样部分到达的块天然被扣住，不会误判。

    收益是双重的：正确性（不误判、不漏判），以及效率 —— 无需等整段输出结束，
    第一个工具调用可以在模型仍在输出后续内容时就开始执行。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._seen: set[str] = set()

    def feed(self, chunk: str) -> list[ToolCall]:
        self._buf += chunk
        out: list[ToolCall] = []
        while True:
            found = self._next_block()
            if found is None:
                break
            info, body, end = found
            cls = classify(info)
            if cls is not None:
                kind, target = cls
                payload = body.strip("\n")
                key = hashlib.sha1(
                    f"{kind.value}|{target}|{payload}".encode()
                ).hexdigest()[:16]
                if key not in self._seen:
                    self._seen.add(key)
                    out.append(
                        ToolCall(
                            kind=kind,
                            payload=payload,
                            target=target,
                            raw=self._buf[:end],
                        )
                    )
            self._buf = self._buf[end:]
        return out

    def pending(self) -> str:
        """尚未判定完的残留缓冲（诊断用）。"""
        return self._buf

    def _next_block(self) -> tuple[str, str, int] | None:
        """取出下一个**完整**的围栏块。

        两条约束都是硬需求，各自对应一次真实的故障：

        **① 开标记必须独占行首**（只允许前导空白）。
        否则正文里**内联**出现的 ``` 会被当成新块的开头。

        **② 闭标记必须独占一行**（前后只允许空白）。
        这是 CommonMark 对闭合围栏的规定。少了它，`write_file` 的正文里
        一旦出现内联的 ```（比如写一个**本身含围栏**的补丁脚本），块就会在
        那里被提前截断。

        ⚠️ 为什么②不能用「info 行之后第一个 ```」这种写法：
        2026-10-15 那次 run 的 turn 11 实测复现过。模型用 `write_file` 写
        `patch_protocol2.py`（一个给 `CONTRACT_RULES` 加说明的补丁脚本），
        正文里含 13 处 ```，其中三处是
        `"```read_file:<绝对路径> [offset=N limit=M]```\\n"` 这样的**字面量**。
        旧写法取 info 行之后第一个 ``` 当闭合 —— 于是：

          · 落盘的文件被截断（实测 796 字节，尾部停在半个字符串字面量上）；
          · 剩余正文失去同步，紧跟其后的**两个 `local_cmd` 块一个都没派发**；
          · 模型末尾说「请回传两个结果」，而结果永远不会来，
            run 以 `stop_reason=completed` 正常结束 ——
            用户看到的就是「对话跑着跑着断了，没有 tool call 继续」。

        这个故障**不报任何错**，所以只能靠约束本身堵住。
        """
        open_m = _FENCE_OPEN_RE.search(self._buf)
        if open_m is None:
            return None
        start = open_m.start()
        nl = self._buf.find("\n", start + 3)
        if nl < 0:
            return None  # info 行还没到齐
        info = self._buf[start + 3 : nl]

        close_m = _FENCE_CLOSE_RE.search(self._buf, nl + 1)
        if close_m is None:
            return None  # 块尚未闭合 —— 保持，不判定
        body = self._buf[nl + 1 : close_m.start()]
        return info, body, close_m.end()


CONTRACT_RULES = """\
# 工具调用协议（必须严格遵守）

你没有原生的 function calling。要操作这台电脑，你必须输出下面这种**代码围栏**：

```local_cmd
<一条 PowerShell 命令>
```

或直接落盘（不经过命令行）：

```write_file:<绝对路径>
<文件内容>
```

文件工具（改现有文件优先用这些，比 shell 可靠）：

```read_file:<绝对路径> [offset=N limit=M]```
读文件片段，带行号。offset 从 1 起。

```list_dir:<绝对路径> [depth=N]```
列目录树（默认深度 2，上限 4）。

```replace_in_file:<绝对路径> [count=N expect=M]```
精确替换一段**字面量**文本（不是正则，无需转义）。围栏正文格式：

<<<<<<< OLD
<原文，逐字照抄>
=======
<新文>
>>>>>>> NEW

count 默认 1（只替第一处）；count=0 替全部；expect 要求原文恰好出现 N 次。

**改文件前想先看看要改什么**：在上面三个编辑工具（replace_in_file / multi_edit / apply_patch）的参数里加 `preview=1`，它们会返回 diff 而**不写入**。确认无误后去掉 preview=1 重新调用即可应用。

```multi_edit:<绝对路径>```
同一文件**多处**编辑，一次落盘（任一处失败则整体不动）。围栏正文放多个三段块，块之间用 `===== EDIT =====``` 分隔。

```apply_patch [root=基准目录]```
应用一个标准 unified diff，**多文件**原子改动。围栏正文放 diff（`--- a/x` / `+++ b/x` / `@@ ... @@`）。只改已有文件。

```grep:<正则> [path=... glob=... max=N fixed=1 ignore_case=1]```
按内容搜索，返回 文件:行号:内容。fixed=1 按字面量匹配（搜含元字符的串时用）。

```glob:<模式> [root=... max=N]```
按文件名/路径搜索，如 `**/*.py`。

```git:status```
```git:diff [<路径>|--stat]```
```git:log [n=条数]```
只读：看仓库状态 / 改动 / 历史（`log` 默认 10 条，上限 50）。
写操作（commit/push/reset）一律走 `local_cmd`，不要找 git 工具。

```todo:write``` （围栏正文放清单）
```todo:read```
当前对话的任务清单。清单用 `- [ ] 任务` / `- [x] 已完成` 格式。长任务开始前先写清单，能防止跑着跑着忘了目标。

```job:list```
```job:status <作业号>```
```job:cancel <作业号>```
后台作业管理。用 `local_cmd:async` 起的命令会返回一个作业号，用 `job:status` 查它的输出。

```ask_user:<问题>```
遇到**真正需要用户决定**的岔路时（需求歧义、要动不可逆的东西、两个方案各有利弊），用这个工具把问题抛给用户，然后**停下来等回答**。不要自己猜一个继续。

```run_tests [path=... cmd=...]```
识别项目类型（pytest / npm / cargo / go / dotnet）并跑测试。`cmd=` 可显式指定命令，跳过识别。

```mcp:list```
```mcp:<server>/<tool> <JSON 参数>```
调用 MCP（Model Context Protocol）服务器的工具。先用 `mcp:list` 看有哪些可用，再用 `mcp:服务器名/工具名 {"参数": "值"}` 调用。

```web_fetch:<URL> [max=字符数]```
读一个网页，返回可读正文（HTML 会转成纯文本）。

```web_search:<关键词> [max=条数]```
搜索网页，返回结果列表。拿到 URL 后可用 web_fetch 读全文。

```run_code [timeout=秒 memory=MB]```
在**沙箱**里跑一段 Python 脚本。适合「批量处理」——一次读多个文件、筛选、聚合、批量改写。脚本只能写工作目录内的路径，不能起子进程。围栏正文放 Python 源码。

或走异步泳道（长命令，不阻塞对话）：

```local_cmd:async
<一条 PowerShell 命令>
```

视觉通道：
```agent-screenshot
```
```agent-attach:<绝对路径>
```

## 铁律

1. 首个非空白字符必须是 ``` 或普通叙述文字；**绝不要**用三重反引号包裹整段回答。
2. 开标记和闭标记都必须**独占一行** —— 行首到 ``` 之间只允许空白，``` 之后到
   行尾也只允许空白。**唯一**能判定「块到此结束」的就是这样一行三反引号。
3. 不要伪造工具执行结果。你只能提出调用，结果由本地回传。
4. 一次调用只做一件事；多个独立操作请分多次输出。
5. 写文件用 `write_file:<路径>`，路径一律用正斜杠。
6. 围栏正文里，**不要让某一整行只有三反引号** —— 那会被当成块结束，块就在那里
   被截断（文件会写坏、后面的调用会丢）。正文里确实需要这种字面量时，用字符串
   拼接绕开（例如 Python 里写 `"``" + "`"`），或给它加前缀/缩进。
7. 命令里不要出现三重反引号；需要反引号时用 `[char]96`。
8. 扫描目录必须带深度限制（如 `-Depth 3`）；无限制全盘扫描会被直接拒绝。
9. 提取失败时**换一种方式**，不要原样重发同一条命令。
10. `.bat` / `.cmd` 脚本内容只允许 ASCII 字符。
"""


#: 初始提示词的**默认模板**。
#:
#: 开源之后每个用户想要的「人格 / 语气 / 边界」都不一样，所以提示词不再是代码里的
#: 常量，而是一个**可编辑的模板文件**（默认落在
#: `<app_data>/prompts/system-prompt.md`，见 `config.PromptConfig`）。
#:
#: 模板里有两类占位符，性质完全不同：
#:   · `{{protocol}}` —— 工具调用协议（上面的 `CONTRACT_RULES`）。**承重**：
#:     它必须和本文件的解析器逐条对应，用户改坏了工具就整个失效。所以它由程序
#:     注入，用户只能决定**把它放在哪**，不能改它的内容。
#:   · `{{os}}` / `{{work_dir}}` / `{{shell}}` / `{{max_turns}}` / `{{soul}}` —— 运行时值。
DEFAULT_PROMPT_TEMPLATE = """\
# 角色

你是一个运行在桌面客户端里的本地代理。你的输出会被解析成对这台电脑的**实际操作**，
然后真实结果会回传给你。

{{protocol}}

# 运行环境

- 操作系统：{{os}}
- 默认工作目录：{{work_dir}}
- 命令解释器：{{shell}}
- 单轮对话内最多连续调用工具 {{max_turns}} 次

第一步：先读取 Soul 文件确认自身规则（若提示词中已内联 Soul，则跳过）。

{{soul}}

{{memory}}

{{summary}}
"""

#: 我们认识的占位符。设置界面用它来提示用户。
PROMPT_PLACEHOLDERS = (
    "protocol",
    "os",
    "work_dir",
    "shell",
    "max_turns",
    "soul",
    "memory",
    "summary",
)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def render_prompt(
    template: str,
    *,
    os_name: str,
    work_dir: str,
    shell_hint: str,
    soul: str = "",
    memory: str = "",
    summary: str = "",
    max_turns: int = 30,
) -> str:
    """把模板里的占位符替换成运行时值。

    **不认识的占位符原样留着**，不静默丢掉。用户把 `{{soul}}` 打成 `{{Soul}}` 时，
    要能在预览里一眼看出来 —— 而不是提示词里凭空少一段、还在奇怪模型为什么不听话。

    替换走 `_PLACEHOLDER_RE`（与 `missing_placeholders()` **同一个正则**），
    不是 `str.replace("{{key}}")` 字面量匹配。这两者曾经不一致：正则允许
    占位符内带空格，`{{ protocol }}` 因此被判为「已知、无需告警」，字面量替换
    却匹配不上，于是**原样留在提示词里** —— 用户看到的是「没有报错，但模型
    不调工具了」。一个判据、一个实现，必须同源。
    """
    values = {
        "protocol": CONTRACT_RULES,
        "os": os_name,
        "work_dir": work_dir,
        "shell": shell_hint,
        "max_turns": str(max_turns),
        # 未配置 Soul 时**不能留空**：默认模板里写着「第一步：先读取 Soul 文件确认自身规则」，
        # 而提示词里又没有 Soul —— 模型会真的去找一个它不知道路径的文件。
        # 2026-10-15 实测：思考内容原话「Soul 文件路径未知。我不知道在哪。通常可能是
        # C:\Users\Admin\Soul.md 之类」—— 白耗一轮，甚至可能触发无谓的工具调用。
        # 所以这里显式说明「没有就是没有」，把话堵死。
        "soul": (
            f"# Soul\n{soul}"
            if soul
            else "# Soul\n（本机未配置 Soul 文件 —— **跳过读取**，直接执行任务。）"
        ),
        # 长期记忆。与 Soul 的处理**刻意不同**：Soul 未配置时要显式说明
        # 「跳过读取」，因为默认模板里有一句「先读取 Soul 文件」会造成指代悬空；
        # 而记忆为空是**正常状态**（新用户本来就什么都没有），给一段
        # 「暂无记忆」的说明纯属占 token。空就是空。
        "memory": memory or "",
        # 接力摘要（见 `compact.py`）。与记忆同样「空就是空」—— 绝大多数对话
        # 不是接力来的，这里本来就该是空串，不该留一段占位说明。
        "summary": summary or "",
    }
    # 单次扫描替换：`re.sub` 的回调不会二次展开 —— 若某个值里恰好含
    # `{{os}}` 这类字面量（Soul 文件或记忆里写了示例），也不会被继续替换掉。
    # 逐个 `str.replace` 的循环则有这个隐患。
    out = _PLACEHOLDER_RE.sub(
        lambda m: values.get(m.group(1).strip(), m.group(0)), template
    )
    # 占位符替换成空串（如没有 Soul）会留下连续空行，收一下。
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.strip()

    # ★ 工具调用协议是**承重件**，不能因为模板里没写就消失。
    #
    # `{{protocol}}` 与 `{{memory}}` / `{{summary}}` 的性质不同：后两者缺失
    # 只是少一段可选上下文，前者缺失会让模型完全不知道要输出围栏 ——
    # 表现是「正常聊天、什么都不做」，**界面上看起来一切正常**。
    # 用户删掉它（嫌长、或手工整理模板时漏了）是很容易发生的事，
    # 而 `missing_placeholders()` 报不出来（它不是「不认识的占位符」）。
    #
    # 所以：模板没写 `{{protocol}}`，就把协议追加到末尾。用户仍然可以
    # 决定它在**有**的时候放哪；但「没有」这个选项不成立。
    declared = {m.strip() for m in _PLACEHOLDER_RE.findall(template)}
    if "protocol" not in declared:
        # 空模板（或全是未知占位符）时 `out` 是空串 —— 直接拼会得到
        # `"\n\n# 工具调用协议…"` 这种前导空行。
        out = f"{out}\n\n{CONTRACT_RULES}".strip()
    return out


def missing_placeholders(template: str) -> list[str]:
    """模板里出现的、我们不认识的占位符。设置界面据此警告。"""
    found = _PLACEHOLDER_RE.findall(template or "")
    return sorted({name for name in found if name not in PROMPT_PLACEHOLDERS})


def declares(template: str, name: str) -> bool:
    """模板里有没有写这个占位符（允许 `{{ name }}` 带空格）。

    调用方拿它决定「要不要把这段内容追加到末尾」。**必须走同一个正则** ——
    用 `"{{name}}" in template` 字面量判断，遇到 `{{ memory }}` 就会
    判成「没写」，于是内容被追加一次、`render_prompt` 又替换一次，
    提示词里同一段出现两遍（白烧 token，且模型可能把重复当强调）。
    """
    return name in {m.strip() for m in _PLACEHOLDER_RE.findall(template or "")}


def build_contract_prompt(
    os_name: str,
    work_dir: str,
    shell_hint: str,
    soul: str = "",
    memory: str = "",
    summary: str = "",
    max_turns: int = 30,
    template: str | None = None,
) -> str:
    """拼装系统提示词。

    `template=None` 时用内置默认模板；传进来的是用户在设置里改过的模板。
    """
    return render_prompt(
        template if template is not None else DEFAULT_PROMPT_TEMPLATE,
        os_name=os_name,
        work_dir=work_dir,
        shell_hint=shell_hint,
        soul=soul,
        memory=memory,
        summary=summary,
        max_turns=max_turns,
    )


def format_command_result(exit_code: int, output: str, timed_out: bool = False) -> str:
    """命令执行结果的回传格式。

    格式照搬 deepseek-agent-desktop —— 已验证能让模型正确接续。
    """
    head = f"[Tool Call Result (Exit: {exit_code})]:"
    if timed_out:
        head = f"[Tool Call Result (Exit: {exit_code}, 超时中止)]:"
    body = output if output.strip() else "(无输出)"
    return f"{head}\n{body}\n\n请输出下一个 ```local_cmd，或结束本轮。"


def format_write_result(target: str, exit_code: int, note: str = "") -> str:
    head = f"[Tool Call: 本地文件直接写入结果 (Exit: {exit_code})]"
    lines = [head, f"目标：{target}"]
    if note:
        lines.append(note)
    lines.append("")
    lines.append("请输出下一个 ```local_cmd，或结束本轮。")
    return "\n".join(lines)


def format_attachment_result(path: str, ok: bool) -> str:
    state = "就绪" if ok else "失败"
    return (
        f"[Tool Call 附件{state}]\n{path}\n\n请输出下一个 ```local_cmd，或结束本轮。"
    )


def format_file_tool_result(ok: bool, text: str) -> str:
    """文件工具（read_file / list_dir / replace_in_file）的回传格式。

    这些工具自己已经产出了「状态行 + 内容」，这里只负责补上收尾提示。
    """
    tail = '请输出下一个 ```local_cmd，或结束本轮。'
    return f"{text}\n\n{tail}"


def format_ask_user(question: str) -> str:
    """ask_user 的工具结果。

    **不能**套用 format_command_result 的收尾 —— 那句「请输出下一个 local_cmd」
    会让模型问完问题又继续动手，正好和 ask_user 的目的相反。
    """
    q = (question or "").strip() or "(未提供问题内容)"
    return (
        "[ask_user] 问题已提交给用户：\n\n"
        f"{q}\n\n"
        "请**停止输出**，等待用户回答。不要继续调用任何工具。"
    )
