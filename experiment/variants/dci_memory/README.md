# V5：DCI 查询池与每题独立 memory

V5 使用 GPT-5.6 Sol / medium，沿用固定 36 training / 24 test 划分、24 题顺序、官方任务工具和评分规则。每题一次 solver rollout，从独立空 memory 开始；上一题经验不传给下一题。

已保存实验完成 **24/24 题，平均得分 0.740963**。24 题均先 reason 再查询训练任务，5 题发生在线 memory 写入；独立复盘最终为 21 次 write、3 次 no_update。流程发生过不等于经验被正确采用或带来收益。

## 经验输入与目录

共享只读 pool 包含 36 张公开训练任务卡、432 条原始来源轨迹（36 × 12 来源模型）和 381 张引用图片。文件名展示训练历史分数，失败轨迹也保留。solver 不接触测试题历史轨迹、历史分数或隐藏 rubric。分享后的语料经过处理，不能称为与历史私有 pool 逐字节相同。

```text
/pool/
  tasks/<task_id>.md
  trajectories/<task_id>/<model_alias>/<task_id>_<score>.txt
  images/<content_hash>.<ext>
  manifest.json
/memory/                         # 当前题独享，初始经验条目为零
  identity.json
  memory_summary.md              # 短导航
  MEMORY.md                      # 适用性、关键词和条目索引
  entries/<slug>.md              # 经验正文
  .entry_metadata.json
  events.jsonl
  final_decision.json
  .tool_outputs/                 # 截断工具结果的续读入口
```

同分轨迹通过模型子目录区分。历史分数只是选择来源的线索，不是相似度，也不能证明中间步骤成功。V5 不加载既有蒸馏技能，不启用跨题全局 memory。

## 每题流程

1. `reason` 记录简短目标、可观察阻塞与下一步；`DCI_search_task` 搜索训练任务描述。
2. 有适用候选时，`DCI_search_trajectory` 阅读动作及对应结果，记录采用或放弃；无适用候选时继续独立求解。
3. 得到有依据的经验时，`distill(stage=online)` 创建或更新条目，按需用 `DCI_search_memory` 读回。新阻塞可以触发继续检索与更新。
4. solver 完成或达到 1800 秒时限后，冻结答案、workspace 与求解记录，保留首次产物。
5. 独立 reviewer 使用同模型与档位，在最多 180 秒内调用 `distill(stage=final)`，明确 `write` 或 `no_update`。
6. 验证复盘决定并保存本题 memory，拆除 pool、memory 与领域工具挂载，再按官方规则评分，最多 1800 秒。

搜索执行 agent 自己生成的 bash / rg 命令，支持正则、多模式、路径、管道与按行读取。没有 embedding、BM25 或自动语义排序。Codex 原生终端、文件、浏览器和任务工具继续可用。`reason` 不要求输出私有思维链。

reviewer 只看到公开请求、solver transcript、冻结 workspace、训练 pool 与本题 memory 副本，不能修改 solver 答案或读取隐藏评分。最终复盘产生的条目不会改善已冻结答案；报告将复盘成本与求解成本分开。

## Memory 证据

条目包含标题、关键词、适用范围、步骤、失败模式、验证范围、来源路径／行号和证据摘要。`distill` 同步维护正文与索引。没有新信息、重复、缺少证据或没有复用价值时，`no_update` 是有效决定。

| 状态 | 含义 |
|---|---|
| `source_observed` | 在训练轨迹中观察到，尚未在当前题验证 |
| `locally_verified` | 有当前题检查证据，并限定验证范围 |
| `uncertain` / `contradicted` | 证据不足，或被新观察否定 |

格式与来源检查不能替代语义审查。设计依据见 [Codex / Reflexion / MemGPT / A-Mem 研究笔记](../../../docs/research_notes/retrieval_followup/codex_memory_structure_20260928.md)。本项目借鉴文件分层、证据与反思约定，没有完整移植这些系统。

## 准备、运行与报告

按 [DATA.md](../../../DATA.md) 恢复数据、固定来源和本地运行环境，然后从项目根目录执行：

```bash
experiment/.venv/bin/python -B -m experiment.variants.dci_memory.runner prepare
experiment/.venv/bin/python -B -m experiment.variants.dci_memory.batch launch
```

准备阶段建立本轮协议并验证查询池。历史协议不会被覆盖，新批次使用独立目录。控制器串行遍历 24 题，每题完成求解、复盘、评分和审计后进入下一题。已有 solver 记录不会自动重跑；基础设施错误保留证据并暂停。

| 路径 | 内容 |
|---|---|
| `prepared/formal/protocol.json` | 模型、提示词、输入与源码校验值 |
| `prepared/pool/` | 本地只读查询池 |
| `experiment/runs/dci_memory/batch.json` | 批次状态 |
| `experiment/runs/dci_memory/formal/dci5-formal-XX/` | 逐题证据与 memory |
| `experiment/reports/dci_memory/` | 汇总、逐题表与流程核查 |

拥有完整本地记录时可执行 `experiment/.venv/bin/python -B -m experiment.variants.dci_memory.report`。该入口读取和审计已有证据，不调用模型或重新评分。分享包提供保存的报告，省略私有原始会话，因此不支持完整历史审计重放。代码整理不等于重新执行实验。

## 解释边界

V5 相比 V1.1 同时改变历史分数可见性、检索策略、工具接口和每题 memory，不能将分差解释为 memory 的单独收益。每题每条件一次运行，流程偏差、超时和有效零分保留。

统计在线写入时，应使用事件中 `stage=online` 的 write 决定。`memory_after_solver` 快照可能包含 solver 的 `stage=final` 写入，条目数不能直接当作在线学习次数。工具调用不等于正文阅读、采用或因果收益。token 总量包含缓存输入，不代表订阅扣费。
