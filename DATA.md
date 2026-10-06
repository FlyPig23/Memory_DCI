# 数据与目录

同一个公开数据集 [FlyPig23/memory_dci](https://huggingface.co/datasets/FlyPig23/memory_dci) 按两个基准平级存放，下载无需 Hugging Face token：

```text
memory_dci/
├── wildclaw_bench/
│   ├── inputs.tar.gz       # 36/24 划分、432 条训练轨迹、381 张图片、任务与来源
│   ├── evidence.tar.gz     # V1 / V1.1 / V5 共 72 份历史结果及可分享证据
│   └── manifest.json      # 压缩包与逐文件校验；另有 task_split.json、来源和迁移记录
└── terminal_bench_2_1/
    ├── tasks.tar.gz        # revision 6 的 89 个官方任务包
    ├── pool.tar.gz         # 53 张训练任务卡、5,785 条历史轨迹
    ├── metadata.tar.gz     # 固定 53/36 划分与来源清单
    └── manifest.json      # 压缩包与逐文件校验；另有 task_split.json / csv
```

解压后的数据分别进入本地 `experiment/benchmarks/wildclaw_bench/` 和 `experiment/benchmarks/terminal_bench_2_1/`，与各自代码同属一个基准目录。GitHub 只提交代码、协议及资料，忽略下载数据、运行记录、环境和认证文件。

## WildClawBench

```bash
python3 -m experiment.benchmarks.wildclaw_bench.scripts.download_data --include-evidence --fetch-benchmark
python3 -m experiment.benchmarks.wildclaw_bench.scripts.report_results
```

`--include-evidence` 恢复历史结果；不加则只恢复输入。`--fetch-benchmark` 获取固定的上游 benchmark 代码。完整运行前还需按照 [运行说明](experiment/benchmarks/wildclaw_bench/README.md) 准备 Docker、运行镜像、依赖、个人 Codex 登录和本地推理服务。下载数据本身不意味着环境已能运行模型。

训练与测试仍是原来固定的 **36 / 24**；每组历史结果恰好 24 题。目录迁移不改变任务内容、划分、轨迹正文、评分结果或得分。新版压缩包只改变归档路径，并更新三个操作性清单中的文件路径；`layout_migration.json` 和逐文件 `previous_path` / `previous_sha256` 保留迁移对应关系。

原始来源：

- [WildClawBench 代码](https://github.com/InternLM/WildClawBench/tree/316334ccc4a87b9b5635ad73da99b4dfc0b3887e)
- [任务数据](https://huggingface.co/datasets/internlm/WildClawBench/tree/75f945578aa00cbdb8f46e4d42e4f4e98f704b4f)
- [官方轨迹](https://huggingface.co/datasets/internlm/WildClawBench-Trajectories/tree/d2816016a7a7b41fa6b7ba368b28ddafcb54fd93)

上游声明及第三方素材边界见数据目录的 `provenance.json`、`UPSTREAM_LICENSE.txt`；既有脱敏记录保存在 `redactions.json`。精简证据不包含完整原始会话和工作区，不替代本地完整历史审计。

## TerminalBench 2.1

```bash
python3 -m experiment.benchmarks.terminal_bench_2_1.scripts.download_data
```

只运行 baseline 可加 `--without-pool`。固定 **53 training / 36 test** 的 ID、版本、分类和测试顺序可直接查阅 [task_split.csv](https://huggingface.co/datasets/FlyPig23/memory_dci/blob/main/terminal_bench_2_1/task_split.csv)。这与 WildClaw 的 36/24 是两套独立划分。启动前按 [运行说明](experiment/benchmarks/terminal_bench_2_1/README.md) 安装固定环境并使用自己的登录。

本次目录整理不改动 TB2.1 的数据压缩包。来源是 [registry revision 6](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/6)，package version `2.0.2` 属于该 TB2.1 发布。官方任务包保持原始文件哈希；训练轨迹中的凭据清理已单独记录。第三方轨迹不自动继承任务仓库的 Apache-2.0 许可，见数据目录的来源声明。

## 校验与历史版本

两套下载器均校验压缩包与逐文件 SHA256，拒绝覆盖不同内容的已有文件；`--root` 可选择全新目标目录，`--local-dataset` 可验证手动下载的压缩包，`--revision` 可指定 Hugging Face commit。新版本默认固定发布 commit，不重新随机划分任务。

旧版根目录下的 WildClaw 文件仍可在历史 commit `b31c851fc4c09108ace8fbdf80dc5c1b1b0c396d` 找到。恢复新布局请使用当前下载器；历史代码与历史数据应配套使用。修改源码路径后应为新实验重新冻结协议，不把重算的代码哈希写回原始历史实验。
