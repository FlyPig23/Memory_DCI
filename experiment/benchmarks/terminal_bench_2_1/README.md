# Terminal-Bench 2.1：baseline、V5 与 V6

在同一组 **36 个测试任务**上运行纯 Codex baseline、初始空 memory 的 V5，或带冻结初始错题 memory 的 V6。模型、reasoning effort 和评分时限策略均可配置；每次实验使用独立的 run name。

本基准固定 **53 training / 36 test**，与 WildClawBench 的 36/24 划分不同。数据来自 [Terminal-Bench 2.1 revision 6](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/6)，registry 中的 package version 为 `2.0.2`，不代表改用了 TB2.0。完整划分和测试顺序随数据下载并校验，也可直接查看 [固定任务分类与题序](https://huggingface.co/datasets/FlyPig23/memory_dci/blob/main/terminal_bench_2_1/task_split.csv)。

| 条件 | Solver 可见经验 | 独立最终复盘 |
|---|---|---|
| baseline | 无训练轨迹、memory、额外技能或检索 MCP | 无 |
| V5 | 53 张训练任务卡、历史轨迹、本题初始为空的 memory | 最多 180 秒，只看评分前冻结证据 |
| V6 | 同一查询池，加上从训练失败轨迹离线提炼的冻结初始 memory；每题独立复制 | 最多 180 秒，只看评分前冻结证据 |

V5/V6 每题 memory 独立，任务内更新不跨题传递。各组保留官方任务说明与原生工具，按每题 `task.toml` 的 agent timeout 求解。通用 runner 默认使用官方 verifier timeout；本文 V6 命令与双阶段控制器显式使用 `unlimited`。方法与历史评分差异见 [PROTOCOL.md](docs/PROTOCOL.md)。

## 1. 安装本地运行环境

需要 **Linux x86_64、Python 3.11.8 或以上、Docker Engine 与 Docker Compose**，以及下载依赖、任务镜像和访问模型服务的网络。安装器另外提供固定 Python 3.12.13 作为运行解释器。固定的 36 道测试题均为 CPU 任务，不需要 GPU；逐题最多配置 4 CPU、8 GiB 内存，宿主机还需留出运行与构建余量。

以下命令均从代码仓库根目录执行：

```bash
python3 experiment/benchmarks/terminal_bench_2_1/scripts/setup_runtime.py
```

安装器在本目录创建 `.venv/`、`runtime/bin/` 与 `runtime/python/`，安装 Harbor 0.23.0、Codex CLI 0.153.4、匹配的 code-mode helper、rg 和独立 Python。它不自动运行 Docker 任务、登录或调用模型。检查已安装文件：

```bash
python3 experiment/benchmarks/terminal_bench_2_1/scripts/setup_runtime.py --check-only
```

需要自定义 HTTPS CA 时，安装命令可加 `--ca-file /absolute/path/ca.pem`，将其加入任务和 reviewer 容器的证书包；安装依赖时宿主机自身仍须信任下载站点的证书。

模型使用操作者自己的 Codex 登录：

```bash
experiment/benchmarks/terminal_bench_2_1/runtime/bin/codex login
```

当前适配器使用 Codex OAuth 登录文件，默认读取 `~/.codex/auth.json`；也可设置 `CODEX_AUTH_FILE` 或向运行命令传入 `--auth-file /absolute/path/auth.json`。它不把普通 API key 转成此登录格式。认证文件留在本机，不随数据集发布。

## 2. 下载固定任务与查询池

数据放在同一个 Hugging Face 仓库 [FlyPig23/memory_dci](https://huggingface.co/datasets/FlyPig23/memory_dci) 的 `terminal_bench_2_1/` 子目录，与 WildClawBench 文件分开。

```bash
experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.download_data
```

默认恢复任务包、固定清单和 V5 查询池，并固定到数据发布 commit `5cd1cf968d544bd8cb619b1fccc3265704fcc85d`。只跑 baseline 可加 `--without-pool`；用 `--revision <Hugging-Face-commit>` 显式选择其他版本。下载器校验压缩包及逐文件 SHA256，拒绝覆盖不同内容的现有数据，不重新抽样划分。

数据包含 89 个官方任务包。V5 仅挂载 53 个 training tasks 的查询池：5,830 条来源记录中，5,785 条有轨迹正文，45 条经来源核查确认没有正文；缺失正文与无评分记录均明确标注，不伪造轨迹或把缺分改成零。共享轨迹经过凭据清理时，变更和来源哈希另行记录，不能将导出文件称为与历史私有 pool 完全一致。

## 3. 设置模型，运行两组实验

[config.json](config.json) 保存默认设置。命令行 `--model` 和 `--effort` 覆盖默认值，适用于当前 CLI 与个人账户支持的模型；填写任意厂商模型名不会自动切换推理后端。V5 solver 和 reviewer 使用相同模型及档位。

先查看 baseline 运行计划：

```bash
experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.run plan \
  --arm baseline --run-name baseline-sol \
  --model gpt-5.6-sol --effort medium --verifier-policy official
```

`plan` 只读取源码与数据，检查哈希并展示 36 题的配置、顺序和时限，不写入运行协议，也不调用 Docker 或模型。实际 `run` 第一次执行时冻结协议，包括运行依赖、证书和 reviewer / 清理容器镜像；恢复时继续使用同一镜像。

启动 baseline 与 V5：

```bash
experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.run run \
  --arm baseline --run-name baseline-sol \
  --model gpt-5.6-sol --effort medium --verifier-policy official

experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.run run \
  --arm v5 --run-name v5-sol \
  --model gpt-5.6-sol --effort medium --verifier-policy official
```

每个 `run` 串行执行固定顺序的 36 题。选择不同模型时，为两组设置相同模型、effort 和 verifier policy，并分别使用新的 run name。`official` 保留官方逐题评分限时；`unlimited` 只移除外层 verifier deadline，不改 solver 限时或官方测试代码内的超时。

查看保存的进度：

```bash
experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.run status \
  --arm baseline --run-name baseline-sol
```

已有完整任务不会重新求解。继续同一 run 时，代码与配置必须匹配保存的协议；遇到已有不完整任务目录会停止核查，不会静默重试或覆盖结果。改变模型或评分策略应开始新实验，并单独报告额外尝试。

## 4. V6：先构建错题 memory，再运行测试

构建器使用 `gpt-5.6-luna / xhigh`，只处理固定训练划分内有正文、已评分且 reward=0 的历史轨迹。它按训练题分组提供有限长度的失败片段，并允许读取完整原文；覆盖与未引用记录会单独报告。来源不足可 `no_update`，修复建议必须标明是否仅是假设。

默认提炼时限按每题材料量计算：`clamp(300 + 30 × 失败轨迹数 + 60 × ceil(轨迹总字节数 / 1 MiB), 600, 3600)` 秒，即 10–60 分钟；完成即可提前退出。这是初始预算启发式，不是已验证的最优配置，也不表示会读完全部原文。逐题材料量、预算与是否触及一小时上限写入构建计划，实际耗时及超时结果写入每次尝试。显式传 `--timeout-seconds` 才使用固定时限。这里仅调整离线提炼；正式测试仍使用官方逐题求解时限，本文 V6 评分仍不设外层超时。

```bash
experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.build_failure_memory plan

experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.build_failure_memory build \
  --work-dir experiment/benchmarks/terminal_bench_2_1/runs/v6_memory_build_20261005 \
  --seed-dir experiment/benchmarks/terminal_bench_2_1/prepared/memory_seeds/v6_luna_xhigh_20261005

experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.run run \
  --arm v6 --run-name v6_seeded_sol_medium_20261005 \
  --memory-seed prepared/memory_seeds/v6_luna_xhigh_20261005 \
  --model gpt-5.6-sol --effort medium --verifier-policy unlimited
```

`--max-jobs 1` 可先检查一个真实提炼任务；移除此参数后继续同一构建目录，不重做已完成任务。只有全部构建任务完成、输出非空且来源与模型审计通过，才生成最终 seed。失败尝试保留，不能用成功结果覆盖。V6 禁止空 seed；baseline 与 V5 禁止传 seed。

提炼进程若在返回最终答复前超时，但已经在截止前写出完整 `failure_memory.json`，收集器可在模型、格式、来源和时间检查通过后接收该产物。原始超时状态、费用记录及 JSON 均保留，并另外记录恢复来源；缺失或不合法的文件不能算完成。人工核查发现错误经验时，以带理由和哈希的排除记录处理，保留模型原文。中断运行可能缺少最后一轮的完整 token usage。

修复收集器后续跑已有建库目录，可显式使用 `--accept-code-update`（双阶段控制器对应 `--accept-builder-code-update`）。该选项只接受源码哈希变化；训练数据、模型和预算必须相同，旧计划及既有产物哈希会留档，不会重新生成已完成任务。

已有建库目录改用自适应时限，还需 `--accept-budget-update`（控制器对应 `--accept-builder-budget-update`），单独保存预算修订记录。已完成经验沿用原始预算与输出；不得把旧尝试的 300 秒改写成新预算。原来超时且没有合格产物的任务，仅在新预算确实增加、修订记录可核验并显式传入 `--retry-budget-exhausted`（控制器对应 `--retry-builder-budget-exhausted`）时追加一次尝试，保留全部旧尝试和成本。需要在当前提炼结束后暂停，可在构建目录创建 `pause_requested` 文件；新控制器会在任务边界暂停，暂停期间不导出 seed 或启动测试，删除此文件后可续跑。

需要按顺序自动执行两个阶段时，运行以下控制器。它只在完整 seed 冻结后开始正式测试，阶段状态写入构建目录的 `pipeline_state.json`：

```bash
experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_2_1.scripts.v6_pipeline \
  --work-dir experiment/benchmarks/terminal_bench_2_1/runs/v6_memory_build_20261005 \
  --seed-dir experiment/benchmarks/terminal_bench_2_1/prepared/memory_seeds/v6_luna_xhigh_20261005 \
  --run-name v6_seeded_sol_medium_20261005
```

查询建库状态用 `build_failure_memory status --work-dir ...`；查询测试状态用 `run status --arm v6 --run-name ...`。各题从同一 seed 开始，任务内更新不回写 seed，也不进入下一题。Luna 建库成本、Sol 求解成本和最终复盘成本分别记录。

## 结果与来源

运行协议、进度和逐题结果保存在本目录的独立 `runs/` 下，不上传个人登录、活跃运行目录或整份私有会话。任务得分、基础设施失败、缺分与最终复盘有效性分别记录；失败评分不能自动当作有效零分。

V5 同时增加轨迹分数线索、检索指引、五个领域工具和 memory。一次条件比较不能单独证明 memory 的因果收益。solver 与 reviewer 的时间和 tokens 分开统计；复盘不能改善已冻结答案。

任务文件来自 Apache-2.0 的[官方 TB2.1 仓库](https://github.com/harbor-framework/terminal-bench-2-1/blob/main/LICENSE)，保留上游声明。Hub 历史轨迹保留各自来源与条款记录，不将任务代码许可自动扩展为所有第三方轨迹的统一许可。
