# TB2.1 方法与复现实验边界

本文件描述发布代码的新运行约定。操作命令见 [README](../README.md)。原本地实验的结果、恢复操作和冻结协议保持独立，新配置不会追溯改写历史结果。

## 固定任务与历史经验

数据集为 [terminal-bench/terminal-bench-2-1 revision 6](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/6)，89 个任务，package version `2.0.2`。划分种子 `20260908`，53 个训练任务和 36 个测试任务；以发布的任务 ID 与顺序为准，不能只用 seed 重新抽样。

划分先按公开任务操作、工具、接口和约束预分类：14 大类、54 个细分任务族，再施加类别配额与任务族覆盖约束。29 个多题族在训练和测试两边均有代表，25 个单例族单独登记。因此它衡量同类新任务的经验迁移，不是未见任务族泛化。历史得分、轨迹、参考答案和 verifier 不参与划分。

训练来源选择官方 leaderboard 的 22 个条件，每条件每任务 5 次 trial，共 9,790 条记录；53 个训练任务对应 5,830 条记录。仅接受与固定任务版本一致或完整任务内容哈希一致的来源，不按成功率筛选。5,785 条有正文，45 条有经核查的正文缺失记录；其中 3 条正文保留为无法解析的原生格式。

“Training”表示只读经验查询池，不涉及模型参数训练。测试任务的历史轨迹、得分和隐藏测试不提供给 solver；本题公开输入仍按官方任务环境提供。

```text
/pool/tasks/<task_id>.md
/pool/trajectories/<task_id>/<model_alias>/<trial_id>/<task_id>_<score>.txt
/memory/                         # 仅本题，可写，初始经验条目为零
```

轨迹文件名保留来源分数；无评分使用 `unscored`，不替换成零。同模型重复运行通过 trial ID 区分。文件名分数不是相似度，也不能证明某个中间操作成功。共享数据的清理记录与哈希只证明导出内容，不证明它与历史私有原件逐字节相同。

## Baseline

Baseline 提示包含官方任务说明及该任务的求解时限。保留 Codex 原生工具，不挂载历史查询池、memory 或离线技能，不注册自定义检索 MCP，不运行最终 reviewer。配置与挂载在任务边界核对；模型会话用于确认实际模型、effort 和 usage。

## V5

V5 使用与 baseline 相同的任务顺序和 solver 配置，增加五个领域工具：

| 工具 | 职责 |
|---|---|
| `reason` | 记录简短目标、可观察阻塞、采用或放弃及下一步 |
| `DCI_search_task` | 从公开训练任务卡中搜索适用候选 |
| `DCI_search_trajectory` | 检索和阅读历史动作及其观察结果 |
| `DCI_search_memory` | 读取本题已记录经验 |
| `distill` | 创建或修订带证据的经验，或明确 `no_update` |

开始先 reason → task search；有适用候选再读取 trajectory 并判断采用或放弃。求解中按需蒸馏、读回 memory；出现具体阻塞时可以继续检索。查询由 agent 生成 bash / rg 命令，不使用 embedding 或自动语义排序。`reason` 不要求输出私有思维链。

每题创建空 memory；最终条目不传给下一题。条目需要适用范围、步骤、局限、来源与验证范围。来源观察不能自动升级为当前题验证结论；无新信息、无依据或没有复用价值时允许不写。

## V6：冻结的训练集错题 memory

V6 在正式测试前，用 `gpt-5.6-luna / xhigh` 从训练集历史零分轨迹提炼失败经验。选择单位是失败的 trajectory，不是整道训练题：同一道题其他模型曾成功，也保留其失败运行。无评分和缺正文不能当作失败证据。原始查询池及含分数的文件名继续保留。

离线提炼默认按训练题材料量分配时限：`clamp(300 + 30 × 失败轨迹数 + 60 × ceil(轨迹总字节数 / 1 MiB), 600, 3600)` 秒。该启发式的范围为 10–60 分钟，逐题提前完成即可退出；不能解释为穷尽阅读保证。首批已完成提炼的 300 秒预算保留原样，后续预算变更作为显式修订留档，原始输出、耗时和成本不改写。只有超时且预算确实增加的任务可显式追加尝试。这个上限属于 memory 构建，与官方逐题 solver timeout、无外层超时的 verifier 分开记录。

条目区分可观察的失败、原因假设和未验证的修复建议，记录适用条件、原始路径、行号及内容哈希。没有足够证据时可以 `no_update`；不能把失败轨迹中的自述包装成已验证的成功步骤。初始条目只允许 `source_observed` 或 `uncertain`。构建日志单列片段提供、原文查阅和被引用的覆盖情况，提供片段不代表完整读过全部轨迹。

历史终端输出可能包含 NUL 字节，例如记录填充或二进制文件转储。冻结 seed 时，仅对具有匹配任务标识、明确零分及已评分头信息的 UTF-8 轨迹，允许 NUL 出现在被引用片段之外；引用片段本身仍须不含 NUL。原始轨迹不删字节、不改行号，文件与引用窗口均保留原字节哈希。

冻结后的非空 seed 由完整文件清单和 manifest SHA256 标识。36 个测试任务从同一份 seed 独立复制，创建各自身份和空事件日志；本题可以修订、补充经验，下一题仍从原 seed 开始。原 seed 不挂载进 solver，只有本题副本可写。构建器看不到测试任务、测试历史、当前测试失败分析或 grader。

这一轮保留 V5 的五工具和启动检索流程，solver 使用 `gpt-5.6-sol / medium`。提示只增加初始错题库及其证据边界说明，没有新增强制的“先查 memory”步骤，也没有把测试题按已知 baseline 成败分配不同帮助。先直接做、失败后再查的策略应另做消融。

本轮 V6 使用 `--verifier-policy unlimited`：各题官方 agent timeout 不变，移除外层 verifier 截止时间，官方测试内部 timeout 保留。题后 reviewer 仍最多 180 秒，且无法改分；V6 将其未完成单列为流程偏差，已有官方分数在求解、初始 seed 和冻结审计通过时仍保留。评分缺失或求解基础设施故障不记为能力上的失败。

报告需要同时给出完整测试集成绩、救回 baseline 失败的题数、损失 baseline 成功的题数、各题差异和成本。离线建库成本与在线求解、题后复盘成本分列。历史 baseline/V5 的代码版本及评分恢复条件与当前可移植 runner 有差别，比较只能据实描述，不能仅凭一次运行宣称 seed 的因果收益。

设计参考：[Filesystem-Based Memory for LLM Agents: Organization, Evolution, and Sustainability](https://arxiv.org/pdf/2607.26637)。该论文关于搜索和构建的重要性依赖实验场景；其程序技能实验也发现 builder 能力影响蒸馏效果，因此 Luna 的质量需要通过经验审查和 V6 实际结果检验。

## 提交、评分与复盘

1. Solver 完成或耗尽官方 agent timeout 后，停止其控制进程，保留任务需要的服务。
2. 在隐藏评分前冻结公开执行证据和工作区快照。任务服务未整体暂停，因此快照不宣称跨服务全局原子性。
3. 运行原始官方 verifier；评分策略独立记录为 `official` 或 `unlimited`。
4. V5 的独立 reviewer 随后运行，最多 180 秒，仅使用评分前冻结的公开证据、训练池和本题 memory 副本。它不能接触隐藏评分或评分日志，不能修改已提交答案。
5. 保存 reviewer 的 `write` / `no_update` 决定及本题 memory；baseline 没有此阶段。

该顺序与 WildClawBench V5 的“复盘后评分”不同，但两者都要求 reviewer 只能使用评分前公开证据。最终复盘不能计作对已冻结答案的帮助。

## 预算、重试与比较

两组 solver 均使用每题官方限时，固定测试集范围为 900–12,000 秒，全部用尽时合计 19.25 小时。这不是预计总耗时，且不含镜像构建、评分和 V5 复盘。全部 36 题为 CPU、单服务任务；最高任务配置为 4 CPU 和 8 GiB 内存。

发布代码默认两组均使用 `official` verifier policy。比较时应选择相同策略：

| 策略 | 外层 verifier 限时 | 官方测试内部超时 | Solver 限时 |
|---|---|---|---|
| `official` | 采用本题 `task.toml` | 保留 | 采用本题 `task.toml` |
| `unlimited` | 移除 | 保留 | 采用本题 `task.toml` |

历史本地 V5 首轮使用官方评分限时，后建 baseline 使用无外层评分限时。历史另有 verifier-only 恢复和一次评分失败后的独立 fresh retry；这些补充操作不是发布版的默认重试流程，也不能混作一次 rollout。原始、补评分和新尝试应分别保留，不取最高分。

新 run 默认每题一次求解，不自动重试 solver。有效低分保留；评分失败、缺分与任务失败分别标记。相同 run 只能在协议一致且没有不完整任务冲突时继续，新模型或新策略使用新 run name。

官方历史参考先对同一任务的全部 repeats 求均值，再对固定测试任务等权平均；不使用 best-of-5。不同模型、effort、agent、运行时间或评分策略的历史记录仅作描述性参考。V5 同时改变多项机制，baseline 与 V5 的整体差值不能单独归因于 memory。

## 数据与知识来源

任务包和查询池经下载器校验后使用；官方代码许可与各来源轨迹条款分开记录。凭据、个人运行状态和当前本地实验不会成为公开数据的一部分。文档与源码校验通过不等于已经重新执行了 36 道真实任务。

- [官方 TB2.1 数据入口](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/6)
- [官方任务仓库与许可证](https://github.com/harbor-framework/terminal-bench-2-1/blob/main/LICENSE)
- [本项目 V5 memory 设计依据](../../../../docs/research_notes/retrieval_followup/codex_memory_structure_20260928.md)
- [DCI / DR-DCI 检索研究](../../../../docs/research_notes/retrieval_followup/README.md)
