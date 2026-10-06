# Terminal-Bench 2.1：baseline 与 V5

在同一组 **36 个测试任务**上运行纯 Codex baseline，或带轨迹检索和本题 memory 的 V5。模型、reasoning effort 和评分时限策略均可配置；每次实验使用独立的 run name。

本基准固定 **53 training / 36 test**，与 WildClawBench 的 36/24 划分不同。数据来自 [Terminal-Bench 2.1 revision 6](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/6)，registry 中的 package version 为 `2.0.2`，不代表改用了 TB2.0。完整划分和测试顺序随数据下载并校验，也可直接查看 [固定任务分类与题序](https://huggingface.co/datasets/FlyPig23/memory_dci/blob/main/terminal_bench_2_1/task_split.csv)。

| 条件 | Solver 可见经验 | 独立最终复盘 |
|---|---|---|
| baseline | 无训练轨迹、memory、额外技能或检索 MCP | 无 |
| V5 | 53 张训练任务卡、历史轨迹、本题初始为空的 memory | 最多 180 秒，只看评分前冻结证据 |

V5 每题 memory 独立，不跨题传递。两组均保留官方任务说明与原生工具，按每题 `task.toml` 的 agent timeout 求解，默认使用官方 verifier timeout。方法与历史评分差异见 [PROTOCOL.md](docs/PROTOCOL.md)。

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

## 结果与来源

运行协议、进度和逐题结果保存在本目录的独立 `runs/` 下，不上传个人登录、活跃运行目录或整份私有会话。任务得分、基础设施失败、缺分与最终复盘有效性分别记录；失败评分不能自动当作有效零分。

V5 同时增加轨迹分数线索、检索指引、五个领域工具和 memory。一次条件比较不能单独证明 memory 的因果收益。solver 与 reviewer 的时间和 tokens 分开统计；复盘不能改善已冻结答案。

任务文件来自 Apache-2.0 的[官方 TB2.1 仓库](https://github.com/harbor-framework/terminal-bench-2-1/blob/main/LICENSE)，保留上游声明。Hub 历史轨迹保留各自来源与条款记录，不将任务代码许可自动扩展为所有第三方轨迹的统一许可。
