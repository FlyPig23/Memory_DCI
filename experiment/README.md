# 实验入口

两个基准平级维护，各自保留代码、配置、数据清单和结果；公共实现只保留一份。

| 基准 | 固定划分 | 入口 |
|---|---|---|
| WildClawBench | 36 training / 24 test | [运行说明](benchmarks/wildclaw_bench/README.md) |
| TerminalBench 2.1 | 53 training / 36 test | [运行说明](benchmarks/terminal_bench_2_1/README.md) |

`shared/` 提供共用 Codex 适配、任务隔离、五个 DCI/memory 工具及数据下载校验。`benchmarks/` 只有以上两个目录。
