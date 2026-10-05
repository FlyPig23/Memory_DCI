# Memory DCI

研究历史轨迹检索与每题独立 memory 能否帮助 agent 完成任务。WildClawBench 保留 **V1 / V1.1 无历史资源基线**和 **V5：DCI + task-local memory**；另提供 [TerminalBench 2.1 的 baseline / V5 最小运行入口](experiment/benchmarks/terminal_bench_2_1/README.md)。V2–V4 工作流不再保留，相关文献与研究笔记作为历史知识资料保存。

代码与方法文档放在本仓库；任务数据、轨迹和可分享的实验记录通过公开的 [Hugging Face dataset](https://huggingface.co/datasets/FlyPig23/memory_dci) 分发。固定来源与下载方式见 [DATA.md](DATA.md)。

| 版本 | Solver | 历史经验输入 | 已完成任务 | 平均得分 |
|---|---|---|---:|---:|
| V1 / A0 | GPT-6 Astra / ultra | 无 | 24 | 0.797892 |
| V1.1 | GPT-5.6 Sol / medium | 无 | 24 | 0.675729 |
| V5 | GPT-5.6 Sol / medium | 432 条训练轨迹 + 本题独立 memory | 24 | 0.740963 |

V1 的模型不同，只作历史参考。V5 与 V1.1 使用相同 solver 配置，但 V5 同时改变历史分数可见性、检索指引、工具接口和 memory，分差不能单独归因于 memory；每题每条件仅运行一次。

TerminalBench 2.1 使用独立的 **53 条训练 / 36 条测试**固定划分，支持通过 `--model`、`--effort` 修改模型配置；代码不依赖 TerminalBench 4.0。本页下方的结果和 36+24 划分均属于 WildClawBench。TB2.1 数据位于同一个 Hugging Face 数据集的 [`terminal_bench_2_1/`](https://huggingface.co/datasets/FlyPig23/memory_dci/tree/main/terminal_bench_2_1) 子目录。

## V5 工作流

固定 36 个 training tasks、24 个 test tasks。训练查询池包含 36 张任务卡、432 条历史轨迹及 381 张引用图片，只读共享；每道测试题从空 memory 开始，题间不传递经验。

1. `reason` 记录目标与下一步，`DCI_search_task` 查找相关训练任务。
2. 有适用候选时，用 `DCI_search_trajectory` 阅读动作与结果，判断采用或放弃。
3. 求解中按需调用 `distill` 保存带来源的经验，用 `DCI_search_memory` 读回；出现阻塞时继续检索。
4. 冻结答案后独立复盘，明确 `write` / `no_update`，再进行隐藏评分。

24 题均完成初始 reason → task search，5 题发生在线 memory 写入。独立复盘最终为 21 次 write、3 次 no_update；复盘发生在答案冻结后，不能算作对本题答案的帮助。

## 阅读与运行

| 入口 | 内容 |
|---|---|
| [实验入口](experiment/README.md) | V1、V1.1、V5 的运行命令与前提 |
| [TerminalBench 2.1](experiment/benchmarks/terminal_bench_2_1/README.md) | 固定 53/36 划分，baseline / V5 安装、下载、改模型与运行 |
| [三组结果汇总](experiment/reports/retained/README.md) | 从 72 份保存结果重算分数、配对差与成本 |
| [V1.1 基线](experiment/variants/vanilla_sol/README.md) | 原 V1 A0 提示词、Sol / medium、无历史资源 |
| [V5 方法与运行](experiment/variants/dci_memory/README.md) | 查询池、五个工具、memory、复盘与评分 |
| [运行与评分文档](experiment/docs/README.md) | 容器隔离、推理服务、评分恢复 |
| [V5 memory 设计依据](docs/research_notes/retrieval_followup/codex_memory_structure_20260928.md) | Codex、Reflexion、MemGPT、A-Mem 的借鉴范围 |
| [检索研究资料](docs/research_notes/retrieval_followup/README.md) | DCI / DR-DCI 文献与固定源码来源 |
| [论文索引](papers/README.md) | 论文版本与来源 |
| [早期研究框架](reports/DCI_upgrade_report.md) | 历史方案与讨论，非当前运行清单 |

```text
experiment/src/                    共享运行、隔离与评分
experiment/scripts/                数据准备、基线及服务入口
experiment/variants/vanilla_sol/    V1.1 基线
experiment/variants/dci_memory/     V5 工作流
experiment/tests/                  共享实现测试
experiment/manifests/              来源、划分与调度
experiment/docs/                   运行方法
docs/                              文献核查与研究笔记
papers/                            论文来源索引
reports/                           历史研究报告
```

历史笔记中的旧目录、运行状态和绝对路径是当时的记录。当前支持范围以上述入口为准。分享的数据经过必要整理，代码整理与数据发布不代表重新执行了历史实验，也不提供原始私有运行环境的完整重放。

按 DATA.md 恢复数据后，可用 Python 标准库重算三组结果，不调用模型：

```bash
python3 scripts/report_results.py
```

脚本要求每组恰好 24 个有效结果及相同任务集合，记录来源哈希；输出到 `experiment/reports/retained/`。这一步核对分享文件与分数计算，不替代原始完整运行审计。
