# V1.1：Sol / medium 无历史资源基线

V1.1 沿用原 V1 / A0 提示词、36/24 划分和 24 题测试顺序，solver 与 LLM judge 使用 GPT-5.6 Sol / medium。历史 V1 的 solver 为 GPT-6 Astra / ultra，不能视作同模型对照。

本版本不提供历史轨迹、蒸馏经验技能、检索 MCP 或跨题 memory。保留 Codex 原生工具和官方任务自带的浏览器／社交接口说明。每题独立容器、一次 solver rollout，求解与评分各最多 1800 秒。

## 运行

先按 [DATA.md](../../../DATA.md) 获取数据与固定来源，并按[共享运行文档](../../docs/README.md)准备镜像、Codex CLI、本地登录和推理服务。从项目根目录执行：

```bash
experiment/.venv/bin/python -B -m experiment.variants.vanilla_sol.runner prepare
experiment/.venv/bin/python -B -m experiment.scripts.launch_vanilla_sol_batch
```

准备阶段从共享配置和原 A0 顺序建立本轮协议，不依赖旧 V2–V4 工作流。已有历史协议不会被覆盖；新实验使用没有旧协议和运行记录的独立工作目录。启动器保存后台进程身份和日志，控制器串行处理 24 题。已有完整结果只接受核查，不重复求解；不完整运行和基础设施错误保留证据并停止。

| 路径 | 用途 |
|---|---|
| `prepared/formal/protocol.json` | 本轮模型、提示词、时限、输入和源码校验值 |
| `experiment/manifests/vanilla_sol_schedule.json` | 测试顺序 |
| `experiment/runs/vanilla_sol/` | 本地状态与逐题证据 |
| `experiment/reports/vanilla_sol/` | 逐题结果与汇总 |

历史协议中的源码哈希只描述当时的执行。分享包不包含完整私有会话与运行环境，不能仅凭该包重放全部历史审计。

恢复分享包中的原始结果后，可执行 `experiment/.venv/bin/python -B -m experiment.reports.vanilla_sol.summarize` 重新生成逐题汇总。该命令只核对保存的分数、身份与来源哈希，不重做运行审计；历史审计回执仍对应其中记录的原始汇总哈希。

## 结果与评分边界

已保存的 **24/24 题平均得分 0.675729**。V5 相同 solver 配置下为 **0.740963**；每条件每题只运行一次。V5 同时增加带分数轨迹、检索指引和 memory，不能将分差单独归因于记忆机制。

地点搜索与艺术作品检索达到 1800 秒求解时限，首次产物取得有效零分，没有重跑 solver。求解后停止容器并冻结证据，再重建静止评分容器、恢复官方任务状态。评分基础设施失败与有效低分分别记录，见[评分恢复规则](../../docs/GRADING_RECOVERY.md)。
