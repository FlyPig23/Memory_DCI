# 为什么 agent 很少检索、也不改 skill：DR-DCI、Lite 与 Codex 源码对照

日期：2026-09-25。范围：源码审计、历史日志检查与下一步设计讨论。没有改实验配置、现有 skills、数据划分或评分，没有运行新实验。

## 结论先行

目前最明确的问题是**资源如何被发现，以及维护工作是否真正被调度**。

- V4 使用了 Codex 原生终端工具，但 67 个蒸馏 skills 没有进入启动时的原生技能目录。24 次运行的历史 session 均可直接验证这一点。模型只收到 /skills 的一般说明，需要自己探索里面有哪些经验。
- V4 的提示词明确允许不检索；skill 修改也由 solver 自行决定。solver 结束后直接冻结、审计和评分，没有独立的经验复盘回合。
- DR-DCI 的任务从空的可见资料工作区开始，答案必须有文件证据。它明确规定第一次取资料、取完先查读、何时再取、什么时候停止。检索对完成任务本身具有直接价值。
- DR-DCI 主配置仍由模型决定下一次调用哪个工具，没有在每个 turn 强制 pull。扩展当题的证据工作集，与跨任务修改可复用 skill，是两种不同机制。
- Codex 当前源码把 skill 的发现、按需读取和后台记忆整理分成不同路径。尤其是记忆整理有独立触发、专用输入输出和 no-op 条件，不能用“目录可写”代替。

这些证据支持改进实验的可发现性和工作流，但不能直接证明改完会提高分数；历史 V4 的成绩和访问次数仍按原结果保留。

## 1. 审计版本与阅读边界

| 对象 | 固定 commit / 本地证据 | 主要阅读范围 |
|---|---|---|
| DR-DCI | 0d0410f3c2b98fb33145adc250a09fded028cd3c | 主启动脚本、实际 prompt builder、pull/read/bash、Pi agent loop、可选 research rounds 和 rescue |
| DCI-Agent-Lite | 271f37e71f053bf0c99c05ce6d2fb53b841d922e | benchmark 默认/IR prompt、RPC runner、上下文配置与启动脚本 |
| OpenAI Codex | c9e25207073a88f1a3a4a885991b9143799a084f | turn loop、tool routing/parallel execution、skill discovery/catalog、memory write pipeline |
| 我们的 V4 | experiment/variants/dci_skills_native/ 与 24 次保存 session | 实际 prompt、资源挂载、启动目录、结束和 skill promotion 路径 |

源码快照放在 archive/research_source_audits/20260925/。历史 V4 使用 CLI 0.153.4；本次 Codex 上游快照不能当成该版本的完整实现说明。关于 V4 的判断优先使用运行时保存的 prompt/session 和项目代码。

已有论文保留在 papers/DCI.pdf、papers/Dr_DCI.pdf，本轮没有重复下载。一手入口：[DCI](https://arxiv.org/abs/2605.05242)、[DR-DCI](https://arxiv.org/html/2606.14885v1)。

## 2. V4 为什么很少读、完全没有更新既有 skill

### 2.1 已证实的实现和运行事实

| 事实 | 证据位置 | 含义 |
|---|---|---|
| 检索明确可选 | experiment/variants/dci_skills_native/prompt.txt:21–25 | agent 不检索并不违反这份实验指令 |
| 开始时只介绍 /skills，没有给 67 个名称与描述 | prompt 第 9–19 行；全部 24 次启动 session | 模型先要支付探索成本，才知道是否有相关经验 |
| 67 个 skill 不在原生 Available skills 中 | 逐运行目录审计（历史文件：`docs/research_notes/retrieval_followup/v4_skill_catalog_audit_20260925.json`，不在精简发布中） | 使用原生 bash，不等于接入原生 skill 发现机制 |
| 官方任务 skills 另有注入路径 | experiment/src/codex_backend.py:80–98、experiment/src/task_runtime.py:256–265 | 读官方技能不能算使用我们的蒸馏库 |
| /skills 实际可写，但仅允许修改既有文件 | runner.py:369–383、skills_runtime.py:85；prompt 第 14–19 行 | 零更新不是简单的文件系统写权限故障 |
| 一次 solver 调用后就冻结、审计、评分 | runner.py:394–418 | “可以在解题后更新”没有独立调度回合；solver 必须在自己结束前完成 |
| 没有既有 skill 的修改尝试 | 24 个 skill_changes.json；初始/最终 manifest | 不应归因为审计拒绝了大量有用修改 |

最后一点的细节：23 次运行没有文件变化；唯一有变化的 dci4-formal-17 在 skill 目录放入了 paper-banana 仓库等 31 个新增文件，不属于允许编辑的 67 个既有 skill，未传递到下个任务。没有发现“修改了既有 skill，却全部被拒绝”的情况。

目录审计只检查首次 assistant 响应前的 developer 技能目录；**零目录命中不等于模型整轮没看到 skill**。已记录的 3/24 次正文访问依然成立。7/24 次目录访问、3/24 次正文访问、7 个不同 skill、0 个既有更新，分别是不同统计口径。

### 2.2 从事实推导的解释

V4 的要求是：完成当题，同时允许在有价值时搜索和维护资源。WildClawBench 的许多题可以直接用工具完成；历史经验并非答案证据的必要来源。模型可能判断直接操作更便宜，而 skill 更新的收益主要发生在之后的任务。

可能的行为链是：不知道有哪个相关 skill → 没有打开正文 → 没发现具体缺陷 → 没有修改依据。这是有代码和日志支持的机制假设，尚不是通过消融实验确认的因果结论。

“能写”“被要求检查是否值得写”“确实存在值得写的经验”是三个不同条件。最后一种即使成立，也需要 agent 看到和识别对应证据。

## 3. DR-DCI 主流程实际如何工作

### 3.1 它的 pool 到底变了什么

DR-DCI 有固定的大语料库，以及每个问题自己的可见文件工作区。pull 根据模型给出的 query 从前者取回文件，累积进后者；模型随后在已取回的文件中运行 rg 和 read。

    当前问题 + 初始空工作区
      → agent 生成 query，调用 pull
      → 检索后端返回候选，全文成为本地文件
      → 工具返回新增/已见数量、排名和路径预览
      → agent 用 rg 筛选，用 read 阅读证据
      → 证据够：引用已读文件回答
      → 出现新的未解决线索：基于线索再次 pull

变化的是**这道题可见的文档集合**，不是改写原文、更新技能规则、训练检索器或将经验传到下一题。[pull 的文件物化与去重](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L451-L575)

### 3.2 读实际启用的 prompt，而不只读 system_prompt.txt

README 指向的主 launcher 使用 rank_aware prompt、root_flat_disclosed 布局和 read,bash,pull。实际任务指令由 build_rank_aware_pull_prompt 生成；静态 system prompt 文件不是这条默认路径的全部提示。[主启动参数](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_full830_dynamic_pull_root_flat_openai_high_l3_300turn_parallel30.sh#L45-L85)

| 环节 | prompt 的具体规则 | 对行为的意义 |
|---|---|---|
| 起点 | 工作区为空；完整语料库不直接展示 | 先获得资料，才有可查读的证据 |
| 首次查询 | 从原问题抽取简短线索；偏实体、标题、日期等 | 不只笼统说“必要时检索” |
| 取回后 | 每次 pull 后先停止继续 pull，查读已有文件 | 防止只取候选却不检查内容 |
| 筛选 | 利用排名预览，先本地筛选，再打开有希望的文档 | 明确从候选到证据的下一步 |
| 再次查询 | 材料出现新线索时再 pull；普通不确定性先查当前工作区 | 扩张由信息缺口驱动 |
| 截断 | 定位相关位置，用 offset/charOffset 继续读 | 大文件有续读入口 |
| 完成 | 证据充分后停止工具，引用实际查读过的文件 | 不把命中列表或排名当最终依据 |

这些是模型应遵循的工作约定，不是代码已经逐项验证的完成门槛。[实际 prompt builder](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_bcplus_eval.py#L939-L1067)

### 3.3 工具输出也在引导下一步

主启动说明配套的是 Qwen3-Embedding-8B 检索服务。因此这里有两层：pull 做全库候选召回，本地 rg/read 做细查和验证；DR-DCI 不是把所有步骤都变成关键词匹配。框架可以替换检索后端，不能把这个具体后端等同于 DCI 的定义。[检索服务启动说明](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/README.md#L135-L152)

pull 直接将模型提供的 query 传给检索服务，内部没有另一个 query 生成模型。主配置每次一条 query，topK 为 300–600；取回全文放进工作区，工具消息默认只展示最多 20 条新增文件预览。**20 是预览数，不是取回全文数。**

反馈包含新增/已经可见的数量、rank/path 和由文件名产生的标题提示，再提醒去本地搜索和阅读。agent 因而知道文件在哪里、哪些是新的、下一步怎么查。去重依据源路径，没有语义去重。[query 传递](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L408-L426)、[执行与输出](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L783-L906)

可借鉴的原则是：工具结果要让 agent 看清**结果是什么、局限是什么、下一步有哪些可执行动作**。无需为了这个原则重新实现 Codex bash/read。

### 3.4 自主循环不等于每轮强制查询

Pi 主循环执行模型的 tool calls，把结果加入上下文，再请求模型；没有工具调用和排队输入时结束。没有“本轮必须 pull/read”的检查。[agent loop](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/agent/src/agent-loop.ts#L167-L228)

论文附录的主 DR-DCI 830 题日志中，pull 为 3,198 次，约 **3.85 次/题**；平均总工具调用约 **30.94 次/题**。所以多数操作仍是本地搜索与阅读。论文另比较排名预览、隐藏预览和打乱预览：100 题设置中准确数分别为 82、72、76。它支持认真设计候选导航，但不能当成我们 24 题中 skill 目录改造的收益估计。[论文表 4 与附录表 10](https://arxiv.org/html/2606.14885v1)

### 3.5 默认主流程与可选控制分开

- research-rounds 默认 0。启用后才有多轮新上下文研究、结构化状态和 verifier：记录假设、缺口、矛盾、已验证证据、负面发现与下一组搜索方向；验证未通过时反馈到下一轮。这是另一个可参考的实验分支。[默认值](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_bcplus_eval.py#L239-L262)、[状态和 verifier prompt](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_bcplus_eval.py#L3604-L3673)、[调度](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_bcplus_eval.py#L3950-L4118)
- Context reset/rescue 对已结束的低置信或无法回答样本再运行：可保留文件工作区、清除失败推理，再读资料。它不是每个 turn 都更新 pool。脚本有 reflection/clean-dci 等模式，默认参数不等于论文组合，复现必须固定模式、筛选阈值和工具集。[rescue 模式](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_confidence_reflection_rescue.py#L228-L246)
- 主 launcher 关闭 budget gate，并把 submit-now 触发参数设成 0；不能把这些可选开关说成主实验检索积极的原因。
- 主配置 level3 使用工具输出截断和 micro-compaction。后者清除较旧工具输出，保留近期内容和调用结构，不是自动写研究总结，也不规划查询。[level3 默认值](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/settings-manager.ts#L182-L189)、[micro-compaction](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/compaction/micro-compact.ts#L1-L51)

## 4. DCI-Agent-Lite：默认与 IR 不能混为一谈

Lite 的基本模式是 agent 对可见 corpus 自主生成 shell/rg 命令，读文件，再决定继续查还是回答。默认 benchmark prompt 较短：答案在 corpus 内、使用本地搜索、不给 web。开启 IR 后，才要求多角度关键词查询、检查证据缺口、读候选后筛选、最后相关性排序。

IR 中“最终最多 20 篇”是最终相关文档列表限制，不是每次检索 topK。enable-ir 是独立实验条件；不能把加强版提示统称为默认 Lite。[默认与 IR prompt](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_bcplus_eval.py#L363-L405)

RPC runner 启动 agent、接收事件，等结束或达到上限；没有最低检索次数检查，也没有独立的 skill 写回流程。终端有写文件能力，并不意味着任务包含经验维护。[RPC 调度](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/pi_rpc_runner.py#L1263-L1404)

复现细节：安装脚本会克隆外部 Pi fork 的可变分支。不能仅固定 Lite commit 就认为底层 runtime 全部固定，也不能直接把 DR-DCI fork 的所有行为归给 Lite。[setup.sh](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/setup.sh#L69-L84)

## 5. Codex 仓库怎样读

不必从头读 CLI UI 或全部 Rust crate。围绕我们的问题，沿下表阅读。路径相对固定 Codex 快照根目录。

| 顺序 | 文件 / 入口 | 重点问题 |
|---|---|---|
| 1 | codex-rs/core/src/session/turn.rs：run_turn | 一次模型输出后，为什么继续或停止？ |
| 2 | codex-rs/core/src/tools/router.rs；tools/parallel.rs | 工具如何展示、分发，并发如何限制？ |
| 3 | codex-rs/core/src/tools/handlers/unified_exec.rs 及同名目录 | 长命令如何返回状态、继续取输出？ |
| 4 | codex-rs/ext/skills/src/host_roots.rs | 哪些目录中的 skill 会被发现？ |
| 5 | codex-rs/ext/skills/src/render.rs、catalog_prompt.rs、host_prompt.rs | 先展示什么 metadata，什么时候加载正文？ |
| 6 | codex-rs/core/src/context_manager/history.rs | 实时上下文怎样截断，与完整日志什么区别？ |
| 7 | codex-rs/memories/write/src/start.rs → phase1.rs → phase2.rs | 谁触发提取和合并，什么时候可以不改？ |

### 5.1 工具层与工作流层分开

Codex 主循环同样是：模型产生工具调用 → 执行 → 返回结果 → 再请求模型；模型结束且没有新输入时，才考虑停止。当前源码还支持 stop hooks，在有继续工作的提示时阻止结束并继续一轮。**这提供了接入工作流要求的位置，但默认没有“必须检索 DCI”或“必须改 skill”的规则。** [run_turn](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/core/src/session/turn.rs#L149-L163)、[继续、压缩和停止](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/core/src/session/turn.rs#L554-L694)

Tool router 分开保存模型可见 specs 和实际 handler。终端 schema、参数、并发能力与权限属于 harness；为了哪个经验缺口去查询属于任务策略。[router](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/core/src/tools/router.rs#L73-L81)

可借鉴的工程处理：

- **长命令会话化**：exec_command/write_stdin 分开开始执行、等待窗口和继续取输出；不必封一个每次阻塞到底的新 retrieval 工具。[参数](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/core/src/tools/handlers/unified_exec.rs#L27-L67)
- **并发能力显式声明**：调度器按工具声明的 supports_parallel 选择共享锁或独占锁。它不自动理解任意 shell 是否有副作用；有依赖的写操作仍需串行安排。[parallel.rs](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/core/src/tools/parallel.rs#L138-L205)
- **完整日志与实时上下文分开**：结果可完整存档，进入上下文再按预算截断。调用过读取，不代表全文都被模型看到；看到也不代表采用。[history.rs](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/core/src/context_manager/history.rs#L442-L501)

对我们最直接的结论是：继续用原生终端、文件和会话机制；把研究工作放在资源目录、提示策略、状态记录和调度边界上。

### 5.2 Skill 的关键：先让模型知道“有什么”

Codex 从指定来源发现 skill，把名称、描述和路径的简短目录放进上下文。任意额外挂载的 /skills 不自动成为发现来源。来源包括兼容的 CODEX_HOME/skills、用户/项目 .agents/skills、系统、插件和额外 roots。[发现路径](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/host_roots.rs#L48-L184)、[目录注入](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/fragments.rs#L39-L57)

用户明确指定 skill 时，代码可匹配已发现 skill 并加载正文；普通任务主要由模型结合描述判断是否适用，再读取。并非每个任务都有固定 topK 技能检索。[显式选择](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/skills/src/selection.rs#L31-L108)、[正文加载](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/host_prompt.rs#L60-L108)

使用模板要求任务明确匹配 description 时使用、先读完整正文、选覆盖需求的最小集合，再按需读关联材料。但模板注入受模型配置控制，不能说所有模型都收到同样完整指令。[使用模板](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/catalog_prompt.rs#L24-L40)、[条件注入](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/extension.rs#L428-L443)

当前目录默认预算为上下文窗口的 2%；未知窗口时退到 8,000 字符。超预算可能缩短描述甚至省略条目。description 开头应清楚写适用任务、工具和约束；实验应记录实际展示的条目。[预算](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/render.rs#L123-L149)、[超预算处理](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/render.rs#L321-L362)

两个易误读之处：allow_implicit_invocation=true 只允许隐式目录曝光，不保证使用；dynamic skill selector 在这里属于 shadow experiment，不改变 prompt，不能称为默认启用的 BM25 技能路由。[目录可见性](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/provider/host.rs#L129-L151)、[shadow 配置](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/ext/skills/src/config.rs#L14-L15)

### 5.3 Memory write 是独立任务，且允许 no-op

当前后台记忆机制有门控：ephemeral、功能未开、非根 agent 等情况下跳过；满足条件才先执行 Phase 1，再执行 Phase 2。外层 memories feature 默认关闭，这里讨论可用设计，不断言历史实验已启用。[两阶段调度](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/src/start.rs#L20-L92)、[feature 默认值](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/features/src/lib.rs#L1147-L1152)

    符合条件的历史 rollout
      → Phase 1：提取结构化经验；没有有用经验可以空输出
      → Phase 2：读取当前记忆、检查增量，调度独立整合 agent
      → 按版本契约更新记忆产物；V1 可选写 skill
      → 验证产物、保存状态；没有有效增量可以不改

它不是每道当前任务结束立即触发：会排除当前 thread，并考虑来源、idle 状态、时间窗。Phase 2 使用单独的 MemoryConsolidation 内部任务。[历史选择](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/state/src/runtime/memories.rs#L225-L254)、[专用 agent](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/src/runtime.rs#L398-L423)

需要区分 memory 版本：V1 契约包括 MEMORY.md、memory_summary.md 和可选 skills；V2 当前模板只要求生成或更新 memory_summary.md，不能概括成所有 Codex memory 都生成 skill。[版本选择](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/src/prompts.rs#L85-L88)、[V2 模板](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/templates/memories/consolidation_v2.md#L1-L52)

V1 要求优先改已有 skill；选择重复有用的工作流、已验证修复、精确输出契约；写触发条件、输入、步骤、效率策略、错误处理和验证。没有可靠可复用经验时，no-op 合法。最终结构验证也不保证迁移收益。[skill 标准](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/templates/memories/consolidation.md#L687-L741)、[no-op 门槛](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/templates/memories/stage_one_system.md#L19-L45)

## 6. 下一轮建议：先可发现，再分开测试读与写

以下为讨论方案，尚未实施；不回写历史 V4，不重跑已完成的实验。

### 6.1 第一优先级：接通 metadata 目录

把 67 个 skill 的 name、description、path 通过所用 CLI 支持的原生发现路径提供给 agent，或显式注入等价简短目录。正文仍由原生终端读。先不改正文、不引入新检索器，便于观察目录曝光本身的影响。

避免两份 skill 副本不同步：目录指向本轮可写版本；记录实际展示条目、路径和 manifest。历史 CLI 的发现机制不能仅凭上游新代码假定，准备时以实际启动上下文确认。

### 6.2 第二优先级：把查询决策写成可执行约定

借鉴 DR-DCI 的“取回—查读—识别缺口—再查”：

1. 开始时检查可见技能目录；明确适用才打开少量正文。
2. 阅读后简要记录采用或不采用，以及环境是否满足前提。
3. 遇到具体阻塞，围绕操作、工具、错误和约束查询 trajectory；读完整动作及结果，再决定下一步。
4. 证据足够时停止；重复查询没有新结果时换线索或继续解题。

应要求**做出可核查的相关性判断**，不强制每题采用 skill、固定搜 N 次或写长篇自述。目录曝光与更明确的查询策略是两个实验因素，最好分开。

### 6.3 第三优先级：独立调度经验复盘

让“是否值得修改”成为有输入输出的阶段。可用 solver 后续回合，也可用独立且有预算的 reviewer；继续使用原生工具。

建议冻结当题答案、尚未提供隐藏评分反馈时执行：输入仅含当题可见执行记录、验证结果和相关 skills；输出 update 或 no_update。update 给出支持证据、适用边界和 patch；no_update 简述原因。不规定每题必须修改。

修改先进入候选副本，再检查身份、格式、内容约束与证据，认可后传给下一题。现有 promotion 主要是语法和标识检查，不是语义正确性或迁移验证。复盘与解题 token/时间分别记录，也报告总成本。

### 6.4 什么实验回答什么问题

| 问题 | 尽量只改的因素 | 观察 |
|---|---|---|
| 模型是否不知道有哪些 skills？ | 增加原生 metadata 曝光 | 目录可见、正文访问、采用、分数与成本 |
| 是否需要更明确的检索策略？ | 同一目录与技能下改变查询约定 | 信息缺口、查询后动作、重复无效检索 |
| 更新是否帮助后续任务？ | 同一读取策略下比较冻结技能与可更新技能 | 修改证据、后续采用、后续成绩与额外成本 |

写回收益应在之后任务观察；新更新不能反向解释当前题已完成的答案。跨测试任务传递经验属于顺序在线评估，应固定顺序并保存每题起始 skill 版本；与每题独立、冻结资源的条件分别报告。

建议事件链：catalog_exposed → search → body_read → adopt/reject → revision_proposed → revision_promoted → reused_later。逐步统计。优化目标仍是任务质量和总成本，不是检索或修改次数本身。

## 7. 文档与证据管理

- 本文：机制分析、源码导航和待讨论设计。
- V4 目录审计（历史文件：`docs/research_notes/retrieval_followup/v4_skill_catalog_audit_20260925.json`，不在精简发布中）：逐运行路径和命中统计，不复制完整上下文。
- [来源清单](workflow_audit_sources_20260925.json)：固定版本、关键源码、论文与本地证据。
- archive/research_source_audits/20260925/：上游源码快照及用途说明，未安装为实验依赖。
- 原实验代码、67 个正式 skills、历史结果保持原状态。本轮不把建议登记为已采用的新条件。
