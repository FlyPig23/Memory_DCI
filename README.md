# Memory DCI

在两个基准上研究历史轨迹检索与每题独立 memory。两套实验平级维护，公共实现集中在 `experiment/shared/`；代码和方法文档放 GitHub，数据放同一个公开的 [Hugging Face 数据集](https://huggingface.co/datasets/FlyPig23/memory_dci)。

| 基准 | 固定划分 | 保留工作流 | 代码与运行说明 | 数据 |
|---|---|---|---|---|
| WildClawBench | 36 training / 24 test | V1、V1.1 baseline；V5 | [wildclaw_bench](experiment/benchmarks/wildclaw_bench/README.md) | [wildclaw_bench/](https://huggingface.co/datasets/FlyPig23/memory_dci/tree/main/wildclaw_bench) |
| TerminalBench 2.1 | 53 training / 36 test | baseline；V5；V6；V7 | [terminal_bench_2_1](experiment/benchmarks/terminal_bench_2_1/README.md) | [terminal_bench_2_1/](https://huggingface.co/datasets/FlyPig23/memory_dci/tree/main/terminal_bench_2_1) |

```text
experiment/
├── benchmarks/
│   ├── wildclaw_bench/       # 代码、配置、文档、测试；本地数据及结果也归入此目录
│   └── terminal_bench_2_1/   # 代码、配置、文档、测试；本地数据及结果也归入此目录
└── shared/                  # 共用 Codex 适配、容器隔离、DCI/memory 工具和下载校验

side_experiments/            # 独立的小规模实验，只读引用主实验，不改动主实验
docs/                        # 研究资料与会议笔记
papers/                      # 论文来源索引
reports/                     # 历史研究报告
```

每个基准自己的 `scripts/` 是操作入口，`manifests/` 保存任务划分与来源，`runs/` 保存运行产物，`docs/` 与 README 解释方法。公共工具不是第三套实验。V2–V4 与 TerminalBench 4.0 不作为执行目录发布。

## 下载与运行

详细前提和固定版本见 [DATA.md](DATA.md)。以下只下载、校验数据并查看已有证据，不调用模型：

```bash
# WildClawBench：恢复固定 36/24 数据和三组历史结果。
python3 -m experiment.benchmarks.wildclaw_bench.scripts.download_data --include-evidence
python3 -m experiment.benchmarks.wildclaw_bench.scripts.report_results

# TerminalBench 2.1：恢复固定 53/36 任务及训练查询池。
python3 -m experiment.benchmarks.terminal_bench_2_1.scripts.download_data
```

实际运行分别遵循两份基准 README。TerminalBench 支持 `--model`、`--effort` 和 `--verifier-policy`；WildClaw 的 V1、V1.1 与 V5 保留各自实验协议。两者的 V5 都使用本题独立 memory，但复盘与评分的时序不同，不能混用运行协议或任务划分。

## 方法与资料

- [公共工具实现](experiment/shared/README.md)：五个 DCI/memory 工具的可执行实现与提示词。
- [WildClaw 历史结果](experiment/benchmarks/wildclaw_bench/reports/retained/README.md)：从原有 72 份结果重算，目录迁移不改变得分。
- [TerminalBench 协议](experiment/benchmarks/terminal_bench_2_1/docs/PROTOCOL.md)：固定 36 题、隔离与评分策略；[V7 报告](experiment/benchmarks/terminal_bench_2_1/docs/V7_REPORT.md)：每题只给本题失败轨迹与 Opus 错题分析时的 36 题结果。
- [难题错题回放（side experiment）](side_experiments/hard_task_failure_replay/REPORT.md)：5 道难题上比较只给失败轨迹、只给错题分析、两者都给，以及七个写分析的模型。
- [V5 memory 设计依据](docs/research_notes/retrieval_followup/codex_memory_structure_20260928.md)、[检索研究资料](docs/research_notes/retrieval_followup/README.md)、[论文索引](papers/README.md)。

任务划分和历史结果保持不变。数据导出中已清理的凭据内容及本次操作性路径迁移均有记录；公开文件不冒充未经处理的原始私有环境。历史笔记中的运行状态仍是当时的记录。
