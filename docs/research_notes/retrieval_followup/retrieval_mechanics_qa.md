# 首轮检索与相关论文：机制详解

日期：2026-09-09。本文对应本次关于关键词生成、稳定性、SIEVE、IRCoT、ITER、Agentic-R、DCI-Agent-Lite 和 DR-DCI 的讨论。只读核查代码、日志和论文，未运行新实验。后续提案仍属于[原框架第13节](../../../reports/DCI_upgrade_report.md)的讨论范围。

## 1. 首轮究竟怎么检索

**首轮是“同一个解题 agent 自主写 query，调用受限的字面搜索/读取工具”。** 没有独立关键词生成器，也没有先运行关键词抽取算法、固定生成一批词，再自动依次搜索的阶段。更准确的名称是通过 MCP 接口实现的 DCI 风格历史轨迹查询 pilot，协议标识为 `initial-keyword-probe-v1`，不等于完整复现 Lite 或 DR-DCI。

```text
当前任务描述、公开输入、工具说明、A3提示
  → 同一Codex agent理解任务，并选择搜索字符串
  → dci_search(query, 可选过滤/数量/分页)
  → 服务扫描冻结构建库，执行连续字面匹配
  → 返回命中行、局部文本、来源和继续查询坐标
  → agent自己选择dci_read的episode与窗口
  → 原始文本回到同一agent上下文
  → agent选择采用、拒绝、再查，或继续执行当前任务
```

A3 的实际提示要求：理解题目并简要查看输入后，在实质解题前至少做一次相关关键词搜索；有候选时至少读一个相关窗口并判断是否适用；之后可自主再次搜索、拒绝经验或继续求解，也明确禁止为凑调用量而检索。它没有规定关键词个数、自动分解算法、关键词词表、操作/工具/约束模板或遇阻必查状态机。提示源码（历史文件：`experiment/benchmarks/wildclaw_bench/src/experiment_protocol.py:27`，不在精简发布中）、调用入口（历史文件：`experiment/benchmarks/wildclaw_bench/scripts/run_wildclaw.py:125`，不在精简发布中）

因此，关键词由模型根据当时可见上下文生成。日志能确认发出的 query 和后续动作；没有完整可观察依据时，不推断模型内部为何选某个词，也不把事后合理解释当作当时的生成算法。

### 搜索服务的规则

| 项目 | 冻结实现 |
|---|---|
| 语料 | 36个构建任务、432条历史episode的规范化文本；跨六类可查，非测试轨迹 |
| 匹配 | 整个 query 作为一个连续字面子串；默认大小写不敏感 |
| 空格/竖线 | 空格保留在短语内；`a b`不表示AND，`a\|b`不表示OR |
| 语义 | 无embedding、BM25、自动别名或相关性重排 |
| 范围 | 可选task_id和model精确过滤；默认扫描全部允许的构建episode |
| 顺序 | episode ID排序，再按文件行号顺序；不是相关性排序 |
| 单行 | 每行只找首个匹配，截取附近最多1200字符的预览；长行后续出现不单独列出 |
| 数量与预算 | limit默认20，允许1–100；实际返回还受单次12000字节及剩余总预算影响 |
| 分页 | next_cursor可继续扫描；cursor定位episode和行，不能逐个枚举同一行的所有匹配 |
| 阅读 | agent指定episode、起始行、行数和可选字符偏移；默认40行，最大100行，仍受字节预算裁剪 |

这里的“窗口”是一次读取的文件片段，不是模型的context window，也不默认等于整条trajectory。起点和请求长度由agent填写，省略才用默认值；不是服务自动按完整操作切块。实际长度为请求范围、文件剩余内容及剩余字节预算共同决定。单次12000 B中预留768 B，items最多11232 B，而且每行重复来源信息也占这个额度。短轨迹可能一次读完，长轨迹须按返回坐标续读；服务不会自动读取余下全文。

搜索实现（历史文件：`experiment/benchmarks/wildclaw_bench/src/dci_tools.py:340`，不在精简发布中）、读取实现（历史文件：`experiment/benchmarks/wildclaw_bench/src/dci_tools.py:383`，不在精简发布中）、工具说明（历史文件：`experiment/benchmarks/wildclaw_bench/src/dci_tools.py:437`，不在精简发布中）。源码内部使用 `re.escape(query)` 后再编译正则，是为了实现字面匹配；不能因为代码调用了正则库，就把工具说成支持用户正则查询。

### 一个真实的调用序列

`formal-22-a3`的4次调用均能对应到solver事件：

| 次序 | 模型实际提交 | 实际效果 |
|---|---|---|
| 1 | `dci_search(query="product poster", limit=5)` | 零命中；两词被作为完整连续短语 |
| 2 | `dci_search(query="poster", limit=6)` | 首屏主要命中论文海报链接等内容 |
| 3 | `dci_search(query="ImageDraw", limit=5)` | 找到repo_to_slides轨迹中的Pillow绘图脚本 |
| 4 | `dci_read(episode_id="0fd95daef4aca24ff8b1b3a4", start_line=1436, line_count=12)` | 实际返回1436–1439行，字节预算使请求的12行未全部返回 |

这是模型从任务题材词转到具体工具词的可见例子，不是系统自动把`poster`扩展为`ImageDraw`。找到绘图代码也不等于完整海报任务因此成功；采用与分数归因仍需独立证据。原始服务日志（历史文件：`experiment/benchmarks/wildclaw_bench/runs/formal/formal-22-a3/dci.audit.jsonl`，不在精简发布中）、已完成检索审计（历史文件：`experiment/benchmarks/wildclaw_bench/reports/analysis/retrieval_audit.md`，不在精简发布中）

### “稳定”要分三件事

1. **程序返回是否可复现：有明确支持。** 固定语料、query、过滤、cursor、limit及剩余预算后，服务基本是确定性程序。此前审计已重建84次服务返回，84/84与保存的result hash一致；今天没有再次运行求解来验证。
2. **命中是否稳定有用：没有保证。** 同一个无关参考文献也可以被稳定地返回第一名。词选得不对、长短语过严、哈希顺序偏向某些episode、窗口未覆盖命中，都可能稳定地产生差结果。
3. **整次agent过程是否可重复：首轮无法估计。** 同一任务每个条件只跑一次，生成seed没有固定；模型选词、读窗、采用和执行路径可能改变。划分seed固定只保证数据划分规则，不保证模型生成完全重复。冻结配置生成（历史文件：`experiment/benchmarks/wildclaw_bench/src/experiment_protocol.py:106`，不在精简发布中）

**日志归属补充及后续更正（2026-09-09）：**最初只核对主solver MCP完成事件时，84次服务请求中能对应82次（52次搜索、30次读取）；另外两条为`formal-33-a3`的call3（`Link-a-Pix`, limit=3）和`formal-06-a3`的call6（`poster paper PDF figure`, limit=3）。当时没有直接对应记录，因此在首次归属核查（历史文件：`docs/research_notes/retrieval_followup/query_provenance_check_20260909.json`，不在精简发布中）中保留为来源未确认。

V3执行前后的完整会话审计已找到两条请求的子代理来源，工具参数与原始响应UTF-8哈希均与服务账本一致：`formal-06-a3`的`paper_factcheck`子会话第29行对应服务第7行/call6；`formal-33-a3`的`independent_check`子会话第24行对应服务第4行/call3。因此当前结论为**82次主线程＋2次子线程＝84次已记录模型调用**，包含54次搜索和30次读取。主线程零命中18/52与全模型会话零命中20/54是不同范围；跨线程合并的先后顺序也不等于单一agent的一条推理链。两条子查询都为零命中、零证据字节，不能作为收益证据。完整身份、路径、事件行、哈希和fork历史去重方法见新增子代理归属证据（历史文件：`experiment/benchmarks/wildclaw_bench/reports/dci_terminal/helper_activity.json`，不在精简发布中）。原始运行、首次审计JSON和成绩保持原样。

## 2. Lite 和 DR-DCI 的检索技术

### DCI-Agent-Lite

Lite预先将可见corpus导出成文档目录。agent根据问题直接生成bash命令，用`rg`等程序找文件、匹配行和上下文，再决定读哪个文件、读哪一段。关键词、正则、管道、文件范围由agent选择；底层程序负责执行。工具结果进入同一上下文，agent可以继续搜索或回答。

它没有独立的“query→向量/BM25→topK”入口。默认benchmark提示较短；IR专用提示才明确提出多角度搜索、读后筛选、补齐证据和最终相关性排序。IR要求的最多20篇是**最终文档列表**，不是每次检索的topK；Python解析和评分也不是候选重排器。[默认与IR提示](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_bcplus_eval.py#L363-L405)

与我们相同的是“模型选搜索方式，工具返回，模型继续决策”。与我们不同的是，Lite允许agent通过终端组合搜索，而首轮书库只能通过字面搜索API访问。这限制了组合条件、文件级筛选和连续正文阅读的自由度。我们仍然有持续agent循环，不能说首轮根本没有交互式检索。[Pi实际循环](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/agent/src/agent-loop.ts#L190-L228)

### DR-DCI

```text
agent生成query和topK
  → pull调用隐藏语料库的检索服务
  → query被embedding模型编码，FAISS查询预先建好的文档向量
     （也可以使用独立的BM25后端）
  → 返回排序后的docid / path / score
  → pull按来源路径去重，取正文并放进本地工作区
  → 向agent给新增/已有数量及简短导航
  → agent在工作区所有文件中rg/read/比较
  → 新线索出现时，自主决定是否再次pull
```

主配置使用Qwen3-Embedding-8B与FAISS，单query，由agent在300–600间选择topK；主预览展示前20个新文件的排名、标题和路径。**请求300个候选、实际新增20个、预览20个和实际读了几个，是四个不同数量。** 已有文件不保证已被阅读；剔除旧文件后也不自动补足topK个新文件。[pull实现](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L783-L865)、[主配置](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_full830_dynamic_pull_root_flat_openai_high_l3_300turn_parallel30.sh#L59-L87)

embedding模型负责将文字编码成向量，FAISS负责向量检索；它们不负责像solver一样规划下一步。候选进入工作区也不表示自动把全文注入上下文。持久文件让agent可以隔几轮再回来读，模型当前上下文只需保留必要线索。

可选多query由agent提交，各自取topK，再按query顺序追加；没有自动同义词生成或跨query全局融合重排。去重键为来源路径，不能合并多模型反复出现的同段内容。SQLite FTS5与Pyserini/Lucene是独立BM25后端；SQLite会抽词后OR组合，不能将输入的引号和操作符自动视为严格字段Boolean。[SQLite转换](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/tools/dense_retriever/sqlite_bm25_server.py#L145-L158)

工作区持久、上下文压缩、失败恢复是不同机制。L3常规清除较旧工具返回但保留文件；论文的workspace-preserving reset对应另行rescue流程，不是每次零命中就自动重置。更详细的固定版本、窗口、后端和默认值见[DR源码审查](dr_dci_review.md)。

## 3. SIEVE：是structured search，但要讲清作用范围

用户理解的大方向正确。SIEVE让agent写字段Boolean表达式，限定词在哪些字段出现、哪些字段排除某些词；随后独立ranker给合格候选排序，卡片展示结构和片段，agent选择列出的章节读取。它同时设计了**筛选、排序、预览、读取**，不是只增加title/body过滤。[论文§3](https://arxiv.org/html/2608.02751v3#S3)

例如以下是解释用表达式，不是论文原始某次运行：

```text
"heatwave plan"[title]
AND "school closure"[body]
AND NOT "workplace safety"[title]
```

这限制文字出现的位置，不等于自动理解网页是否真正满足所有语义条件。零命中时系统有明确的放宽/覆盖率回退，放宽候选也不能当作满足了全部条件。

**section字段匹配与“读取某个具名section”要区分。** 本次额外核查的Lucene schema以整篇网页为一个检索记录，section不是独立子文档；多个section被折叠进该记录的字段。因此，不能推断任意多个条件都被保证绑定在同一具名章节内部。具名章节选择发生在后续fetch。原框架所提的事件内AND，比简单照搬这个doc级schema多了一项范围设计。[固定schema](https://github.com/ielab/skim-search-agent/blob/4a640296770c39b8274e7678c6de844daea9541c/agent_search/retrievers/structural/lucene/schema.py#L4-L19)

### 在trajectory中的适配提案

与其把整条episode当一篇无结构网页，更适合两级导航：episode保留任务/模型/来源，事件层保留输入、工具调用、参数/代码、关联返回、错误、产物及后续检查。

| 网页结构 | 我们的候选映射 | 用途 |
|---|---|---|
| 文档标题/元数据 | 历史任务概述、来源模型、工具环境；未知字段显式缺失 | 定位来源和大致适用条件 |
| 章节及其入口 | 带稳定ID的动作—观察事件块 | 搜索后能直接进入相关步骤 |
| 正文 | 命令、代码、工具返回及必要上下文 | 保留原始方法证据 |
| 日期/其他精确字段 | 若原始记录确有，使用工具版本、输出格式、退出码等 | 条件过滤或提示；不凭空推导 |

假设当前子目标是“裁切图片后导出指定尺寸”，可先在action/code字段中找`ImageOps.fit`或裁剪接口，再读与之关联的真实返回、图像尺寸检查。API名出现在环境说明或论文摘要中时，不和真正调用混为一谈。不要只因为整个episode末尾写了成功，就给其中每个动作标成功；也不要用`NOT error`删除失败后成功恢复的宝贵步骤。

事件块优先按原始tool_call_id关联，不能把相邻两行直接认定为一对。若方法跨多个工具调用，可以继续读前提与后续验证；缺验证时标未知。先用原本已有结构建立可逆导航，LLM概括的“通用操作卡”另算A4表示变化。

## 4. IRCoT：核心简单，但有明确的交替控制

IRCoT论文的过程是：完整问题先做BM25检索；LLM结合问题、已有段落和先前推理生成下一句；用新句子形成下一次检索；累计段落，遇回答标记或步数上限后，由reader基于证据作答。**通常不是等搜索失败才检索，而是把生成和检索交替编排。** 中间发现能改变下一次搜索对象，这是重点。[论文§3](https://aclanthology.org/2023.acl-long.557.pdf)

官方代码还会只保留新生成的第一句；查询构造路径会先过滤部分纯推断连接句，再取最后一句。论文的8步设置和当前源码的默认句数上限不同，不能把论文数值当作所有配置的硬上限。[查询选择](https://github.com/StonyBrookNLP/ircot/blob/3c1820f698eea5eeddb4fba3c56b64c961e063e4/commaqa/inference/ircot.py#L347-L353)、[单句截取](https://github.com/StonyBrookNLP/ircot/blob/3c1820f698eea5eeddb4fba3c56b64c961e063e4/commaqa/inference/ircot.py#L845-L849)

它与DR-DCI共享“证据改变后续搜索”的思路，但机制不同：IRCoT规定句子级交替及段落累计，DR-DCI让agent自主选择pull、本地搜索、阅读、比较和作答。其贡献分别更偏向查询/生成的编排，以及检索构造可持续交互的文件工作区。

对我们的启发是用一个简短、明确的未解决子目标更新查询。例如图片已生成，但尺寸检查失败，下一次应找尺寸变换和验证方法；不必重复搜索整道题的题材词。这里采用显式状态摘要是适配方案，不要求访问隐藏思维链，也不是每个动作后强制多查一次。

## 5. ITER：谁生成query，训练什么，为什么可能有用

main question可对应我们的完整task描述；sub-query同样由求解agent生成。需要区分两种trajectory：我们书库里的是其他任务的解题过程；ITER训练数据主要是搜索agent的查询、候选返回、打开文档及读后反应。后者用来教检索器如何排序，不只是把前者再存一次。

ITER训练一个dense retriever。默认query编码输入为`Q + 当前q + 先前queries`；文档访问行为还用于构造与时间有关的训练信号：当前读到且判断有用的文档为正例，此前读过且有用的是冗余负例，此前读过但无用的是难负例，当前返回且全程未读的只作为较弱负例。主方法不是把所有interaction全文直接拼进输入。[论文§3–4](https://arxiv.org/html/2608.27912v1#S4)

因此，它的贡献同时涉及**查询编码所接收的状态**和**由交互过程构造的相关性/冗余监督**。首轮Codex本来就能看到之前的工具结果；缺少的是将这些信息明确转为查询策略、候选阅读记录或检索器特征。不能把ITER概括为“让agent第一次拥有历史记忆”。本次没有取得论文声明仓库的可访问源码，ITER相关结论限于论文核查。

### 在多步骤任务中的适配

例如同一个海报任务分为排版、字体、裁切、导出检查。排版步骤已解决后，下一轮字体问题需要新的方法证据。仅按整道任务相似性检索，可能持续返回同一份排版内容；结合当前子目标和已读步骤，可以优先显示尚未消费的字体处理方法。

但是，一条历史trajectory中可能同时包含排版、字体和导出检查。**读过排版段，不等于整条trajectory都已无用。** 阅读状态和新颖性应落实到事件/片段，而不是将整个episode永久排除。不同模型共享的方法可以折叠，但失败与恢复的分叉仍需保留。曾经读过的证据在新子目标下也可能重新有价值；“重复”应针对尚未解决的需求判断。

候选记录宜拆成两部分：暴露历史（返回、阅读及坐标）和当前决策（采用、拒绝、待查及子目标）。新候选优先显示，旧候选保留入口，不把未读自动等同于无用。这里是我们的轨迹适配推论，论文在知识文档上的收益不能直接迁移为本项目预期分数。

## 6. 与Agentic-R做什么对比才有意义

两个论文关于历史query的结果方向确实不同，但不构成同一设置下的互相推翻。Agentic-R默认`Q + 当前q`，其加入历史的消融在平均成绩上变差；ITER的训练与评测中加入先前query更好。训练目标、模型、语料和查询分布不同。历史可以帮助排除已解决方向，也可能稀释当前需求。[Agentic-R附录C.2](https://arxiv.org/html/2601.11888v1#A3.SS2)、[ITER§7.1](https://arxiv.org/html/2608.27912v1#S7.SS1)

值得做对比，但先区分历史在哪一层起作用：

| 层次 | 可以提出的对照 | 需要固定什么 |
|---|---|---|
| agent生成query | 现有自主策略 vs 明确围绕子目标/缺口写短query；原agent本来就有工具历史 | 同一搜索语义、书库、solver、读取及总预算 |
| retriever编码输入 | `q` vs `Q+q` vs `Q+q+有界先前queries` | 同一合适的检索后端与候选单位；明确是固定模型输入消融，还是各自重新训练 |
| 结果展示 | 原顺序 vs 优先尚未读的相关片段，旧结果保留入口 | 相同检索原始候选、展示名额和字节预算；排序与自动补充新候选要分开 |
| retriever学习 | 不同交互监督、冗余负例与任务推进标签 | 训练集和训练预算；这是另一个更大的实验，不能由简单接口修改代替 |

**不能把`Q + q + history`直接拼入当前dci_search。** 当前工具会把它当成一个巨大的连续字面串，这测到的是短语越来越难命中，不是ITER关于交互上下文的假设。若研究关键词生成，应让模型利用状态生成一个或多个有明确语义的短query；若研究检索器输入，应先定义对应的BM25/向量/结构检索条件。

测量要区分新召回的相关方法、实际阅读完整性、适用性、采用及输出验证、最终得分和成本。先在构建集内部按family隔离选择方案；已分析24题做回归，确认性结论另需未参与调参的holdout。本次只提出比较设计，没有训练检索器或运行任何新solver/grader。

## 7. 从源码里最值得借鉴的细节

| 具体技巧 | 可如何使用 | 不应照搬的部分 |
|---|---|---|
| Lite让agent组合多模式和局部文件搜索 | 明确phrase/OR/AND与作用范围，先定位候选再读正文 | 自由shell不能自动保证合理选词；书库访问边界仍需保留 |
| DR将候选物化与上下文暴露分开 | 暂存更多可访问候选，只显示短导航，正文按需读取 | 不直接把300–600篇配置搬到432条轨迹的小库 |
| DR返回新增、已有、缺失数量 | 识别没有新材料的重复请求；旧材料可以继续读 | 来源路径相同不等于信息重复，已存在也不等于已读 |
| DR提供ranked/shuffled/hidden预览 | 借鉴其稳定打乱，隔离“排名提示”的作用 | 保持同一候选池和预算；不要把展示对照说成重排模型提升 |
| 窗口与续读参数 | 搜索命中携带可恢复的位置，读动作及关联返回 | DR长行定位从命令猜关键词，仍可能跳错；应直接传精确命中坐标 |
| IRCoT限制每轮生成跨度 | 用简短当前缺口更新query，及时接回证据 | 不需要复制隐藏推理，也不应把英文连接词过滤规则当通用语义判定 |

DR预览模式见[buildAgentVisiblePreview](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L97-L115)。长行处理见[truncate.ts](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/truncate.ts#L135-L214)。其JS字符窗口以UTF-16索引计数，不能直接套到Python字符索引；重排行号后也应保留原文坐标映射。这些工程细节影响引用能否真正读到对应内容。

优先讨论查询/读取契约及按事件记录的使用策略，再分别考虑排序、经验表示和动态扩库。改善服务的可复现性、候选相关性和最终任务成功是三个不同目标，后续结论应分别报告。

2026-09-09后续讨论进一步将**忠实的Lite默认终端检索适配基线**放到新增机制之前，具体范围见[原框架第13.8节](../../../reports/DCI_upgrade_report.md)。先复核上游基线，再决定是否需要上述改进；本次没有开始新实验。
