# 交接文档 · PoW 加速（让工具调用更快）

> 交接时间：2026-10-16 13:05 +0800
> 仓库：`C:/Users/Admin/Documents/Projects/deepseek-agent-desktop`
> 本轮提交：**`e6e0c83`**（9 文件，+429 / −10）
> 状态：**Phase 1 + Phase 1.5 已实现、已提交、已验证；Phase 1 真实数据采集与 Phase 2 未开始**
> 用户指令：本轮结束时「写交接文档 停止工作」

---

## 一、一句话现状

PoW（工作量证明）里藏着**一个每轮恒定浪费 10 秒的 bug**，已修（实测 10.1s → 约 3.5s）；
计量与离线门禁已补齐并提交。**但真正的上游往返（11–12s）不在本轮可动范围内** —— 详见第七节。

---

## 二、本轮诉求与硬约束

用户诉求（原文）：

> 「那么评估加速的可能性 让工具调用更快一些」

经 `AskUserQuestion` 确认的两条硬约束（原文）：

| 维度 | 用户选择 | 含义 |
|---|---|---|
| 优化目标 | **先量 PoW 再优化（推荐）** | 第一步是加计量，不是直接改求解器 |
| 风险边界 | **只做纯本地优化，一次上游请求都不多加** | 明确排除「预解 PoW」「复用 challenge」等需要额外请求的方案 |

方案全文（已获批）：`C:/Users/Admin/.workbuddy-ai/plans/stellar-vortex-newton-3TG5Gx69.md`

---

## 三、已完成（已提交，`e6e0c83`）

### 3.1 核心 bug 修复 —— 本轮最大收益

`engine/gad_engine/pow.py` 的 `_parallel_search()` 调用 `concurrent.futures.wait()` 时
**漏了 `return_when=FIRST_COMPLETED`**。

- `concurrent.futures.wait` 的**默认值是 `ALL_COMPLETED`** —— 与函数自身文档
  「第一个分片必须返回」**正好相反**。
- 后果：那一行不是「等第一个分片」，而是「等**全部 36 个**分片跑完」，
  被 `startup_timeout=10` 截断 ⇒ **每轮恒定等满 10 秒，与答案位置完全无关**
  （答案是 0、第一个候选就命中，照样等满）。
- 而那 10 秒是**纯本地空耗**，全加在「按下回车 → 请求真的发出」之间。

**实测（干净进程单测）：**

| 场景 | 修前 | 修后 |
|---|---|---|
| 答案 = 0（首片即命中） | 10089 ms | **405 ms** |
| 真实挑战 | 10.1 s | **5.6 s** |
| 离线合成基准（见第五节） | 恒定 ≥10 s | **中位 3.2–3.5 s** |

⇒ **平均每轮省约 4.6 秒。**

### 3.2 Phase 1：PoW 计量（此前是完全的黑洞）

`sent` 事件在 `transport.send()` **之前**发出，而 challenge + PoW 都在 `send()`
**内部** ⇒ `sent` 报的 `gate_wait_ms` / `pre_send_ms` / `local_total_ms`
**三个数一个都不含 PoW**。这段开销此前**从未被任何指标计量过**。

| 文件 | 改动 |
|---|---|
| `engine/gad_engine/pow.py` | `_parallel_search` 新增 `stats` 出参记 `pool_startup_ms`；`solve()` 返回补 `_elapsed_ms` / `_mode` / `_workers` / `_trials`（条件性 `_pool_startup_ms`） |
| `engine/gad_engine/transport/base.py` | `TurnOutcome` **新增** 6 字段：`challenge_ms` / `pow_ms` / `pow_mode` / `pow_workers` / `pow_trials` / `pow_pool_startup_ms`（**不动现有字段**） |
| `engine/gad_engine/transport/deepseek_rpc.py` | 计时 challenge 段并回填上述字段（走 `TurnOutcome`，因为 `emit` 通道只认 delta/thought/meta/done/error） |
| `engine/gad_engine/agent.py` | 新增 `pow_stats` 事件（以 `pow_mode` 作守卫，T1/T4 无 PoW 不报） |
| `shell/DeepSeekAgent.Desktop/AgentHost.cs` | `case "pow_stats":` → Faint 日志 + `GetDouble()` 辅助 |

> **关键约束（已守住）**：`sent` 事件原有的 `gate_wait_ms` / `pre_send_ms` /
> `local_total_ms` **三字段一字未动**（外壳在消费）。

### 3.3 Phase 1.5：门禁 `--powcheck`（零上游请求）

`python -m gad_engine --powcheck`，报告写 `%TEMP%\gad-powcheck.txt`，退出码 0/1：

1. 哈希金标准向量（C# 侧硬判定，**正确性判据**）
2. 单进程求解（答案固定在 nonce=5000，跨分片）
3. 多进程求解
4. **FIRST_COMPLETED 回归守护**（**唯一硬性耗时判据**，阈值 6000 ms）
5. 单核 µs/hash 基准（只报告）
6. 可选 `--full` 跑 `selftest()` 线上真实向量（约半分钟）

**回归守护已验证有效**：刻意把 `FIRST_COMPLETED` 撤掉重跑 →
**10056 ms → FAIL → 退出码 1**；恢复后 **698 ms** 通过。**分离度 14 倍。**

外壳侧：`App.xaml.cs` 新增 `--powcheck` 分支 + `RunPowCheck()`
（并发读两流防互锁、`StandardOutputEncoding=UTF8` 防乱码、180s 有界等待）；
`EngineClient.ResolvePython` 改 `public`。

### 3.4 验证结果

| 项 | 结果 |
|---|---|
| `test_pow.py` | **15 / 15 通过** |
| 引擎全量测试 | **31 / 31 通过**（含此前失败的 `test_tests_exec.py`） |
| 五道门禁 | `--selfcheck` / `--layout-audit` / `--enginecheck` / `--perfcheck` / `--powcheck` **全 EXIT=0** |
| 配置未被污染 | `config.json` md5 `891d7c8ab2cfe0bb27afc1324745121f`、`ui-prefs.json` md5 `06ed3a0e66f1b9f2714ded4f8dc7a18a` **全程未变** |

---

## 四、提交隔离（重要，别踩）

工作区里**同时存在另一条在途工作流**（MCP 工具集扩展），且 `agent.py` 被两条工作流**同时修改**。

本轮的处理方式：

1. `git add` 8 个**干净**文件（`__main__.py` / `pow.py` / `transport/base.py` /
   `transport/deepseek_rpc.py` / `test_pow.py` / `AgentHost.cs` / `App.xaml.cs` / `EngineClient.cs`）
2. 用脚本从 `agent.py` 的 7 个 hunk 中**按内容**（含 `pow_stats`）挑出**唯一属于本轮**的那个
   hunk（`@@ -547,6 +552,33 @@`），生成补丁 `git apply --cached`
3. 于是 `git status` 里 `agent.py` 显示 `MM`：**暂存区 = 本轮的 `pow_stats` hunk；工作区 = MCP 工作流的改动**

**⚠️ 后续注意**：以下文件属于**另一条工作流，不要提交、不要动**：

```
 M docs/plan-20261015-工具集扩展.md
 M engine/gad_engine/agent.py          （除 pow_stats 那块外的其余改动）
 M engine/gad_engine/config.py
 M engine/gad_engine/protocol.py
 M engine/tests/test_tests_exec.py
 M engine/tests/test_tool_wiring.py
?? engine/gad_engine/mcp.py
?? engine/tests/test_mcp.py
?? engine/tests/test_mcp_exec.py
?? engine/tools/real_mcp_check.py
```

（`a953062`「新增 9 个工具」是那条工作流已提交的部分，与本轮无关。）

---

## 五、Phase 1 数据（离线合成基准，**零上游请求**）

因为真实 `pow_stats` 需要「用户重建二进制 + 正常使用」才能落日志，本轮先用**离线合成基准**
替代：构造方式与真实服务端**同分布** —— 随机取 `nonce ∈ [0, difficulty)`，
用 `ds_hash` 反算 target 当 challenge，`solve()` 必须把那个 nonce 找回来。
**全程不碰网络。**

脚本：`%TEMP%\gad_pow_bench3.py`（同进程 + 间隔）、`%TEMP%\gad_pow_bench4.py`（同进程 + join-drain）、
`%TEMP%\gad_pow_clean2.py`（一进程一解）、`%TEMP%\gad_pow_drain_diag.py`（残留 worker 诊断）。

### 5.1 结果（difficulty = 144000，与本机线上一致）

| 指标 | 一进程一解（n=3） | 同进程+间隔 4s（n=5） | 同进程+join-drain（n=6） |
|---|---|---|---|
| **`mode`** | **multi**（8 进程） | **multi** | **multi** |
| `pow_ms` 中位 | 3467 ms | 3553 ms | **3203 ms** |
| `pow_ms` p90 | 6237 ms | 10150 ms | 8615 ms |
| `pow_ms` max | 6929 ms | 11227 ms | 9537 ms |
| `pool_startup_ms` 中位 | 2322 ms | 2401 ms | 2393 ms |
| `trials` 中位 | 45663 | 45663 | 40316 |

**两个关键结论：**

1. **`mode` 恒为 `multi`，从未出现 `degraded`** ⇒ **多进程在本机可用**
   （方案第七节风险 #1 的答案）。若线上出现 `degraded`，才是「单核 25–80 s」量级的另一类问题。
2. **`pow_ms` 中位约 3.2–3.5 s**，与方案推算的 ~3.5 s 吻合。
   `_trials` 中位约 4 万（均匀分布期望 72000 的同一量级），确认答案位置是随机的。

> ⚠️ **样本量只有 3–6**，远未达方案要求的 **n ≥ 100**。这三个数是**量级正确**的，
> 但 p90/max 不可当结论用。**Phase 1 的真实验收（n≥100）尚未完成** —— 见第六节。

### 5.2 基准方法学（踩过的坑，别重犯）

- **同进程连续 `solve()`（无间隔）会被污染**：`solve()` 收尾用
  `executor.shutdown(wait=False)`，上一代 worker 还没死、下一代又 spawn 出 8 个
  → CPU 超订 ⇒ **同一条挑战从 3 s 涨到 15 s**。第一版基准（`gad_pow_bench2.py`）
  就是被这个坑到的。
- **正确做法**：一进程一解（最干净，但 ~13 s/样本），或同进程 + 足够间隔
  （间隔需 ≥ 7.5 s，见下），或同进程 + `multiprocessing.active_children()` 显式 join。

---

## 六、本轮新发现（**重要，尚未落进 `docs/evidence/`**）

### 6.1 残留 worker 会继续烧 ~7.5 秒（每次 PoW 白烧约 60 CPU-秒）

诊断脚本 `%TEMP%\gad_pow_drain_diag.py` 实测：

```
solve 返回：2010.8ms（mode=multi×8，答案在分片 3）
  +  0.0s  存活 8: [7704, 8388, 8864, 12420, 12764, 15804, 22080, 24124]
  ...
  +  7.0s  存活 8: [7704, 8388, 8864, 12420, 12764, 15804, 22080, 24124]
全部 worker 退出耗时（solve 返回后）：7537ms
  （8 个 pid **全部恰好存活 7537ms**，同时死亡）
```

**8 个 worker 在 `solve()` 返回后仍存活 ~7.5 s，且同时退出** —— 这是
「**它们把 `call_queue` 里剩余的分片全部跑完**」的特征：

- `shutdown(cancel_futures=True)` **只能取消还没被推入 `call_queue` 的 work item**；
  36 个分片几乎在提交瞬间就被推入队列，所以**一个都取消不掉**。
- worker 的循环是「从队列取任务 → 跑完 → 再取」，要一直取到 shutdown 哨兵为止。
  剩余 ~28 个分片 × 4096 hash × ~0.35 ms ≈ 39 CPU-秒 ÷ 8 worker ≈ 5 s，
  加上首批，与实测 7.5 s 吻合。

**含义（对 Phase 2 决策很关键）：**

1. 每次 PoW 除了「等答案」的 3.2 s，还会**额外白烧约 60 CPU-秒**（8 进程 × 7.5 s）。
2. 生产里这 7.5 s 与「发 HTTP 请求、等响应」重叠，而引擎此时是 I/O 空闲的，
   所以**对单轮延迟基本无害**；但它会与用户机器上**其它**工作抢 CPU。
3. **这是 Phase 2A（持久进程池）的反面证据**：池若长期存活，上一轮没跑完的
   剩余分片会**带进下一轮**，新任务排在它们后面 ⇒ **反而增加延迟**。
   要消除这个代价，得先让 `_parallel_search` 在命中后能真正**停掉**未完成分片
   （例如把 `_search_chunk` 的分片做小、或用 `cancel_futures` 之外的手段，
   或在命中后主动 `executor.shutdown(wait=False)` 之前清空队列）。

### 6.2 子进程启动会重导入整个 `gad_engine`

spawn 出来的 worker 用的是 venv 的**基础解释器**
（`C:\Users\Admin\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe`），
每个 worker 都会**重新导入 `__main__` 模块**（= `gad_engine/__main__.py` → 整条引擎依赖链）。
这构成 `pool_startup_ms`（中位 2.4 s）里不可忽略的一块 —— 但它**也是生产的真实代价**，
不是基准的假象。做 Phase 2B（内核加速）时若能把单核降到 µs 级、**彻底不需要多进程**，
这一块与 6.1 的浪费会**一起消失** —— 这也是方案里 2B 被标为「收益最高」的原因。

---

## 七、未完成 / 下一步

| # | 项 | 说明 | 优先级 |
|---|---|---|---|
| 1 | **Phase 1 真实数据（n ≥ 100）** | 需用户重建二进制后正常使用；`pow_stats` 会随每轮落日志。要看的分布：`challenge_ms` / `pow_ms` / `mode` / `pool_startup_ms` / `trials`。**尤其确认线上 `mode` 不是 `degraded`** | 高 |
| 2 | **Phase 2 决策（按数据）** | 候选：2A 持久进程池（**已被 6.1 削弱**）、2B PoW 内核加速（**收益最高**，但 `numpy`/`setuptools` 未装、编译器可用性是最大不确定点）、2C 复用已解 PoW（**默认不做**，改请求特征） | 中 |
| 3 | **证据文档** | `docs/evidence/20261016-*.md` 尚未写（本轮已有素材，见第五、六节） | 中 |
| 4 | **记账** | Obsidian Vault 的 `00-Dashboard/Changelog-Stream.md` 与 `01-Projects/20261014-deepseek-agent-desktop.md` **本轮未更新** | 中 |
| 5 | Phase 3 其它本地项 | 同轮多工具并行、grep/glob 换 ripgrep、临时 `.ps1` 复用 —— 收益远小于 PoW，独立排期 | 低 |

### 7.1 收益上限（务必先看这条）

**PoW 即使砍到 0，也只省一轮的约 24%**（14.5 s → ~11 s）。
真正的大头是**上游往返 11–12 s**，而它**不在本次约束可动范围内**
（动它就要改上游请求模式，违反「一次请求都不多加」）。本轮的成果是
**把一段被隐藏的、可优化的本地开销修掉并让它可见**，不是「让整轮快一倍」。

---

## 八、怎么恢复工作

### 8.1 用户侧（必须先做）

用户当前跑的是**修复前**的二进制 —— **必须重建**才能生效：

```bash
cd C:/Users/Admin/Documents/Projects/deepseek-agent-desktop
dotnet build -c Release
```

> 重建前请先关闭正在运行的引擎/外壳进程（旧二进制会占文件锁）。
> 重建后正常用几轮对话，`pow_stats` 就会落进日志，即可开始第 7 节第 1 项的数据采集。

### 8.2 门禁（随时可跑，互不依赖）

```bash
# 引擎侧（零上游请求）
engine/.venv/Scripts/python.exe -m gad_engine --powcheck          # PoW 本地开销
engine/.venv/Scripts/python.exe -m gad_engine --powcheck --full   # 加跑线上真实向量（约半分钟）
engine/.venv/Scripts/python.exe -m gad_engine --selfcheck
engine/.venv/Scripts/python.exe -m gad_engine --enginecheck
# 外壳侧
DeepSeekAgent.Desktop.exe --layout-audit
DeepSeekAgent.Desktop.exe --perfcheck
```

报告落 `%TEMP%\gad-*.txt`；`WinExe` 抓不到 stdout，**只认退出码**。

### 8.3 本轮改动的文件（都在 `e6e0c83` 里）

```
engine/gad_engine/pow.py                  ← 核心（bug 修复 + 计量）
engine/gad_engine/__main__.py             ← --powcheck
engine/gad_engine/transport/base.py       ← TurnOutcome 6 字段
engine/gad_engine/transport/deepseek_rpc.py ← 计时 + 回填
engine/gad_engine/agent.py                ← pow_stats 事件（仅这一个 hunk）
engine/tests/test_pow.py                  ← [7] 计量字段 + FIRST_COMPLETED 回归守护
shell/DeepSeekAgent.Desktop/AgentHost.cs  ← pow_stats 日志 + GetDouble
shell/DeepSeekAgent.Desktop/App.xaml.cs   ← --powcheck 分支 + RunPowCheck
shell/DeepSeekAgent.Desktop/EngineClient.cs ← ResolvePython 改 public
```

### 8.4 临时脚本（在 `%TEMP%`，非交付物，可删）

```
gad_pow_bench2.py        # 同进程无间隔（被污染，反例）
gad_pow_bench3.py        # 同进程 + 间隔（可用）
gad_pow_bench4.py        # 同进程 + join-drain（最干净，含 drain 计时）
gad_pow_clean2.py        # 一进程一解（最干净，但慢）
gad_pow_drain_diag.py    # 残留 worker 诊断（6.1 的证据来源）
gad_pow_bench3_result.json / gad_pow_bench4_result.json / gad_pow_clean_result.json
```

---

## 九、关键环境事实（省得再查）

| 事实 | 值 |
|---|---|
| 引擎 venv | `engine/.venv`，报告 3.13.14；spawn 出的 worker 实际用基础解释器 `~/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe` |
| CPU | `cpu_count = 16`；`solve()` 默认 `workers = min(cpu, 8) = 8` |
| 线上 difficulty | 恒为 **144000**；challenge 带 `expire_after: 300000` |
| PoW 算法 | Keccak-f[1600] 变体，**只跑 round 1..23（跳过第 0 轮）** —— 没有现成库能直接算 |
| 判据 | 32 字节**完全相等**，`nonce ∈ [0, difficulty)` |
| `chunk_size` | 4096 ⇒ 144000 分成 **36 个分片** |
| 已装 / 未装 | `cffi 2.1.1` 已装；**`numpy` 与 `setuptools` 未装** |
| 闸门链顺序 | L1 身份 → L4 熔断 → L2 节奏 → L3 会话轮换 |

---

## 十、给接手人的三句话

1. **`e6e0c83` 是干净的、可交付的**：bug 修复 + 计量 + 门禁，测试与五道门禁全绿，
   用户配置零污染。工作区里**其余改动属于另一条 MCP 工作流，别碰**。
2. **先让用户重建二进制并正常用几轮**，才能拿到 Phase 1 的真实 `pow_stats`
   （尤其是 `mode` 字段）。**在拿到 n ≥ 100 的真实数据之前，不要动手做 Phase 2。**
3. **Phase 2A（持久池）已被 6.1 实质削弱**（残留分片会带进下一轮）；
   **Phase 2B（内核加速）才是收益最高、且能顺带消灭 6.1 与 6.2 两条代价的方向**，
   但它最大的不确定点是**目标机有没有 C 编译器**。
