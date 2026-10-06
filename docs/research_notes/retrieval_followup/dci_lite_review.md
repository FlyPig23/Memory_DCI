# DCI-Agent-Lite：哪些能力已有，哪些仍要我们设计

**上游已经提供“模型自由组合终端检索 → 读取证据 → 根据新证据继续搜索”的运行基础；但没有替我们实现一个自动改写查询、去噪去重、排序、适配历史操作并保证采纳正确的检索系统。** 其中最容易混淆的是 IR 专用提示词：它明确要求多角度搜索、读候选、反思缺口和相关性排序；默认 benchmark 提示词没有这么完整的要求，相关排序也由 agent 完成。

本次通过 GitHub API 核实当前 HEAD 为 **`271f37e71f053bf0c99c05ce6d2fb53b841d922e`**，与项目冻结 checkout 相同。9 份核心文件另从该 commit 下载并逐字节比较，与本地一致。上游 Pi 依赖使用移动分支 `codex/context-management-ablation`；本次核实分支 HEAD 为 **`a6be5eb4cce278de31ac05792af3dfc0883215dc`**，并阅读7份运行与工具源码。Pi 分支当前状态不能反推论文每次历史运行使用的依赖 commit。时间、API 地址、文件 SHA256 和比较结果见 [source snapshot](dci_lite_source_snapshot.json)。只读下载了源码，没有更新 vendor checkout、执行仓库脚本或运行模型实验。

本文把“具体实现”限定为源码里有直接执行的逻辑；“提示词引导”表示模型被要求这样做，不能当作必然发生；“没有发现”限于本次检查的 Lite 默认/IR 接入及所引用 Pi 源码，不代表所有 Pi 插件或其他 DCI 系统都没有。

## 一、查询和候选

| 关注点 | 具体实现 | 提示词引导 | 没有发现或需要另设计 |
|---|---|---|---|
| 以操作、机制、约束为关键词 | agent 可以通过 bash 自行运行检索命令 | IR 要求多样且定向的关键词 | 没有把任务自动解析成操作词的专用模块；也没有明确要求“题名词必须转操作词” |
| 多词、别名、中英文 | 原生 bash 接受完整命令，不会像本轮 MCP 一样将整个输入强制 `re.escape`；可使用终端工具已有的正则、字面模式、管道和文件范围 | IR 的多角度查询允许 agent 自行换词 | 没有统一的 AND/OR API、自动别名词表、跨语言扩展或自动查询编译器 |
| 候选去噪 | agent 可以通过命令筛字段、筛路径、查看局部正文 | IR 要求排除仅宽泛相关、不能支撑问题的候选 | 没有针对轨迹中环境说明、目录链接、模型清单、重复 API JSON 的自动去噪器 |
| 候选去重 | bash 允许 agent 自行调用去重命令；文档导出时有同目标路径同文本复用 | 没有发现明确的候选去重指令 | 没有按任务/episode 的近重复合并或每来源配额；导出时的文件冲突处理不是检索去重 |
| 候选排序 | agent 能自由读取与比较候选；IR 结果会被解析和计算 NDCG | IR 明确要求 agent 将最终文档列表按相关性排序 | 没有固定 BM25、向量索引、独立 reranker 或统一相关性打分器；普通 `rg` 输出顺序不等于相关性顺序 |

代码依据：默认工具参数为 `read,bash`，并被传给 Pi，见 [runner 工具接入](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/pi_rpc_runner.py#L181-L214)、[默认值](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/pi_rpc_runner.py#L1379-L1383)。Pi bash 将 command 交给 shell，见 [bash.ts](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/coding-agent/src/core/tools/bash.ts#L69-L83)。上游没有一个先接收关键词再统一返回 top-k 的自建搜索接口；搜索程序及参数主要由 agent 决定。

**多词能力的准确说法是“可以由 agent 组合”，不是“系统自动理解多词”。** 例如 agent 可以选择多个字面模式搜别名，用正则限定单词或上下文，再用第二次过滤处理额外约束。即使有原生 shell，agent 仍可能选错词、只看首屏或误用正则。命令可组合与策略有效是两件事。

导出的 BrowseComp 文档按域名/标题命名，文本保持原内容，见 [export_bc_plus_docs.py](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/export_bc_plus_docs.py#L125-L143)。`unique_path` 只是处理目标路径已存在及同文本情况，见 [第54–82行](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/export_bc_plus_docs.py#L54-L82)。BRIGHT 保留文档ID路径与内容，见 [export_bright_docs.py](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/export_bright_docs.py#L42-L54)。不能把这些转换描述成清洗失败轨迹、抽取成功经验或跨文档去重。

IR 输出处理仅解析 agent 的最终列表、规范路径并排除 query 对应文档，随后计算 NDCG，未见候选内容去重或最终列表去重，见 [结果解析与评分](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_bcplus_eval.py#L301-L360)。因此“上游已经有系统层自动排重/重排”是不准确的。

## 二、默认 prompt 与 IR prompt 必须区分

[默认 benchmark prompt](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_bcplus_eval.py#L363-L370) 只指定问题、本地 corpus、禁止联网并要求使用 `rg`。它没有明确规定分解约束、扩展同义词、读后反思、文档排序或最低搜索次数。

[IR prompt](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_bcplus_eval.py#L373-L405) 则要求：

- 一次响应里并行进行多个搜索，使用不同的定向关键词。
- 每轮检查仍缺什么证据，继续搜索其余角度，不在找到少数文档时立即停止。
- 读过候选再决定纳入，只保留直接回答问题或提供必要证据的文档。
- 最终由 agent 按相关性列出最多20个文档，并给解释、答案与置信度。

这些是**提示层策略**。IR 对“每轮补缺”的要求值得借鉴，但没有一个程序状态机检查 agent 是否真的完成这些动作。Lite 的 `--enable-ir` 默认关闭，[参数定义](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_bcplus_eval.py#L137-L144) 和 [分支选择](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_bcplus_eval.py#L1456-L1467) 可验证。BRIGHT 示例显式启用 IR，[run_bio.sh](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bright/run_bio.sh#L22-L36)；BrowseComp L3 示例没有，[run_L3.sh](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/scripts/bcplus_eval/run_L3.sh#L22-L35)。不能把 IR 专用的较强策略说成所有上游运行的默认设置。

仓库中的 `prompts/system_prompt.txt` 也是可选文件。runner 未指定时使用 Pi 动态生成的 system prompt，见 [参数说明](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/pi_rpc_runner.py#L1389-L1404)。当前 Pi 默认提示依据可用工具与上下文构建，包含文件探索等一般要求，见 [system-prompt.ts](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/coding-agent/src/core/system-prompt.ts#L83-L125)。本次没有实际启动 Pi，因此不声称已经复现某一环境最终拼装的完整 prompt。

## 三、读取、采用与再查询

| 关注点 | 具体实现 | 提示词引导 | 没有发现或需要另设计 |
|---|---|---|---|
| 命中后的上下文读取 | `read(path, offset, limit)`；bash 可按 agent 选择命令读取和处理文本；图像读取也有支持 | read 工具要求大文件按 offset 继续；IR 要求认真阅读候选 | 没有自动 `read_hit`、以关键词坐标为中心装配上下文或完整事件读取保证 |
| 动作和返回结果形成连续反馈 | Pi 确实执行模型工具调用，将结果加入同一上下文，然后进入下一轮 | agent 自主利用结果决定下一步 | 没有把历史轨迹自动整理成“问题→行动→结果→验证”经验单元 |
| 判断是否采用历史方法 | 模型拥有原始证据，可以比较并拒绝 | IR 对证据相关性有明确要求 | 没有面向 WildClawBench 的操作可迁移性判定器、采纳记录或可证实的采纳门控 |
| 环境适配 | bash 当前命令有 cwd/env/exit code，错误作为运行反馈 | 默认 coding agent 可以自行诊断环境 | 没有自动比较历史与当前API/版本/工具能力，再改写历史操作的适配器 |
| 遇阻再查询 | 持续 agent loop 可以在工具返回后再发搜索、读取或其他工具调用 | IR 每轮要求寻找证据缺口并继续查 | 没有“失败码/低置信度/验证失败→强制检索”的独立触发规则；不保证每次遇阻都查询 |

运行循环不是空壳：Lite 发送一次 RPC prompt，接收消息和工具事件，直到 `agent_end`，超过 turn cap 才发 abort，见 [prompt_and_wait](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/pi_rpc_runner.py#L1263-L1336)。真正的模型—工具循环在 Pi：[agent-loop.ts 第167–231行](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/agent/src/agent-loop.ts#L167-L231) 将工具结果加入 `currentContext.messages`，只要仍有工具调用就继续。工具执行还支持串行与并行分支，[第336–347行](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/agent/src/agent-loop.ts#L336-L347)。这证明能力已实现，不证明每个实际任务一定使用了多轮检索。

## 四、截断确实存在，不是无限制读原文

当前核查的 Pi 版本有两层需要区分的运行截断。

1. 工具自身：`read` 默认最多2,000行或50 KiB，按 offset/limit 读取并给后续 offset。单行超过上限时提示使用 bash 处理，见 [read.ts 第188–236行](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/coding-agent/src/core/tools/read.ts#L188-L236)、[上限常量](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/coding-agent/src/core/tools/truncate.ts#L1-L13)。`bash` 保留输出尾部；超过字节阈值时将完整输出存到临时文件并在截断响应中提供路径，见 [bash.ts 第295–357行](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/coding-agent/src/core/tools/bash.ts#L295-L357)。因此很宽泛的命令输出也可能丢掉前面的候选，agent 仍需控制命令与续读。
2. 运行上下文层：`afterToolCall` 还能对成功工具结果按字符数二次截断；`transformContext` 在调用模型前做上下文管理，见 [agent-session.ts 第413–456行](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/coding-agent/src/core/agent-session.ts#L413-L456)。当前 `level3` 默认工具文本上限20,000字符，工具结果累计超过240,000字符时触发 micro-compaction，配置保留最近12轮，in-loop summary关闭；`level4` 才默认开启该 summary 路径，见 [settings-manager.ts 第182–198行](https://github.com/jdf-prog/pi-mono/blob/a6be5eb4cce278de31ac05792af3dfc0883215dc/packages/coding-agent/src/core/settings-manager.ts#L182-L198)。这些是当前依赖分支的配置，不能自动视为论文每个实验的冻结值。

另有第三种容易误读的内容：Lite Python runner 的 `--conversation-clear-tool-results` / `--conversation-externalize-tool-results` 修改的是导出给人查看的 `conversation.json`，见 [参数说明](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/pi_rpc_runner.py#L1414-L1432) 和 [复制后处理](https://github.com/DCI-Agent/DCI-Agent-Lite/blob/271f37e71f053bf0c99c05ce6d2fb53b841d922e/src/dci/benchmark/pi_rpc_runner.py#L874-L890)。不能仅从这段 artifact 整理逻辑推断模型上下文怎样压缩；真正生效的是上面 Pi 的 runtime hook。

## 五、与我们首轮的差距，怎样准确地讨论下一版

我们首轮已经保留了一次持续的 Codex agent loop，以及可交替使用的 search/read 接口；因此不能说它只有一次检索或“没有 agent”。实际24个 A3 中多数只在开头查询，属于本轮观测到的策略行为。具体证据见 首轮检索审计（历史文件：`experiment/benchmarks/wildclaw_bench/reports/analysis/retrieval_audit.md`，不在精简发布中）。

更准确的差距如下。

| 本轮受限接口 | 上游可提供的自由度 | 下一版仍需我们定义的部分 |
|---|---|---|
| 一个 query 被整体按字面串搜索 | agent 通过 shell 自由选择多模式、正则、管道、路径范围 | 如何保留清晰且有边界的多词/别名语义，不必暴露无约束shell |
| 按 episode ID和行序返回最早若干行 | agent 可先列候选文件、改变范围、自己筛选和比较 | 去除重复摘要/环境列表、任务多样性配额、是否加入排序对照 |
| 每行重复完整provenance，占用读窗预算 | 普通文件读取返回较连续的文本，仍有工具和runtime截断 | 确保返回命中及方法体；按块给来源、保留原位置、续读被截断步骤 |
| 强制开局至少查一次，有候选再读 | 上游没有同样的最低调用计数门槛；IR 强调多角度证据策略 | 何时放弃无关命中、何时遇阻再查、怎样记录方法采用及验证 |
| 历史任务轨迹作为经验库 | 上游主要处理检索文档与问答/IR任务 | 行动、结果、失败及环境差异如何表示与用于当前任务 |

因此可以有三种明确的讨论口径：

- **恢复上游原语能力**：提供可组合搜索、候选级局部读取和继续查询的自由度。这是在缩小本轮接口与终端交互的差距。
- **借鉴上游 IR 提示策略**：多角度查证、读后判相关、围绕缺口继续查。需要明确是 IR 配置的借鉴，不是声称默认 BrowseComp prompt 已全部具备。
- **为轨迹迁移新增机制**：轨迹去噪去重、命中读窗、环境兼容检查、采纳与验证记录；如增加 BM25/独立排序器，应单独标为检索策略对照。这些不能都称为“照搬上游现成实现”。

本文件只完成源码核查和方法讨论，尚未改 prompt、接口或实验配置，也没有运行新版实验。
