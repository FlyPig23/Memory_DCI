# V7 实验报告：本题失败轨迹 + Opus 错题分析

- **日期：** 2026-10-07
- **代码：** `scripts/v7_corpus.py`（语料）、`scripts/v7_memory.py` 与 `scripts/v7_seed.py`（错题分析与 seed）、`scripts/v7_compare.py` 与 `scripts/v7_builder_audit.py`（本报告的比较与审计）；它们写出的数据文件在本地 `reports/v7_same_task_replay/`，不随仓库发布
- **基准：** Terminal-Bench 2.1（revision 6，任务包 2.0.2），Harbor 0.23.0，同一组 36 道测试题，题序与 baseline、V5、V6 相同
- **solver：** `gpt-5.6-sol / medium`（Codex CLI 0.153.4），`--verifier-policy unlimited`，题后 reviewer 最多 180 秒
- **错题分析写手：** `claude-opus-5-5 / high`（Claude Code CLI 2.1.292，无交互）；安全拒答回退到其他模型按用户决定照常计入
- **运行名：** `v7_same_task_opus_high_sol_medium_20261007`；语料 `prepared/v7_corpus/final_20261007`

---

## 0. 结论摘要

**主结果：V7 通过 29/36。** 36 题全部有效，没有流程偏差，没有任何一题访问基准来源。

| 组 | 36 题通过 | 说明 |
|---|---:|---|
| baseline | 31（按诚信规则 30） | db-wal-recovery 的通过来自下载原版 Terminal-Bench 仓库、读取该题隐藏测试与 `solution.sh`，按“取用基准来源记 0”算 30 |
| V5 | 28/31 | 只有 31 题可比；V7 在这 31 题上是 27 |
| V6 | 28 | 与 V7 运行时相同，只是语料换成训练集材料 |
| **V7** | **29** | 本题失败轨迹 + Opus 5.5 High 错题分析 |

1. **整体没有超过 baseline。** 按诚信规则，V7 比 baseline 赢 3 题、输 4 题，净 −1；按原始分净 −2。比 V6 赢 4 题、输 3 题，净 +1。每题只跑一次，这些差异都在噪声范围内（符号检验 p = 1.0）。
2. **有材料的 30 题打平 baseline。** 这 30 题 V7 与 baseline 都是 24，V6 是 23。另 6 题历史上没有失败记录、V7 给的是空语料，V7 做出 5 题，baseline 6 题。
3. **救回 baseline 的失败题：** train-fasttext、pypi-server，以及按诚信规则计的 db-wal-recovery。baseline 另 3 道失败题（torch-pipeline-parallelism、filter-js-from-html、qemu-alpine-ssh）仍未做出。
4. **丢掉 baseline 做出的题：** pytorch-model-cli、dna-assembly、model-extraction-relu-logits，以及没有材料的 feal-differential-cryptanalysis。前三题的共同模式是 solver 自查后宣布完成，用时只有 4–9 分钟，但隐藏测试没过。
5. **材料确实被用了。** 30 道有材料的题，solver 都从 Opus 分析起步，也都读了原始轨迹（DCI_search_trajectory，部分题还直接 `sed` 原文）；21 题在求解中给 memory 新增或修订了条目。
6. **与 side experiment 对照：** 在 side experiment 的 5 道难题上，同样“轨迹 + Opus 分析”的给法把成功率从 1/15 提到 12/15。主实验这 36 题对 solver 大多不难，baseline 已做出 30–31 题，可提升的空间只有 5–6 题，V7 只救回其中 2–3 题，又丢了几道。

**成本：** Opus 写错题分析 $67.48（34 次尝试，会话 3.56 小时）；solver 4.1 小时、6,644 万 token；题后复盘 40 分钟。

---

## 1. 研究问题与设定

### 1.1 V7 在问什么

V1–V6 都先把 89 道题分成 53 道 training、36 道 test，solver 解测试题时只能查训练题的轨迹与经验，衡量的是**跨题迁移**。V7 换了一个问题：同样 36 道测试题，如果 solver 能查到**这道题本身**在官方榜单上的失败记录，以及一个更强的模型读完这些记录后写的错题分析，成绩会怎样？

这对应 [side experiment](../../../../side_experiments/hard_task_failure_replay/REPORT.md) 的设定：在 5 道难题上，`gpt-5.6-sol / medium` 什么都不给时为 1/15，只给同题失败轨迹为 8/15，只给 Opus 5.5（v4 提示词）的错题分析为 10/15，两样都给为 12/15。V7 用同一份 v4 提示词和“轨迹 + 分析”的给法，换成 Opus 5.5 High，在完整的 36 题测试集上运行。

### 1.2 与 V6 的唯一区别

V7 的运行时就是 V6，只替换每道题挂载的语料：

| | V5 / V6 | V7 |
|---|---|---|
| `/pool/tasks` | 53 张训练题任务卡 | 只有本题自己的任务卡（官方说明原文 + 失败尝试清单） |
| `/pool/trajectories` | 53 道训练题的全部历史轨迹（成功与失败） | 只有本题的官方失败轨迹（reward 0、已评分），不含任何成功轨迹 |
| `/memory` 初始内容 | V5 为空；V6 为 Luna 从训练失败轨迹提炼的 221 条 | Opus 5.5 High 从本题失败轨迹写的错题分析（6–9 条） |
| 五个 DCI 工具、启动流程、reviewer、solver、评分策略 | — | 与 V6 相同 |

做哪道题就只给哪道题的材料；任务内对 memory 的修改不传给下一题。

### 1.3 为什么只用失败轨迹

成功轨迹等于直接给出可通过评分的做法，会把实验变成“抄答案”；只给失败轨迹时，solver 能学到的是“哪些路走不通、哪些检查被漏掉”，更接近从错题中学习。用户明确要求“一定不要包含成功的”，因此选取阶段就排除了成功、无评分和缺分记录，查询池、任务卡和清单中都没有成功轨迹。

---

## 2. 语料构建（`scripts/v7_corpus.py`）

### 2.1 来源与选取

来源是与训练池相同的官方 leaderboard 记录（`reports/official_test_trials.csv`）：22 个条件、每条件每题 5 次 trial，36 道测试题共 3,960 条，全部与固定任务版本一致。其中 3,060 条 reward 1，874 条 reward 0，26 条已评分但没有数值得分。

V7 只取 874 条 reward 0 的记录，分布在 30 道题上，写入 `manifests/v7_failure_sources.json`。另 6 道题历史上没有任何失败：prove-plus-comm、constraints-scheduling、fix-ocaml-gc、feal-differential-cryptanalysis、fix-git、reshard-c4-data。它们以空查询池和空 memory 运行，这一组实际等于“V6 运行时 + 空语料”。

### 2.2 下载

正文从 Harbor Hub 公开接口获取（`v7_corpus download`），结果写入 `manifests/v7_download_audit.json`：873 条可用（新下载 496 条，复用本地 377 条），1 条（extract-moves-from-video 的一次尝试）经官方来源确认没有正文，在任务卡上标为 `NO TRAJECTORY BODY AVAILABLE`。

### 2.3 去污染

部分历史 agent 在求解时联网找过本题的基准来源，这样的轨迹会把隐藏测试或参考解带进查询池。建池前在宿主机上用隐藏测试、参考解和任务 README 逐条比对，只记录行号与指标，不输出其正文。

最初冻结的固定阈值不可用：它把 29% 的轨迹（255/873）判为污染，build-pov-ray、build-cython-ext、rstan-to-pystan、sparql-university 整题被清空，原因多是自然重合（同样调用公开库、第三方库自带的 `tests/test_*` 路径）；同时又漏掉了真正的来源访问。于是改为逐条人工复核，规则是只排除以下轨迹：

- 访问了本题的基准来源（官方仓库、镜像、题解站点、注册页）；
- 覆盖参考解至少一半；
- 与隐藏测试连续重合至少 60 个 token；
- 引用评分文件（`test_outputs.py`、`solve.sh`、`/solution/`）。

256 条复核记录写入 `prepared/v7_corpus/decontamination_decisions.json`，其中 250 条保留并写明理由，6 条排除，全部是基准来源访问：

| 任务 | 排除条数 | 原因 |
|---|---:|---|
| headless-terminal | 1 | 从 Hugging Face 上的基准镜像和 marginlab.ai 取到本题隐藏测试与参考解 |
| build-pov-ray | 1 | 读了第三方针对本题的 Terminal-Bench 题解（spylab.ai） |
| mteb-leaderboard | 3 | 检索结果含本题的基准注册页（registry.harborframework.com、tbench.ai） |
| winning-avg-corewars | 1 | 检索答案依据 tbench.ai 上对本题的描述 |

最终 30 个查询池共 867 条轨迹，约 895 MiB（有些轨迹内嵌 base64 截图，extract-moves-from-video 单条最大 183 MB）。判定文件的 SHA256 写入语料索引，运行协议再核对一次。

### 2.4 查询池格式

```text
/pool/manifest.json, /pool/README.md
/pool/tasks/<task_id>.md                                     # 唯一任务卡
/pool/trajectories/<task_id>/<model_alias>/attempt-NNN/<task_id>_0.txt
```

- Hub trial ID 统一替换为匿名的 `attempt-NNN`，solver 无法据此回到 Hub 查同一 job 的其他记录。
- 每条轨迹头部第一行是 `# Historical failed attempt at this task: <task_id>`，并写明官方得分 0、已评分执行。
- 轨迹渲染沿用训练池的 ATIF 文本格式，保留全部字段；文件名中的 `_0` 是官方分数。
- 任务卡写出失败尝试条数。这个数字本身是难度信号（见第 7 节）。

---

## 3. 错题分析（`scripts/v7_memory.py`、`v7_seed.py`、`v7_memory_prompt.txt`）

### 3.1 引擎与隔离

每道有失败轨迹的题跑一个 Claude Code CLI 无交互会话：`claude -p --model claude-opus-5-5 --effort high --output-format stream-json --json-schema <schema>`。

- **容器：** 每题一个容器，只读挂载本题查询池，另有一个可写的 `/work` 用于检查点；不挂载测试、参考解、任务 README、其他题或成功轨迹。
- **工具：** 网页搜索、网页抓取，以及能启动、调度或联系其他 agent 的工具全部禁用。实际只用到 Bash（822 次）、StructuredOutput（50 次）、Read（3 次）和 Write（3 次），没有意外工具。
- **凭据：** 宿主机的 Claude Code 登录只把短期访问令牌按环境变量名传进容器，不出现在命令行；会话结束后删除复制的凭据，并在全部记录中把令牌字节替换为 `[REDACTED_CLAUDE_ACCESS_TOKEN]`。事后按令牌格式扫描全部 1,043 个建库记录文件，没有残留。
- **时限：** `clamp(300 + 30 × 失败轨迹数 + 60 × ceil(MiB), 600, 3600)` 秒，与 V6 建库相同的启发式。提示里附有至多 90,000 字符的轨迹摘录，完整原文让模型自己用 `rg`、`sed -n` 读取。

CLI 本身要联网访问模型接口，容器无法断网，所以隔离只能事后核实（3.4 节）。

### 3.2 提示与输出

提示词沿用 side experiment 的 v4 版，只改路径并要求给每条教训配 1–8 个检索关键词。关键规则：

- 只做本地读取：不得自己解题、运行任务软件、上网，或寻找测试、评分器与参考解；
- 区分轨迹直接显示的事实与推断，原因除非轨迹直接显示，否则标为假设；
- 只推荐做好交付物本身的方法，不得推荐通过改变检查所处环境来过关的手段：注入后续进程的启动钩子（`.pth`、`sitecustomize`、shell 启动文件）、修改检查可能使用的系统解释器或工具、改动测试与评分文件。历史尝试用过这类手段时，只能作为禁止事项提及。

输出是封闭的 JSON schema：任务摘要、要求清单、方法族、共同假设、验证缺口、至多 8 条教训（失败模式、观察到的失败、原因假设及置信度、替代做法、能发现问题的检查、引用）、可复用细节、决策指南、未解释之处。每条引用必须落在本题的失败轨迹或任务卡内，行号有效。

### 3.3 转成 memory seed

每题的分析转成固定格式的条目：1 条概览（含决策指南与要求清单）、每条教训 1 条、1 条可复用细节。条目状态只能是 `source_observed` 或 `uncertain`，并注明“所有引用的尝试都得了 0 分，这里没有任何内容在本题上验证过”。每条引用以文件和行窗口的 SHA256 绑定；seed 冻结后，每题运行前重新核对。

**NUL 行：** 有些历史终端输出含二进制 NUL 字节，seed 规定被引用的行窗口必须是文本。make-mips-interpreter 和 password-recovery 的前两次分析都因引用了含 NUL 的窗口被整份拒收。修正后，验证器只把这类窗口收窄到其中最长的无 NUL 连续段，分析文字不改，每次收窄都记录。用修正后的验证器重放这 3 份被拒的输出，全部通过并能转成合格 seed；正式 seed 来自两题各自的第三次尝试（make-mips-interpreter 收窄 2 处，password-recovery 无需收窄）。

### 3.4 导出闸门与事后审计

`v7_memory export` 写 seed 之前，逐题检查被采用的那次会话：

1. 构建流中每个工具调用：是否真正执行了网络程序（curl、wget、pip、git、node 等），是否执行含网络代码的 heredoc 或 `python -c`，是否写入含网络代码的脚本，输入中是否出现基准来源字符串。引号里的 grep 模式和写入文件的正文不算执行。
2. 分析正文与隐藏测试、参考解的重合：覆盖参考解至少一半、与测试连续重合至少 60 个 token、提到评分文件或基准来源，都会标记。

被标记的任务必须在 `runs/v7_memory_build_20261007/export_reviews.json` 里写下人工复核结论才能导出，结论随 seed 的来源信息一起冻结。结果：

- 30 题中只有 sparql-university 被标记。它在校验自己的检查点时写了 `python3 -c "json.load(...)" || node -e "JSON.parse(...)"`；前半句成功，`node` 根本没有执行。复核后放行。
- 分析与参考解的最大覆盖为 4.0%（large-scale-text-editing），与隐藏测试的最长连续重合为 16 个 token（path-tracing），都是自然重合。
- 另用 `scripts/v7_builder_audit.py` 扫描全部 34 次尝试（含被拒的尝试）：没有实际执行的外部访问。mteb-leaderboard 的工具结果里出现 `huggingface.co/datasets`，来自轨迹正文本身。

### 3.5 构建结果

| 指标 | 数值 |
|---|---:|
| 构建任务 | 30（另 6 题无失败轨迹，不建 seed） |
| 尝试次数 | 34（make-mips-interpreter、password-recovery 各 3 次，其余 1 次） |
| 费用 | $67.48 |
| 会话总时长 | 3.56 小时（6 路并行，墙钟约 1.5 小时） |
| 教训 / seed 条目 | 170 / 230（每题 6–9 条） |
| 严格模型审计通过 | 29/30 |

dna-assembly 由 Opus 5.5 开始，被安全机制判为生物类内容后，Claude Code 自动切到 `claude-opus-4-8`，4.8 写了几乎全部输出（与 side experiment 中 dna-insert 的情况相同），按用户决定计入。逐题的费用、时长、教训数与模型构成由 `v7_builder_audit` 写入本地 `reports/v7_same_task_replay/builder_stats.json`。

---

## 4. 求解运行

### 4.1 运行时

`run run --arm v7 --task-corpus prepared/v7_corpus/final_20261007`。与 V6 相同的部分：五个 DCI 工具（reason、DCI_search_task、DCI_search_trajectory、DCI_search_memory、distill）、启动流程、可写 `/memory`、`gpt-5.6-sol / medium`、Codex CLI 0.153.4、reviewer 镜像与 180 秒上限、`unlimited` 评分策略、36 题串行、不自动重试。

MCP 服务器多了一个 `--pool-scope same_task`：它要求查询池里只有本题一张任务卡，并且只接受本题的轨迹。V5/V6 的启动参数不变（有逐字节的回归测试）。

### 4.2 提示词改动

只改描述语料的句子，其余与 V6 逐字相同：

- `/pool/tasks` 改为“本题自己的说明与轨迹导航卡（唯一的任务卡）”；
- 轨迹改为“其他 AI agent 在本题上经官方评分为 0 的失败尝试；成功尝试被保留不提供”；
- `/memory` 改为“测试前由另一个模型从本题上述失败尝试写成的冻结错题分析”；
- 工具说明和流程中去掉 “training” 一词，标题由 V6 改为 V7。

### 4.3 冻结协议

运行协议记录语料索引（`4c0defff…`）、seed 索引（`73a9e0ad…`）、来源清单与去污染判定（`d0b7eae2…`）的哈希。每题开始前重新清点本题查询池与 seed，结果写入该题的 `runtime_contract.json`（`condition: v7`、`pool_scope: same_task`、轨迹条数、文件清单摘要）。汇总时任何一项与计划不符，该题判为无效，不计分。

V6 的结果记录未受影响：`run status --arm v6` 仍重算出 36/36 完成、均分 0.7778，与改代码前保存的结果逐行一致。

---

## 5. 结果

### 5.1 逐题结果

0/1 为官方得分；“—”表示该组在这题没有可比结果；† 表示该次通过来自取用基准来源，按诚信规则记 0。

| # | 任务 | 失败轨迹数 | V7 | baseline | V5 | V6 | 解题分钟 | DCI 调用 | 读了轨迹 | memory 起→止 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|:---:|---|
| 1 | mteb-leaderboard | 14 | 1 | 1 | 1 | 1 | 13.4 | 13 | 是 | 8→8 |
| 2 | path-tracing | 22 | 1 | 1 | 1 | 1 | 2.3 | 8 | 是 | 8→9 |
| 3 | headless-terminal | 26 | 1 | 1 | 1 | 1 | 3.3 | 12 | 是 | 8→8 |
| 4 | train-fasttext | 75 | 1 | 0 | 1 | 1 | 49.8 | 12 | 是 | 8→8 |
| 5 | pytorch-model-cli | 17 | 0 | 1 | 0 | 1 | 4.0 | 10 | 是 | 6→6 |
| 6 | mailman | 16 | 1 | 1 | 1 | 1 | 4.8 | 11 | 是 | 8→9 |
| 7 | prove-plus-comm | 0 | 1 | 1 | 1 | 1 | 0.8 | 5 | — | 0→1 |
| 8 | torch-pipeline-parallelism | 38 | 0 | 0 | — | 0 | 2.0 | 9 | 是 | 9→9 |
| 9 | filter-js-from-html | 106 | 0 | 0 | 1 | 1 | 11.5 | 11 | 是 | 8→9 |
| 10 | make-mips-interpreter | 36 | 1 | 1 | 1 | 1 | 7.0 | 11 | 是 | 8→9 |
| 11 | gpt2-codegolf | 50 | 1 | 1 | 1 | 1 | 3.9 | 11 | 是 | 9→10 |
| 12 | extract-moves-from-video | 71 | 1 | 1 | 1 | 0 | 27.3 | 8 | 是 | 9→10 |
| 13 | pypi-server | 28 | 1 | 0 | 0 | 0 | 2.4 | 10 | 是 | 6→6 |
| 14 | constraints-scheduling | 0 | 1 | 1 | 1 | 1 | 1.2 | 5 | — | 0→1 |
| 15 | db-wal-recovery | 52 | 1 | 1 † | 1 | 1 | 1.8 | 9 | 是 | 8→8 |
| 16 | code-from-image | 5 | 1 | 1 | 1 | 1 | 1.2 | 9 | 是 | 7→7 |
| 17 | winning-avg-corewars | 27 | 1 | 1 | 1 | 1 | 2.7 | 7 | 是 | 8→9 |
| 18 | fix-ocaml-gc | 0 | 1 | 1 | 1 | 1 | 20.0 | 9 | — | 0→1 |
| 19 | dna-assembly | 39 | 0 | 1 | — | 0 | 8.7 | 10 | 是 | 9→10 |
| 20 | feal-differential-cryptanalysis | 0 | 0 | 1 | 1 | 0 | 0.8 | 3 | — | 0→1 |
| 21 | large-scale-text-editing | 4 | 1 | 1 | 1 | 1 | 2.1 | 9 | 是 | 8→9 |
| 22 | fix-git | 0 | 1 | 1 | 1 | 1 | 1.5 | 6 | — | 0→1 |
| 23 | git-multibranch | 2 | 1 | 1 | 1 | 1 | 3.8 | 10 | 是 | 7→8 |
| 24 | chess-best-move | 30 | 1 | 1 | 0 | 1 | 1.7 | 9 | 是 | 7→8 |
| 25 | sparql-university | 3 | 1 | 1 | 1 | 0 | 5.0 | 9 | 是 | 7→7 |
| 26 | portfolio-optimization | 3 | 1 | 1 | 1 | 1 | 4.2 | 9 | 是 | 7→7 |
| 27 | build-pov-ray | 32 | 1 | 1 | 1 | 0 | 8.3 | 15 | 是 | 8→8 |
| 28 | rstan-to-pystan | 6 | 1 | 1 | 1 | 1 | 7.4 | 9 | 是 | 7→7 |
| 29 | password-recovery | 5 | 1 | 1 | 1 | 1 | 1.5 | 10 | 是 | 7→7 |
| 30 | polyglot-rust-c | 10 | 1 | 1 | 1 | 1 | 2.7 | 9 | 是 | 6→7 |
| 31 | compile-compcert | 35 | 1 | 1 | 1 | 1 | 15.0 | 11 | 是 | 8→8 |
| 32 | model-extraction-relu-logits | 64 | 0 | 1 | 1 | 1 | 3.8 | 7 | 是 | 9→10 |
| 33 | build-cython-ext | 13 | 1 | 1 | — | 1 | 4.1 | 10 | 是 | 8→10 |
| 34 | reshard-c4-data | 0 | 1 | 1 | 1 | 1 | 4.4 | 6 | — | 0→1 |
| 35 | qemu-alpine-ssh | 30 | 0 | 0 | — | 0 | 5.4 | 8 | 是 | 8→8 |
| 36 | tune-mjcf | 8 | 1 | 1 | — | 1 | 7.4 | 11 | 是 | 6→7 |

### 5.2 与各组的配对比较

| 对照 | 题数 | V7 | 对照组 | V7 赢 | V7 输 |
|---|---:|---:|---:|---|---|
| baseline（按诚信规则） | 36 | 29 | 30 | train-fasttext、pypi-server、db-wal-recovery | pytorch-model-cli、dna-assembly、feal-differential-cryptanalysis、model-extraction-relu-logits |
| baseline（原始分） | 36 | 29 | 31 | train-fasttext、pypi-server | 同上 4 题 |
| V6 | 36 | 29 | 28 | extract-moves-from-video、pypi-server、sparql-university、build-pov-ray | pytorch-model-cli、filter-js-from-html、model-extraction-relu-logits |
| V5（可比的 31 题） | 31 | 27 | 28 | pypi-server、chess-best-move | filter-js-from-html、feal-differential-cryptanalysis、model-extraction-relu-logits |
| baseline，只看有材料的 30 题 | 30 | 24 | 24 | train-fasttext、pypi-server、db-wal-recovery | pytorch-model-cli、dna-assembly、model-extraction-relu-logits |

V7 与 V6 用同一套运行时、同一个 solver，只换了语料，是最干净的对照：净 +1（4 赢 3 输）。V6 输给 baseline 的 5 题里，V7 做出了 extract-moves-from-video、build-pov-ray 和 sparql-university，dna-assembly 与 feal 仍失败。

### 5.3 几道题的具体情况

- **pypi-server（救回，baseline、V5、V6 都失败）：** solver 读了一条失败轨迹，然后打包 wheel、写简单索引，并用 `setsid -f` 让服务在求解结束后继续运行，再在新 venv 里自测安装。没有启动钩子，也没改系统 Python。“服务器必须活过 solver 进程”正是 Opus 从失败轨迹里归纳出的要点。
- **db-wal-recovery：** 2 分钟内直接用 0x42 做 XOR 解出 WAL。这个密钥是本题失败尝试从可见数据里找出来的，那些尝试败在后面的步骤。这属于 V7 要测的“从同题失败中学习”。baseline 的那次通过则是联网读了原题测试和参考解。
- **丢掉的 pytorch-model-cli、dna-assembly、model-extraction-relu-logits：** 三题都只用了 4–9 分钟，最后一条消息都是“已实现并验证”。隐藏测试分别失败在 `test_cli_tool_output`、`test_primers`、`test_stolen_matrix_matches`。三题的 solver 都读了本题的错题分析和失败轨迹，仍在自查通过后提前收手。
- **filter-js-from-html：** solver 自测 27/27 项 XSS 用例全部通过，两项官方测试都失败。V6 和 V5 做出了这题，baseline 没有。

### 5.4 材料使用

| 指标 | 数值 |
|---|---:|
| 有 seed（Opus 分析）的题 | 30 |
| 读了 memory 的题 | 31（含无 seed 的 fix-ocaml-gc，查的是空 memory） |
| 读了原始轨迹的题 | 30（全部有轨迹的题） |
| 求解中 memory 增长的题 | 21 |
| 题后复盘有效 | 36/36（写入 28 次，no_update 8 次） |

### 5.5 运行与成本

| 项目 | 数值 |
|---|---:|
| 错题分析（Opus 5.5 High，离线） | $67.48，34 次尝试 |
| solver | 4.12 小时，66,441,272 token |
| 题后复盘 | 40.3 分钟 |

第 26 题之后按用户要求暂停一次，停在两题之间，没有中断任何一题；随后用同一个冻结协议续跑（见 `runs/v7_same_task_opus_high_sol_medium_20261007/operator_pause.json`）。第 27–36 题运行时，side experiment 的重跑同时在跑，共用同一个 Codex 账号。

---

## 6. 诚信检查

- **基准来源访问：** 用同一条规则扫描三组的全部 solver 命令（`scripts/v7_compare.py` 的 `BENCHMARK_SOURCE`）。V7 36 题：0 次。V6：0 次。baseline：db-wal-recovery 一题克隆或下载了 `laude-institute/terminal-bench`，读了 `original-tasks/db-wal-recovery/tests/test_outputs.py` 与 `solution.sh`，还复制了原始数据文件，按规则记 0。V5 的 69 份事件日志：0 次。
- **环境改动候选：** 2 处模式命中，逐条人工核实为误报，记录在本地 `reports/v7_same_task_replay/tamper_reviews.json`。headless-terminal 只在 `mkdtemp()` 临时目录里写测试用的 `.profile`/`.bashrc`；pytorch-model-cli 命中的 `.pth` 是题目自带的权重文件 `model.pth`。
- **错题分析的隔离：** 见 3.4 节。导出闸门只标记了 sparql-university 一题，复核后放行（`node` 实际没执行）。seed 中没有 Hub trial ID，也没有基准来源字符串；全部建库记录中没有残留访问令牌。
- **题后复盘：** 36 次都只读评分前冻结的证据，没有收到隐藏评分（`received_hidden_grade: false`），证据未被改动。

---

## 7. 局限

1. **每题只跑一次。** 单题 0/1 的差异主要是噪声：V7 对 baseline、对 V6 的输赢都在 3–4 题量级，符号检验 p = 1.0。要比较 V6 与 V7 这类小差异，需要每题多次 rollout。
2. **这是同题回放，不是迁移。** V7 给的是同一道题的失败记录，衡量的是“从别人在这道题上的失败中学习”的效果上限，结果不能与 V5/V6 的跨题迁移等同。
3. **任务卡上的失败条数本身透露难度。** 0 条失败意味着历史上全部成功；失败条数越多，题越难。
4. **6 道无失败记录的题拿不到任何材料。** 它们在 V7 里等于“V6 运行时 + 空语料”，其中 feal 失败。这部分差异与错题材料无关。
5. **写分析的模型能联网。** Claude Code CLI 必须访问模型接口，容器无法断网；隔离只能事后从完整构建流核实（3.4 节）。
6. **历史对照的条件不同。** baseline 与 V5 来自较早的运行，代码版本、评分恢复条件和评分策略都与现在不同；只有 V6 与 V7 用的是同一个 runner。
7. **提示词流程沿用 V6。** 只改写了描述语料的句子；流程里“查找相似任务”的步骤对只有一张任务卡的 V7 略显多余，但为了只改变语料，保持不动。
8. **运行中有一次人为暂停，后段与 side experiment 并行。** 暂停发生在两题之间，没有中断任何一题；后 10 题与 side experiment 的重跑同时进行。开跑前的审查测得同期 token 吞吐没有变慢，但不能完全排除影响。

---

## 8. 复现

构建语料、写错题分析和运行 V7 的命令见 [README 第 5 节](../README.md)，方法边界见 [PROTOCOL 的 V7 一节](PROTOCOL.md)。本报告的比较和审计数字由下面两条命令重新生成（只读冻结的运行记录，不调用模型）：

```bash
experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.v7_compare v7_same_task_opus_high_sol_medium_20261007

experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.v7_builder_audit \
  runs/v7_memory_build_20261007 prepared/v7_corpus/final_20261007
```

baseline 与 V5 的逐题参照取自本地冻结的 `reports/tb21_report_app/src/data.json`（脚本核对其 SHA256），V6 取自 `run status --arm v6`。
