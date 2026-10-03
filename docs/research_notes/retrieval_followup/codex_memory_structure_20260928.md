# V5：每题独立的空 memory，如何记录和更新经验

日期：2026-09-28。本文是实现建议；不改变历史结果，不启动实验。V5 的约束是每个测试任务使用独立、初始没有经验内容的 memory，继续使用 Codex 原生工具。67 个既有蒸馏 skills 不作为本轮初始经验。当前题 memory 不传给其他测试题。

采用 `memory_summary.md` 作为短导航，`MEMORY.md` 作为适用性和关键词索引，`entries/<id>.md` 保存带证据和适用条件的唯一规范正文。原始 trajectory 继续只读。检索、记录、验证和修改都可以通过原生终端/文件工具完成。

## 1. Codex 源码中值得保留的结构

审计版本固定为 `openai/codex@c9e25207073a88f1a3a4a885991b9143799a084f`，本地快照在 `archive/research_source_audits/20260925/codex/`。这份源码不是历史 CLI 0.153.4 的行为证明，也不意味着 V5 要启用 Codex 的全局后台 memory。

| 源码实际提供的设计 | V5 采用的部分 |
|---|---|
| V1 memory 将简短 summary、可检索 handbook、来源摘要分开；skills 是可选输出 | 保留导航、正文、来源三个层次，不要求把每条经验写成 SKILL.md |
| 条目包含 scope、applies_to、关键词和来源指针 | 每条经验明确触发条件、环境前提和证据位置 |
| 轨迹是不可改写的证据；第三方文本作为数据处理 | memory 提炼动作与观察，原始 trajectory 保持只读 |
| 没有有用的新经验时允许 no-op | 不设每题最低条目数、最低修改次数 |
| Phase 1 提取和 Phase 2 整合由独立阶段调度 | 将“检查是否需要记录/修改”作为有明确触发的工作，而非只授予写权限 |

依据：[V1 文件结构与证据/no-op 规则，第 20–50 行](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/templates/memories/consolidation.md#L20-L50)、[handbook 的 scope、来源和关键词，第 201–273 行](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/templates/memories/consolidation.md#L201-L273)、[两阶段调度，第 88–91 行](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/src/start.rs#L88-L91)。

Codex 当前上游的后台机制有自己的 feature、历史任务选择与 idle 条件。V5 的每题 memory 由实验工作流调度，不能依赖这些后台任务自动处理 `/memory`。上游 `memories` feature 默认关闭；V1 可选写 skills，V2 模板只要求更新 summary。[feature 默认值](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/features/src/lib.rs#L1147-L1152)、[V2 模板](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/templates/memories/consolidation_v2.md#L1-L52)。

## 2. 三篇 memory 论文能借鉴什么

以下引用固定论文版本，只采用与本轮问题直接相关的设计。不把各论文的性能数字当作 WildClawBench 的预期收益。

| 工作 | 原方法 | 适合 V5 的改法与边界 |
|---|---|---|
| Reflexion，2023，§3 | 将轨迹和反馈交给独立 self-reflection 组件，形成短的经验记录，供后续尝试使用 | 记录“哪里失败、观察到什么、下一步如何改变”。本轮只能用 solver 可见的工具结果和检查；不引入隐藏评分或额外完整重跑 |
| MemGPT，2024 版本，§2 | 区分上下文内的工作记忆和上下文外的存储，通过显式工具操作搬运；提供容量与分页机制 | summary 保持短且可见，详细条目按需读取。文件写入后不会自动改变已发出的模型上下文，继续求解前需要读到更新后的导航/相关条目 |
| A-Mem，2025，§3.1–3.4 | 为记忆生成上下文、关键词、标签与链接；新增记忆触发关联和已有记忆更新 | 增加新条目前先查已有条目，发现同一流程则补充或纠正，保留来源关联。V5 用 Markdown 链接和 rg，不移植其 embedding 检索器 |

一手来源：[Reflexion v4](https://arxiv.org/html/2303.11366v4#S3)、[MemGPT v2](https://arxiv.org/html/2310.08560v2#S2)、[A-Mem v11](https://arxiv.org/html/2502.12110v11#S3)。

这三篇共同支持的是：记忆有自己的读取、记录和更新约定。它们并不能证明“目录可写”就足以产生高质量记忆。Reflexion 的多次尝试、MemGPT 的分层存储、A-Mem 的向量与链接机制，都应与我们采用的局部设计明确区分。

## 3. 最小目录

下面的 `/memory` 是建议的 solver 可见挂载路径；宿主机每题使用不同的目录。

```text
/pool/                           # 36 个构建任务描述与带分数 trajectories，只读
/memory/                         # 当前测试任务独享，可写
  memory_summary.md             # 简短目录：触发条件、状态、条目 ID
  MEMORY.md                     # 适用性与关键词索引
  entries/                      # 初始为空
    <id>.md                    # 唯一规范经验正文，含来源窗口指针
```

初始化只创建空目录/空白模板，两种都可以，但不能预填经验。若建立模板，`memory_summary.md` 只说明“当前任务尚无 memory entries”，`MEMORY.md` 只保留标题与空条目区；结构文字不算已学习内容。实验日志应记录初始 `entry_count=0`，以及每题独立的初始文件清单。

正文只保存在 `entries/`，两个顶层 Markdown 只维护不同粒度的索引，不复制完整流程。宿主机保存完整 transcript、文件 diff 和版本快照；这些审计文件不必放入 solver 的检索目录。

这套目录借鉴 Codex 的层次，格式由实验定义。`memory_summary.md` 不会因为同名就获得 Codex 自动加载；工作流必须明确告诉 solver 路径，并在后续求解回合提供或读取最新摘要。不要写入用户全局 `CODEX_HOME` memory，避免把其他任务带入这次测试。

## 4. 每条 memory 最少记录什么

建议每条记忆对应一个可以影响下一步行动的发现。字段可以写成普通 Markdown，不需要包装成 JSON 或技能 frontmatter。以下是模板，不是已发生的实验记录。

```markdown
## M001 · <操作或问题的短标题>
keywords: <工具名、操作名、约束或原样错误词>
trigger: <什么具体情形下值得读这一条>
applies_to: <当前输入、工具/版本、环境前提和不适用边界>
origin: trajectory | current_task | mixed
status: source_observed | locally_verified | uncertain | contradicted
updated_at: <时间>
related: <已有条目 ID；没有则省略>

### Knowledge
- <具体发现或步骤；适用时写 symptom → observed cause → action>
- <需要先检查的前提和停止条件>

### Evidence
- <来源 episode_id / source model / 原始路径 + 精确行号或事件 ID>
- <看见的动作及其返回；只描述实际读到的部分>
- <若只是来源 assistant 自述，明确写 self-report>

### Local validation
- <未在本题执行；或本题实际检查的命令/事件 ID、观察结果>
- <验证覆盖到哪个步骤，哪些要求尚未验证>

### Limitations / revision
- <不确定项、冲突、未完成验证、被替代条目；没有则省略>
```

`origin` 与 `status` 必须分开。一条从构建轨迹学到的操作可以后来在当前题中验证，此时 origin 仍保留 trajectory，status 变为 locally_verified；不要丢失原始来源。若同一条里有已验证与未验证的独立主张，应拆成条目或逐项标注，不给整段内容统一盖章。

| 状态 | 最低证据 | 不能推导出的结论 |
|---|---|---|
| source_observed | 在历史轨迹窗口中读到具体动作与对应观察；是否只是自述须说明 | 不能直接推导当前环境有效，也不能推导整个历史任务成功 |
| locally_verified | 当前题实际执行了相应检查，工具观察支持这条主张 | 单个文件存在或某个检查通过，不等于整题满分 |
| uncertain | 只有候选方法、自述、缺少结果的片段，或证据不一致 | 不能写成可靠操作规则；应记录还需检查什么 |
| contradicted | 新观察直接否定旧主张，且可定位证据 | 不静默覆盖成另一条“成功经验”；标明原主张为何失效 |

例如“轨迹显示该命令返回成功”属于 source_observed；“在本题用该命令生成文件，并检查了格式和所需字段”才可在这个检查范围内标 locally_verified。`trace_status=completed`、来源模型的最终“done”以及阅读者的合理猜测，均不能单独升级为 locally_verified。局部失败也可以有价值：保留错误、已确认的限制和可检查的下一步，不要求每条 memory 都是正向解法。Codex 对仅有 assistant 成功自述时采用保守结果标签，见 [stage_one_system.md 第 156–216 行](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/templates/memories/stage_one_system.md#L156-L216)。

## 5. 摘要目录怎么写

`memory_summary.md` 只做短目录，避免与正文形成两套需要同步的完整流程。

```markdown
# Task memory index
task_scope: <当前测试 task_id；所有条目仅属于本题>

| ID | When to read | Keywords | Status | Location |
|---|---|---|---|---|
| M001 | <触发条件> | <工具/错误词> | source_observed | entries/M001.md |

## Open checks
- <未解阻塞与对应条目 ID；没有则省略>
```

更新顺序是：先改正文和证据，再改 summary；保留稳定 ID，不将旧 ID 重新分配给无关经验。summary 中只能提升导航优先级，不能把尚未验证的条目压缩成确定结论。模型可先读摘要，再用 rg/按行读取定位正文，最后按需回原 trajectory；这个过程不需要增加新的 read/bash 包装器。

## 6. 什么时机检查与更新

建议在一次任务的两个自然边界检查 memory，而不是每次工具调用都总结：

1. DCI 已读到与当前阻塞相关的操作及结果时，决定是否保存 source_observed 经验；仅有关键词命中但没有读到内容时不蒸馏。
2. 采用该经验后，或当前题出现明确失败、修复与验证时，检查是否新增证据、收窄前提、纠正旧条目或升级局部状态。

若实现为独立 writer 回合，输入应包括当前题的最新 memory、此次确实可见的工具观察/读取片段和必要的任务上下文；输出是一个可审阅的更新或 no_update。不要把“检索已经调用”替代为“内容已经读到”，也不要由 writer 读取测试集历史解法或隐藏 grading 材料。writer 可以用原生工具更新普通文件。

最后一次归档回顾可以记录最终状态，但答案提交后才产生的 memory 不能计为“帮助本题”。若所有写入都发生在题目结束后、且各题又隔离，本轮就没有测试到 memory 对求解的帮助。因此需要至少记录首次写入时点、后续读取时点及当时是否还在求解。

no_update 合法的情况：只有重复信息；没有可支持的动作/结果；与当前题无关；已有条目已经覆盖；没有新验证；只剩泛泛建议。必须检查是否有新信号，不强制一定新增条目。no_update 及理由记录在审计事件中，避免把一串“无更新”堆进可检索的 memory。上游也明确不以固定条目数为目标，见 [consolidation.md 第 185–197 行](https://github.com/openai/codex/blob/c9e25207073a88f1a3a4a885991b9143799a084f/codex-rs/memories/write/templates/memories/consolidation.md#L185-L197)。

## 7. 实现时需要记录的最少证据

- 初始化：task_id、独立 memory 根、初始条目数为 0、没有读取其他测试任务 memory。
- 读取：原始 trajectory 路径与窗口、memory 条目 ID、是否实际返回了正文。
- 写入：触发原因、create/update/no_update、前后文件 hash/diff、来源/状态变化。
- 验证：支持 locally_verified 或 contradicted 的本题事件 ID、观察结果和检查范围。
- 使用：写入后是否再读、采用或拒绝的可见说明、后续动作；没有证据时不推断采用。
- 成本：solver、memory writer 各自的 token/时间以及合计；最终提交后生成的条目单列。

source_observed/locally_verified 标签是可审计的证据声明，不是自动真实性判定。实现可以检查字段和引用是否存在，语义是否获得足够支持仍需抽样审阅。先把这些统计与 24 题分数并排报告，避免再次用文件访问数代替实际使用。

## 8. 论文归档信息

以下原始论文 PDF 已下载并验证 PDF 文件头，固定版本和 SHA-256 记录见 papers/V5_memory_sources.json。

| 论文 | 固定 PDF 来源 | 建议文件名 |
|---|---|---|
| Reflexion | https://arxiv.org/pdf/2303.11366v4 | papers/Reflexion_2303.11366v4.pdf |
| MemGPT | https://arxiv.org/pdf/2310.08560v2 | papers/MemGPT_2310.08560v2.pdf |
| A-Mem | https://arxiv.org/pdf/2502.12110v11 | papers/A-Mem_2502.12110v11.pdf |

本轮主张来自以上论文原文和固定 Codex 源码。Markdown schema、证据状态和每题隔离规则是面向 V5 的实验设计，不宣称属于任何一篇论文的原样实现。
