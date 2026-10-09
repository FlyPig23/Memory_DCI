# Memory DCI：V8

当前仓库只维护 **Terminal-Bench 4.0 的 V8 工作流**：求解模型通过五个 DCI/memory 工具读取本题的官方失败轨迹，以及 Opus 基于这些轨迹写的错题分析；每次运行使用独立 memory。代码、协议和报告放在 GitHub，固定数据与结果证据放在公开的 [Hugging Face 数据集](https://huggingface.co/datasets/FlyPig23/memory_dci/tree/main/terminal_bench_4)。

**冻结结果：35 题 × 5 次通过 66/175（37.7%），官方 Sol 同题为 17/175（9.7%）。** 36 道入选题中，cad-model 因官方参考解预检失败而排除。官方 Sol 的 158 条失败轨迹全部进入查询池，其历史成绩也用于选题；再加上 effort、harness 和运行时间不同，这不是独立对照，不能将分差单独归因于 DCI 或 memory。bun 的一次官方通过存在可逆编码的测试覆盖缺口，原分保留并单列说明；ROY 与 layout 的成功包含具体候选答案、历史代码和参数的复用。完整证据见 [V8 报告](experiment/benchmarks/terminal_bench_4/docs/V8_REPORT.md)。

## 下载与运行

需要 Linux x86_64、Python 3.11.8+；实际运行还需要 Docker 和自己的 Codex 登录。从仓库根目录执行：

```bash
# 只下载并校验冻结输入，不调用模型。
python3 -m experiment.benchmarks.terminal_bench_4.scripts.download_data

# 安装 V8 独立运行环境；不启动实验。
python3 -m experiment.benchmarks.terminal_bench_4.scripts.setup_runtime
```

接下来按 [V8 运行说明](experiment/benchmarks/terminal_bench_4/README.md) 登录自己的 Codex 账户、查看运行计划并创建新的运行。下载的 seed 可直接用于求解；只有重新生成错题分析时才需要 Claude Code 登录。数据内容、固定版本与恢复边界见 [DATA.md](DATA.md)。

## 版本演变

旧版执行目录已从当前代码树移除；历史提交与已经发布的数据保留供查阅。不同基准、模型和评分条件下的结果不能直接横向排名。

| 版本 | 基准与变化 |
|---|---|
| V1 | WildClawBench 固定 36 training / 24 test；GPT-6 Astra / ultra，无历史轨迹或 memory 的基线。 |
| V1.1 | 保持原提示词、划分与 24 题顺序，solver 和 judge 改为 GPT-5.6 Sol / medium，仍不给历史资源。 |
| V5 | GPT-5.6 Sol / medium 查询训练题的历史轨迹；加入五个可执行 DCI 工具、每题从空开始的 memory 与独立题后复盘，更新不传给下一题。先用于 WildClawBench，后用于 TB2.1 的 53 training / 36 test。 |
| V6 | TB2.1 同一划分；先用 GPT-5.6 Luna / xhigh 从训练失败轨迹提炼 221 条初始经验，每道测试题从同一 seed 的独立副本开始，并继续查询训练轨迹。 |
| V7 | TB2.1 同一组 36 测试题；改为只给本题的官方失败轨迹和 Opus 5.5 / high 写的本题分析，衡量同题失败材料回放。每题一次，评分不设外层超时。 |
| V8 | 改用 TB4 官方 Sol 通过率低于 50% 的难题，36 题入选、35 题通过参考解预检，各跑 5 次；采用官方求解与评分时限，延续逐题查询池、seed、工具及 180 秒题后复盘，并调整去污染中的重合计数与题面路径判定。 |

工具包含 `reason`、`DCI_search_task`、`DCI_search_trajectory`、`DCI_search_memory` 和 `distill`，有可执行实现及提示词。检索执行模型生成的终端命令，不使用 embedding、BM25 或自动语义排序。每次求解后的更新不回写冻结 seed，也不带入下一次运行。

## 当前目录

```text
experiment/
├── benchmarks/
│   └── terminal_bench_4/
│       ├── scripts/       # 安装、下载、建库、运行和结果入口
│       ├── engine/        # V8 使用的求解与 memory 构建实现
│       ├── manifests/     # 选题、版本与审计记录
│       └── docs/          # 协议与 V8 报告
└── shared/                # Codex 适配、隔离、memory 工具与下载校验
```

数据恢复到基准目录内的 `prepared/`、`runs/` 和 `source_snapshots/` 等位置，由 Git 忽略。发布代码已将旧的跨目录依赖迁入 V8；历史运行协议和审计哈希仍描述实际执行时的原源码与原路径，未被改写。数据包另附原始源码快照，用于核对冻结实验；新运行使用当前代码重新冻结协议。源码迁移对应关系见[校验清单](docs/SOURCE_MIGRATION.json)。详见 [方法与边界](experiment/benchmarks/terminal_bench_4/docs/PROTOCOL.md)及 [数据说明](DATA.md)。
