# WildClawBench 小规模实验计划 v2

日期：2026-09-08
状态：首轮已完成，正式结果见中文摘要（历史文件：`experiment/benchmarks/wildclaw_bench/reports/formal/summary_zh.md`，不在精简发布中）。下文保留首轮设计记录；文末“检索改进讨论”仅为后续提案，不是新一轮执行清单。

## 1. 首轮要回答的问题

在同一执行模型、相同任务环境和预先固定的预算下，让 agent 通过 DCI 交互式查询其他任务的历史轨迹，能否提高 WildClawBench 未参与建库任务的得分？

首轮目标为 **36 个构建任务 + 24 个测试任务、一个模型、两个条件、每题每条件一次**，预计 48 次正式求解。这里的“未见”指测试任务及其轨迹未进入建库和调参；正式求解时只开放当前测试题的合法输入，不声称基础模型预训练从未见过公开题目。若近重复整组约束使最终配额必须调整，正式求解次数相应为 **2 × 冻结测试任务数**，并在运行前更新全部分母。

研究对象固定为只读的历史轨迹查询库。任务内允许保存私有查询记录和证据笔记，每次运行后重置。handbook 蒸馏、WikiSkill 演化、跨测试任务写回、DR-DCI 扩库和第二档执行模型留作后续实验。这是对[最新研究笔记](../../../../docs/research_notes/findings.md)中首个端到端 pilot 的具体化，不是直接启动旧报告的完整多臂研究。

## 2. 数据下载与完整性

### 2.1 下载范围

锁定下载时的 GitHub commit 和两个 Hugging Face dataset revision，再按该快照下载：

- [官方代码仓库](https://github.com/InternLM/WildClawBench)：全部 60 个正式 task 定义、解析器、环境准备代码、通用 runner 与评分代码；模板不计入 60 题。
- [任务数据仓库](https://huggingface.co/datasets/internlm/WildClawBench)：60 题相关 workspace 输入与评测资源，分区存放。外部视频、代码归档及模型权重按依赖清单补齐，逐项记录来源和 hash。
- [轨迹仓库](https://huggingface.co/datasets/internlm/WildClawBench-Trajectories)：固定快照实际公开的全部模型轨迹及原始输出。清单覆盖 parquet、sessions，以及所有 output 原始包，兼容 tar.gz、zip 等实际发布格式。
- 镜像只准备选定执行路线需要的版本，不因下载全部任务而下载所有 harness 的镜像。

“全部”以冻结快照的实际文件 inventory 为准，不等于排行榜全部模型，也不假定模型 × 任务矩阵没有缺项。导入完成后输出模型名、模型目录别名、task ID、尝试编号、轨迹数、评分可用性和缺失项的覆盖表；多个格式表示同一次执行时做来源关联，不当作多次独立尝试。

当前官方页面的 compact parquet 显示 720 行/12 模型，而 sessions 列表与原始包覆盖并不完全一致，因此不能把 parquet 行数或 sessions 目录作为唯一下载清单。parquet 的图片是 hash 占位符；完整图片、score、usage 和产物需要关联原始文件。[数据说明](https://huggingface.co/datasets/internlm/WildClawBench-Trajectories)、[文件目录](https://huggingface.co/datasets/internlm/WildClawBench-Trajectories/tree/main)

### 2.2 原始归档和可检索材料

原始文件不可变归档，记录仓库、revision、相对路径、SHA-256、task ID、来源模型/尝试、trace status、outcome 来源与验证状态。

下载顺序调整为“任务元数据与来源 inventory → 冻结任务划分 → 全量轨迹归档与分区导入 → 构建检索视图”。下载所有轨迹不代表把所有轨迹开放给 solver：

- 36 个构建任务的轨迹进入建库流程，保留成功、失败、中断和未知状态，不按模型成绩挑选。
- 24 个测试任务的公开轨迹、历史成绩和产物进入封存区；正式评测完成前不参与内容分析、调参、索引、提示词或人工选题。
- 已有历史资料里的 revision 是候选记录，不自动视为本次冻结版本；最终版本写入新 manifest。

可检索视图是轨迹的结构规范化文本，不进行 LLM 语义蒸馏：保存动作、观察、模型输出和可追溯的图片引用，处理真实凭据、控制字符和评测专用载荷。原始 task Markdown 的评分段、gt/private_eval、评分代码不进入查询库。历史命令只作为证据文本，不自动回放。

本轮允许保留合法构建题的 agent 解答过程与自生成内容，研究结论表述为“历史经验辅助的端到端迁移”。它不是旧 SWE 设计的 patchless 纯过程迁移验证；若要排除最终答案/产物复用，需要后续另加有明确定义的消融。测试题及近重复题的答案始终不可进入查询库。

## 3. 类别内 6:4 划分

沿用官方六类，不重新做语义分类。各类规模不完全相同，采用最大余数法使总数为 36/24；余数相同按官方类别编号升序分配。

| 官方类别 | 总任务数 | 构建集 | 测试集 |
|---|---:|---:|---:|
| Productivity Flow | 10 | 6 | 4 |
| Code Intelligence | 12 | 7 | 5 |
| Social Interaction | 6 | 4 | 2 |
| Search & Retrieval | 11 | 7 | 4 |
| Creative Synthesis | 11 | 6 | 5 |
| Safety Alignment | 10 | 6 | 4 |
| **合计** | **60** | **36** | **24** |

类别规模来自[官方任务说明](https://github.com/InternLM/WildClawBench#tasks)。以上配额已满足近重复整组隔离，具体清单及 hash 见 `manifests/split.json` 和 `manifests/split_audit.md`。

划分规则：

1. 固定 split seed 为 `20260908`，将算法、排序方式与输出 hash 一起保存。不得反复换 seed 挑选更有利的划分。
2. 同一 task 的所有模型、尝试、成功和失败轨迹必须同侧。
3. 在加工轨迹和查看成绩前，根据 task prompt、公开输入、资源 hash、模板与解法重合风险检查近重复 family。共享资源不自动等于同一道题，但必须记录；高度近重复、同模板换参数的任务整组划分。
4. 官方准备脚本显示部分题共享视频、SAM3 或代码仓库，因此六大类别不能代替近重复检查。优先满足整组隔离，并在可行范围内保持总 36/24 和表中比例。若分组约束不能同时满足配额，保留所有 60 题、采用最接近比例的整组划分并记录偏差，不为凑比例拆组。[准备脚本](https://github.com/InternLM/WildClawBench/blob/main/script/prepare.sh)
5. 类别仅用于分层划分和分组报告。DCI 默认可以查询全部构建集，不强制仅查询当前题类别；两实验条件使用相同公开任务信息。
6. 调试仅发生在构建集内部：默认每类暂留 1 题共 6 题，其余约 30 题建临时库。近重复 family 必须一起排除，实际临时库数可随之减少。调试完成并冻结配置后，用全部构建任务重建最终库，正式测试阶段不再调参。

开发验证中若出现有证据支持的输入准备、容器或评分基础设施故障，先保留原运行并修复故障。能使用冻结产物恢复评分时，不再次求解；原状态不可恢复时，可在开发集为该题创建新的、有修复原因和旧运行引用的验证记录。该选择单独存入 `manifests/development_run_selection.json`，不能用来替换已有有效评分的低分。正式 48 次求解不采用这种开发重跑规则，也不根据测试结果调整方法。

## 4. 两个正式条件

| 条件 | 历史轨迹权限 | 实际执行 |
|---|---|---|
| A0：无历史轨迹库 | 不挂载构建库 | 使用当前题的输入和正常任务工具解题 |
| A3：原始轨迹 DCI | 构建库只读 | 理解当前题后、实质解题前至少进行一次相关关键词检索；有候选则读取至少一个局部窗口并判断适用性，此后自主决定继续检索、采用或拒绝证据 |

两组使用相同模型、reasoning 设置、任务工具、官方任务所需 skills/warmup、镜像、公开输入、尝试次数及总执行时间上限；A0 不是移除 benchmark 原本所需的工具或 skills。DCI 查询、阅读和由此产生的推理时间全部计入该组成本。

本次比较回答“增加历史库的 DCI 系统有没有帮助”。它不能单独证明交互检索优于一次性检索。若后续要做这种机制归因，再增加候选证据和计算预算匹配的静态检索条件；首轮不强制增加第三组。

构建集工程调试发现旧 optional 提示允许 A3 完全不调用查询工具，因此 v2 增加上述最小启动检索，使处理条件可观察。无相关命中可以退出检索，服务不可用必须记录，不强迫采用历史建议或凑多轮次数。这是本项目的机制适配：固定上游 DCI-Lite 的默认 L3 提示强烈引导查本地 corpus，但没有最低调用数硬约束；其 IR 模式另有多轮检索提示。此次调整依据工具使用合规性，不依据分数；旧 optional 开发运行完整保留。正式阶段不会因少查、未查或低分而补做挑优 rollout，主结果仍包括全部分配到 A3 的任务并另报合规率。

每题每组一次，正式运行顺序以固定 seed 生成，交错/平衡两组先后顺序；每次使用独立新容器和新会话。API 不支持确定性 seed 时，记录为不支持，不能把调度 seed 写成模型生成 seed。

## 5. Python、Codex 和 DCI 执行方式

### 5.1 已核实的本机条件

本次只读检查结果：

- `codex login status` 确认使用 ChatGPT 登录。
- CLI 版本为 `0.153.4`。
- 本机配置为 `model=gpt-6-astra`、`model_provider=openai`、`model_reasoning_effort=ultra`。
- Docker daemon 可访问，版本为 `29.1.3`。

首轮固定上述模型和 reasoning 设置。工程阶段已在隔离容器内验证真实文字、图片和 DCI 调用；现有登录文件以不输出内容的方式复制到本项目私有运行目录，并为每次 Codex 会话提供独立副本。源登录文件保持不变。

### 5.2 认证与执行后端

Python 负责数据、划分、容器生命周期、调度、超时、日志、评分和汇总；Codex CLI 负责一次任务内的 agent 执行循环。通过受支持的 Codex 登录机制复用现有 ChatGPT 认证，无需用户另行提供 OAuth token。

不把 ChatGPT OAuth token 当成普通 OpenAI Platform API key。官方文档区分 Codex 登录与通用 API 凭据，且 `codex exec` 支持复用已有登录和输出 JSONL 事件。[Codex 认证](https://learn.chatgpt.com/docs/auth)、[非交互运行](https://learn.chatgpt.com/docs/non-interactive-mode)、[API 认证](https://developers.openai.com/api/reference/overview#authentication)

官方 WildClawBench Codex runner 当前配置的是 OpenRouter，需要新增/改造 ChatGPT 登录适配层，并验证容器内版本、认证、图片能力和日志格式。不能把官方 runner 视为已经支持本机 OAuth。保留官方任务环境、输入准备和评分语义，两组统一使用修改后的执行器。[官方 Codex runner](https://github.com/InternLM/WildClawBench/blob/main/src/agents/codex/runner.py)

Codex 登录仅解决主 solver 的认证。搜索、图像/视频生成及 LLM judge 等依赖逐项核验；不得假定它们自动共享 ChatGPT 权限。优先使用已有可用服务或支持的原生能力；评分服务如需改接，则记录 provider/模型/提示词/解析规则与官方实现的差异，并在构建题上验收后固定。

### 5.3 可控制的“思考时机”

Python 可以控制任务启动、结束、工具接口、结果回传、预算和证据可见性；Codex 在单次运行中自主决定何时搜索、继续读、调用任务工具和提交结果。固定 reasoning effort 可固定一个配置因素，但不能宣称 Python 能精确控制或完整读取模型内部思考。

首轮不把一次任务拆成很多独立 `codex exec` 来伪造连续推理。以后若要精确操纵每轮 LLM 调用和反思节点，再做独立的自定义 API agent 实验。

DCI 提供受限的关键词搜索、文件列表、按行/事件窗口读取和图片引用读取。返回 task ID、来源模型、episode、窗口定位和 source hash，并设定单次返回大小及总证据暴露上限。搜索范围由构建 manifest 决定，不允许搜索父目录或封存区。

### 5.4 任务环境与隔离

求解器仅获得当前题公开 prompt、exec 输入、所需工具、私有工作区；A3 通过只读 DCI 服务访问冻结构建库，容器不直接挂载书库。它不能看到全部项目目录、所有 task Markdown、其他测试题工作区、原始归档或 evaluator 数据。

官方 task Markdown 同时包含 Prompt、Automated Checks 等内容，必须通过解析器拆分；求解器退出后再由独立评分阶段注入 gt 和检查代码。[任务解析器](https://github.com/InternLM/WildClawBench/blob/main/src/utils/task_parser.py)、[官方 runner](https://github.com/InternLM/WildClawBench/blob/main/src/agents/openclaw/runner.py)

先验证挂载/权限边界和隔离 canary。普通任务所需网络可以使用，但需要阻断求解器获取本 benchmark 的测试轨迹、评测资源与公开解答；只隐藏本地路径不足以构成完整隔离。

## 6. 运行预算、评分和结果解释

### 6.1 运行安排

- 工程调试只使用构建集内部保留题，不访问测试轨迹或正式测试成绩。
- 正式配置冻结时写入：模型/CLI/镜像、prompt、任务工具、split、书库 hash、证据上限、超时、重试政策、任务顺序及 grader 版本。
- 建议初始并发为 1，正式求解时间上限为每题每组 30 分钟；环境准备和评分时间单列。可在构建集调试阶段统一调整，正式测试前必须冻结数值。
- 逐次记录 input/output/reasoning tokens（接口可提供时）、缓存、查询次数、读入内容量、耗时与认证/限额错误。不把“相同最大时间”写成“实际计算消耗相等”；CLI 若不能硬限制总 tokens，就明确记录此限制。
- 已执行的任务失败或超时不追加挑优尝试。网络传输重试须有固定策略，区分同一调用重试和一次新 rollout。基础设施故障单独记账。

### 6.2 评分

沿用冻结版本的官方逐题评分，保留原始指标及 overall score，不自行把所有任务变成二分类。官方通用评分可能使用 overall_score，缺失时按其代码聚合数值指标；实现时对冻结版本逐项核对并保留公式。[评分代码](https://github.com/InternLM/WildClawBench/blob/main/src/utils/grading.py)

主结果是冻结测试集（目标 24 题）的逐题配对得分变化：

`delta(task) = score_DCI(task) - score_no_history(task)`

汇报两组平均官方分数、平均差值、提升/不变/下降的任务数、六类描述性结果，以及时间、tokens、证据访问和执行故障。只有评分本身为二元的任务才直接报告成功率。

环境失败、求解超时、任务失败、评分器故障分开记录。缺失评分不能伪装成官方 0 分，不能悄悄删题缩小分母；若无法完成全部预定评分，报告计划覆盖数、已评分数、缺失原因及结果不完整状态。

目标 24 题是探索性 pilot。统计单位为任务或近重复 family，不是历史模型轨迹条数；按这些单位给出配对差值与适当的探索性区间。单次运行没有估计模型随机性，目标配额下各类别只有 2–5 个测试任务，类别差异只作描述。

共享构建库在全部正式测试期间冻结。查询日志和新轨迹留在结果域，不能帮助后续测试题；测试成绩不能反馈给 prompt、筛选规则或调参。

## 7. 拟新增的代码与交付物

路径均相对于项目根目录：

| 拟新增文件/目录 | 职责 |
|---|---|
| `experiment/benchmarks/wildclaw_bench/scripts/download_wildclaw.py` | 冻结来源清单、下载、hash、原始格式去重关联 |
| `experiment/benchmarks/wildclaw_bench/scripts/split_wildclaw.py` | 六类整数配额、近重复分组和固定划分 |
| `experiment/benchmarks/wildclaw_bench/scripts/build_dci_corpus.py` | 仅构建集生成有来源定位的只读检索视图 |
| `experiment/shared/codex_backend.py` | ChatGPT 登录 Codex 的执行适配与事件记录 |
| `experiment/benchmarks/wildclaw_bench/src/dci_tools.py` | 限定书库范围的搜索、读取、引用与预算 |
| `experiment/benchmarks/wildclaw_bench/scripts/run_wildclaw.py` | 两组任务调度、独立容器、配置冻结与超时 |
| `experiment/benchmarks/wildclaw_bench/scripts/evaluate_wildclaw.py` | 独立评分与原始指标归档 |
| `experiment/benchmarks/wildclaw_bench/scripts/summarize_wildclaw.py` | 逐题配对得分、成本、覆盖与故障报告 |
| `experiment/benchmarks/wildclaw_bench/manifests/` | 来源、模型覆盖、task family、split、配置和书库 hash |
| `experiment/benchmarks/wildclaw_bench/trajectory library/WildClawBench/` | 原有 task/trajectory 归档入口；hard coded skill 保留为空 |
| `experiment/benchmarks/wildclaw_bench/corpus/`、`experiment/benchmarks/wildclaw_bench/runs/` | 构建集可检索视图与各次运行结果 |

目录分区只是存储组织，实际隔离由容器挂载、访问接口与网络规则保证。

验收顺序：全部来源覆盖与 hash → task 分组及划分 → 检索库内容与隔离 → 构建集内部 smoke → 冻结正式配置 → 预计 48 次求解及评分（实际为 2 × 冻结测试任务数）→ 逐题结果报告。

用户已确认以上方案并明确以完成 24 道测试题验证为目标。认证平台已指定为 Codex/ChatGPT；具体资源与运行参数在构建集工程调试阶段落实，不能使用测试结果决定。所有工作限定在 skill_dci；只管理 hangxiao-skill-dci 前缀及所有权标签对应的实验资源，不更改或清理他人的文件、进程或容器。

---

## 8. 检索改进讨论（2026-09-08；尚未执行）

首轮36构建/24测试、A0/A3共48次正式运行已经完成。本节是用户要求查阅上游代码和相关论文后的补充；第1–7节记录首轮方案，不能将其历史执行授权理解为启动这些新实验。

完整提案已写入[原多基准实验框架第13节](../../../../reports/DCI_upgrade_report.md)，另有HTML阅读版（历史文件：`reports/DCI_upgrade_report.html`，不在精简发布中）与[文献/源码核查入口](../../../../docs/research_notes/retrieval_followup/README.md)。

当前建议的讨论顺序：

1. **查询与读取接口。** 明确短语/OR/AND及匹配范围，以当前操作、工具和约束构造查询；返回命中字段、原文位置和事件入口；从命中读取关联动作与实际返回，明确截断与续读。别名、字段去噪、展示去重和排序分别记录为因素。
2. **使用策略。** 阅读后检查当前环境、输入和输出要求；保存采用/拒绝及验证依据。工具报错、环境不符、验证失败或明确证据缺口时再查，允许拒绝不适用经验和停止无增量搜索。
3. **独立后续因素。** 固定候选内排序与候选生成分开；ExpeL/ReasoningBank启发的策略卡属于表示变化，对应A4；DR-DCI/RISE的工作区与动态扩张属于后续规模方向，不同时揉进一个“DCI升级”条件。

源码核查显示：Lite已有可组合终端操作和持续交互；多角度搜索、先读再选和相关性排序主要在IR提示词中。DR-DCI实现了检索器选候选、按源路径去重与持久工作区读取，但没有现成的历史操作环境适配器。SIEVE提供字段Boolean和定点读取的紧密参考，IRCoT/ITER等提供状态化查询和候选阅读记录的思路；这些均不等于已经验证在本任务上有效。

评估仍须分别看命中、操作链读取完整性、适用性、采用验证、最终配对分数及实际成本。已做逐题分析的24题作为诊断/回归集；后续调参先在36构建题内部按family留出，排除验证任务的全部模型轨迹、同family材料及其派生卡片。确认泛化需要未参与调参的新holdout。

本次未修改冻结书库、检索代码、prompt、评分器和正式结果，也未运行solver或grader。具体接口、参数、对照和预算在讨论之后另行形成新版方案；旧WikiSkill/handbook笔记继续保留。
