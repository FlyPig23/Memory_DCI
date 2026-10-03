# DR-DCI：动态候选工作区、局部检索与证据核验

本笔记只做文献和代码研究，未安装上游依赖、执行 setup、启动检索服务或运行模型实验。GitHub API 确认的当前 main HEAD 为 `0d0410f3c2b98fb33145adc250a09fded028cd3c`，提交时间 2026-06-16；本次按该版本做稀疏只读快照。论文为 [arXiv:2606.14885v1](https://arxiv.org/abs/2606.14885v1)，当前版本记录仅列 v1。README 中“论文链接待公开”的文字已经落后于 arXiv，不能据此判断论文未公开。文件哈希和固定来源见 [dr_dci_sources.json](dr_dci_sources.json)。

## 核心机制，以及与本轮实验的关系

DR-DCI 的改动是把检索做成 agent 可调用的工作区扩张操作：先从隐藏大库拉取候选文档，落到可持续积累的本地目录，再让 agent 用跨文档搜索和局部阅读核验条件。新线索出现时可以再次拉取。它不是固定一次 top-k 后直接回答；同时也不能说它排斥检索或不属于任何广义的 agentic RAG。论文把检索用于候选发现，把 DCI 用于后续证据处理。[论文 §3.1–3.3](https://arxiv.org/html/2606.14885v1#S3.SS1)

对我们最有价值的区别是：`topK` 决定落盘候选规模，不等于一次塞进模型上下文的文本量。模型可以先看候选导航、再按多个条件交叉筛选，最后读取真正需要的片段。我们首轮的字面串搜索直接从整本轨迹库返回若干行，缺少这个可反复操作的候选工作区。把论文中的文档替换成经验轨迹还需要自行定义可迁移单元与适用条件；上游没有实现面向 WildClawBench 的成功经验抽取或轨迹采用判断。

## 查询与候选：已经实装什么

| 项目 | 已核实的实现 | 不能据此推断 |
|---|---|---|
| 主实验入口 | `read,bash,pull`；root-flat 工作区；rank-aware 模式；单 query；agent 选 topK 300–600；300 turns；L3 | 并非当前每次拉取自动多查询，也不是 topK 只取3或5 |
| 稠密后端 | 主启动脚本使用 Qwen3-Embedding-8B；FAISS 服务编码 query、搜索向量并按后端结果序返回 docid/path/score | FAISS 自身不是语义模型，也没有自动选择最适合当前任务的后端 |
| BM25 | 两个独立可运行服务：SQLite FTS5、Pyserini/Lucene；都接受 pull 的 `/retrieve` 请求 | 不是给 FAISS 传 `model_name=bm25`；FAISS 服务明确拒绝该值 |
| 多查询 | 旧接口支持 `queries` 数组；rank-aware 可选 `queryVariants`，逐条请求后端 | 没有发现自动生成别名、自动查询扩展、跨查询 RRF/MMR 融合 |
| 去重 | 当前调用共享 `createdSet`；主 disclosed 模式还读过往 managed paths；键为规范化源 `doc_path` | 不是语义去重、内容哈希去重，也不是同任务12个模型轨迹的家族去重 |
| 排名预览 | 主配置前20个新文档的序号、路径与标题；全文在目录里 | 没有主工具层二次相关性重排；rank 只是检索导航信号 |
| 累积工作区 | 文件持续保留；新拉取跳过已管理路径；记录新增、缺失和已存在数量 | 没有发现主配置按文档数自动淘汰或固定大小缓存；“局部”不是绝对容量上限 |

主入口参数见 [正式脚本 L59–87](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_full830_dynamic_pull_root_flat_openai_high_l3_300turn_parallel30.sh#L59-L87)。稠密服务见 [后端启动 L11–16](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/start_qwen3emb8b_retriever.sh#L11-L16)、[编码与检索 L436–483](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/tools/dense_retriever/faiss_searcher.py#L436-L483)、[拒绝 BM25 L266–270](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/tools/dense_retriever/faiss_searcher.py#L266-L270)。

BM25 的细节值得区分：SQLite 先用 Unicode `\w` 抽取词项、转小写、删除单字符和重复词，最多64项并为 OR 查询；FTS5 用 title/text 两字段，BM25 权重2:1，按分数取前 k。它没有中文专用分词或显式 n-gram；不能把传入的引号当成它会完整保留短语语义。Pyserini 则支持原 query 或 `sqlite_or` 转换，再调用 LuceneSearcher's BM25。[SQLite 查询转换 L145–158](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/tools/dense_retriever/sqlite_bm25_server.py#L145-L158)、[FTS5 配置 L186–190](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/tools/dense_retriever/sqlite_bm25_server.py#L186-L190)、[排序 L358–374](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/tools/dense_retriever/sqlite_bm25_server.py#L358-L374)、[Pyserini L68–84](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/tools/dense_retriever/pyserini_bm25_server.py#L68-L84)。

多查询只做 trim 后字符串集合去重。主 query 与 variants 来自 agent 输入，不由检索器自动生成；开放 variants 的配置按 query 顺序获取各自 topK。工具将各查询新文档依次追加到预览，再截前20项，没有按跨查询分数融合，因此早查询可能占满预览。主脚本 `--pull-max-queries 1` 不开放 variants 字段。配置名也要小心：variants 路径的内部切片上限作用于“额外变体”，主 query 另算，并不总是总 query 数上限。[接口 L632–672](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L632-L672)、[执行 L783–809](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L783-L809)、[逐查询累积 L829–865](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L829-L865)。

topK 是请求后端的数量，不是保证新增数量。旧文档和缺失文件会被剔除，没有在同一次调用里继续向后补足 k 个新文档；因此多次用同一 query 拉取可得到很少甚至0新增。主配置的跨调用去重只按同一源路径生效，不会识别两条不同文件里的相同操作。对我们的多模型轨迹库，这个差异尤其重要。[源路径判定 L494–509](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L494-L509)、[跨轮管理路径 L383–405](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L383-L405)、[disclosed 模式分支 L815–818](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L815-L818)。

## 阅读与采用：工具保障和提示要求的边界

**已实现的阅读能力。** `read` 支持1起始行号窗口、字符窗口、字节窗口；行模式受2000行/10KiB约束，字符窗口默认4096字符。字符分支按字符数裁剪，不应把其上限数值误称为严格10KiB字节上限。首行超长时自动返回一个起始字符窗口，避免空结果；返回下一行/字符/字节偏移。`bash` 的超长 grep/rg 行会转换成局部片段及 read 建议。但长行命中位置是根据命令文本启发式寻找，不是对任意复杂正则的精确定位器。[read 参数 L25–42](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/read.ts#L25-L42)、[窗口执行 L263–354](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/read.ts#L263-L354)、[长行定位与续读提示 L161–214](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/truncate.ts#L161-L214)。

**已实现的文本物化。** 普通文本优先硬链接；主配置启用1200字符宽的单行重排，避免整篇 OCR 文档成为一行。需要重排的文本会另写工作区文件，不是对原文件直接改写。经 `/document` 获取的内容也可写入目录。转换后工作区行号不再等于原文本行号，移植到我们的引用体系时仍需保存坐标映射。[物化 L293–341](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L293-L341)、[主配置 L44–49](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_full830_dynamic_pull_root_flat_openai_high_l3_300turn_parallel30.sh#L44-L49)。

**提示层要求，不是程序自动完成的验证。** 主提示要求每次拉取后先停下来检索/阅读当前目录；优先短线索、实体、标题、日期；按预览筛选后读正文；输出截断就续读；只在获得新线索后再 pull；最终引用实际读过的文件。这比只要求“一次 search + 一次 read”更具体。可是主动态 pull 入口没有检查 agent 是否确实核验了每个条件，也没有“任何失败即自动再 pull”的状态机。[工作流提示 L1049–1061](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_bcplus_eval.py#L1049-L1061)。

仓库另有 `--research-rounds` 实验模式及 verifier 门控，其默认值0，主正式脚本没有启用。不能把这个额外模式算作所有 DR-DCI 动态拉取的默认自动验证机制。[参数定义 L240–263](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_bcplus_eval.py#L240-L263)。

## 上下文管理与失败恢复是两件事

常规 L3 已实装：单工具文本截到20000字符；历史工具返回累计超过240000字符后，将较旧返回内容清为空占位，保留最近12个 assistant turn，保持工具调用配对结构；这一步不调用 LLM 生成总结，也不删除工作区文档。L3 的 in-loop LLM compaction 为 false。[L3 参数 L182–190](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/settings-manager.ts#L182-L190)、[触发 L518–526](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/agent-session.ts#L518-L526)、[清理逻辑 L14–52](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/compaction/micro-compact.ts#L14-L52)。

论文另报告可选的 workspace-preserving reset：在低置信且表现为缺证据/弃答的最终答复后，保留工作区，用新的 DCI 会话重新调查。论文配置为 confidence≤70 且匹配弃答模式；它不是每次搜索失败就自动重试检索。[论文 §3.4](https://arxiv.org/html/2606.14885v1#S3.SS4)

代码里对应的是完成实验后的独立 rescue 脚本，提供 `reflection` 和 `clean-dci` 两种模式。`clean-dci` 仅给新问题与现存工作区，不传旧最终答复；其 prompt 要求不再 pull。选择条件实现为 `confidence < max_confidence` 加可选弃答正则，默认阈值60、默认 reflection；因此论文“≤70”的配置不等于脚本默认值，需要显式参数才能对应。未发现主 full830 脚本内自动执行该后处理。[模式 L235–246](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_confidence_reflection_rescue.py#L235-L246)、[工作区拷贝与新调用 L301–328](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_confidence_reflection_rescue.py#L301-L328)、[选择逻辑 L460–478](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_confidence_reflection_rescue.py#L460-L478)、[默认参数 L569–581](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/scripts/bcplus_eval/run_confidence_reflection_rescue.py#L569-L581)。

## 环境约束与不能直接移植的部分

pull 会拒绝工作区与源库互相嵌套，物化路径也做规范化和目录边界检查；但本次看到的主入口并不等于完整 OS 沙箱。`read` 接受可访问的绝对路径，默认 `bash` 通过本地 shell 执行；网络和跨文档搜索阻断是环境变量控制的命令模式检查。不能仅凭论文所说源库不可变，就声称克隆后运行具备只读挂载、网络隔离或对任意命令的强制约束。我们的服务器实验边界仍应独立保留。[pull 路径约束 L776–780](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/pull.ts#L776-L780)、[read 路径 L54–66](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/path-utils.ts#L54-L66)、[shell 执行 L376–390](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/bash.ts#L376-L390)、[可选命令限制 L51–94](https://github.com/EigenTom/DR-DCI/blob/0d0410f3c2b98fb33145adc250a09fded028cd3c/pi-mono/packages/coding-agent/src/core/tools/bash.ts#L51-L94)。

本次对我们的启发属于设计推论，尚未经实验验证：

1. 先按“当前操作与未满足条件”发现一小组候选轨迹，再在候选内找动作、反馈和失败处理；不要直接把任务实体名称当唯一查询。
2. 查询、后端排名、工作区搜索、读取和采用分开记录。BM25、向量召回或多 query 都可作为候选层变化，不等于已经解决经验适用性。
3. 多模型同题轨迹需要额外的 task/family/近重复处理；上游 doc_path 去重解决不了这种重复。
4. 优先借鉴有用的窗口返回、命中附近续读和“先读后再查”约束。不要直接照搬300–600篇文档预算到仅432条轨迹的库。
5. 区分“源库里有没有经验”“候选工作区里有没有”“模型有没有读到”“是否被正确采用”。论文自己的案例也包含工作区已召回证据但最终消歧失败的情况，召回充分不代表回答正确。[论文 A.11 / Table 16](https://arxiv.org/html/2606.14885v1#A1.SS11)

以上仅为后续讨论材料，没有改变本轮冻结实现、评分或测试集用途。
