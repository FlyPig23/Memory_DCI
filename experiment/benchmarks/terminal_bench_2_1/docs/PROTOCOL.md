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
