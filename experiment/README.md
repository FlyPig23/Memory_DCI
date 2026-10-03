# WildClawBench：V1 / V1.1 基线与 V5

本目录保留无历史资源基线与每题独立 memory 的最新工作流。数据、运行产物和本地依赖不随 GitHub 代码发布；固定来源与恢复方式见 [DATA.md](../DATA.md)。

| 版本 | 条件 |
|---|---|
| V1 / A0 | 历史无检索基线，GPT-6 Astra / ultra |
| [V1.1](variants/vanilla_sol/README.md) | 原 A0 提示词，GPT-5.6 Sol / medium，无历史资源 |
| [V5](variants/dci_memory/README.md) | GPT-5.6 Sol / medium，只读训练轨迹与本题 memory |

冻结划分为 seed `20260908` 的 36 training / 24 test，按类别配额和近重复 family 整组划分。V5 仅检索 training 的 432 条轨迹（36 × 12 来源模型）；test 的历史轨迹、历史得分和隐藏 rubric 不进入 solver。官方浏览器／社交工具使用说明保留，它们与已经移除的蒸馏经验技能不同。

## 运行入口

从项目根目录执行。需要 Python 环境、Docker、固定 WildClawBench 代码与任务输入、准备好的求解镜像、Codex CLI 及本地推理服务，见[运行文档](docs/README.md)。模型调用使用操作者自己的登录，认证文件和私有服务状态留在本机。

以下命令用于已恢复必要数据和运行环境的全新工作目录：

```bash
# V1：冻结本轮协议，只执行 A0。
experiment/.venv/bin/python -B -m experiment.scripts.run_wildclaw --phase formal --freeze
experiment/.venv/bin/python -B -m experiment.scripts.run_wildclaw --phase formal

# V1.1
experiment/.venv/bin/python -B -m experiment.variants.vanilla_sol.runner prepare
experiment/.venv/bin/python -B -m experiment.scripts.launch_vanilla_sol_batch

# V5
experiment/.venv/bin/python -B -m experiment.variants.dci_memory.runner prepare
experiment/.venv/bin/python -B -m experiment.variants.dci_memory.batch launch
```

准备阶段建立当前代码与数据的校验值。旧 `formal_protocol.json` 记录历史实验；V1 新协议写入 `experiment/manifests/v1_protocol.json`。已有历史 V1.1 / V5 协议不会被静默覆盖；新批次应使用没有旧协议和运行记录的独立目录。已有完整结果不自动重跑，不完整运行需要核查或恢复。

`experiment/runtime/bin/rg` 必须预先准备，并匹配 `workflow_config.json` 中的哈希。默认推理服务使用 Sol / medium judge，适用于 V1.1 / V5；匹配历史 V1 评分条件还需要单独配置 Astra / medium judge，仅选择 V1 solver 不会自动切换评分服务。

分享数据用于查看保存的结果及重建分享后的查询池，经过处理的语料不承诺与历史私有 pool 逐字节一致；完整历史审计仍需要原始本地证据。新运行还需要 DATA.md 中列出的上游环境与本地登录。

## 执行与结果边界

每题独立容器、一次 solver rollout，求解与评分各最多 1800 秒。V5 在答案冻结后增加最多 180 秒独立复盘，不能修改答案或接触隐藏评分。每题 memory 初始为空，完成后不传给下一题。

评分使用固定官方逐题 rubric；本地兼容层通过 Codex 承接需要 LLM 的调用。V1.1 与 V5 的 judge 配置为 Sol / medium，不声称等同于原始 OpenRouter judge。有效低分与超时首次产物保留；基础设施评分失败记为缺失，按[评分恢复规则](docs/GRADING_RECOVERY.md)处理。

历史均分：V1 **0.797892**，V1.1 **0.675729**，V5 **0.740963**，各 24 题。V1 模型不同；V5 对 V1.1 是多个机制同时变化的单次条件比较。累计 tokens 包含缓存输入，不等于订阅扣费。

研究依据见 [V5 memory 笔记](../docs/research_notes/retrieval_followup/codex_memory_structure_20260928.md)与[检索文献核查](../docs/research_notes/retrieval_followup/README.md)。旧 V2–V4 资料仅用于解释研究思路，不再作为执行入口。
