# Terminal-Bench 4.0：V8

V8 在 Terminal-Bench 4.0 上检验同一种做法：`gpt-5.6-sol / medium` 在解一道难题时，可以用 DCI 工具查询**这道题本身**的官方失败轨迹，以及 Opus 5.5 High 基于这些轨迹写的错题分析。每道题跑 5 次（与官方相同），看整体通过率，并与官方 GPT-5.6 Sol 在同一批题上的成绩对照。方法边界见 [PROTOCOL.md](docs/PROTOCOL.md)，结果见 [V8_REPORT.md](docs/V8_REPORT.md)：35 题 × 5 次通过 66/175（37.7%），官方 GPT-5.6 Sol 在同一批题上是 17/175（9.7%）。

**结果已核验，并补充三题轨迹审计（2026-10-08，America/Chicago）。** cad-model 因官方参考解预检失败而排除；175 次有效求解和原始评分保留。官方 27.1% 的参考值只覆盖同题版本、有评分的 29 个条件。bun-sourcemap-leak 的一次官方通过存在可逆编码掩盖私有常量的语义缺陷；ROY 与 layout 的成功则有具体的失败候选、代码与参数复用证据。详见报告第 4.2、5 节与 [审计记录](manifests/v8_report_audit_20261008.json)。

V8 与官方 Sol 的 effort、harness、材料及运行时间不同；对照中的 158 条 Sol 失败轨迹也全部进入 V8 查询池。该差距不能单独归因于 DCI 或 memory，更不代表跨题迁移收益。

历史 V8 沿用 TB2.1 的 V7 运行时：五个 DCI 工具、启动检索流程、本题可写 `/memory`、180 秒题后复盘，提示词逐字相同。当前发布已将必需实现迁入本目录的 `engine/`，可独立安装运行，不依赖旧实验目录。内部 `v7_*` 文件名与历史证据字段保留以对应原实现；当前唯一工作流是 V8。

## 快速复跑冻结输入

需要 Linux x86_64、Python 3.11.8+、可用的 Docker、访问上游镜像与模型服务的网络，以及自己的 Codex 登录。以下命令从仓库根目录执行：

```bash
python3 -m experiment.benchmarks.terminal_bench_4.scripts.setup_runtime
python3 -m experiment.benchmarks.terminal_bench_4.scripts.download_data
experiment/benchmarks/terminal_bench_4/runtime/bin/codex login

# 查看 36 题和 5 次运行的计划，不调用模型或 Docker。
experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.run_v8 plan \
  --run-name v8_reproduction --corpus final_20261008 --rollouts 5

# 创建新实验：逐题预检，通过后每题求解 5 次。
experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.run_v8 run \
  --run-name v8_reproduction --corpus final_20261008 --rollouts 5 \
  --model gpt-5.6-sol --effort medium --workers 4

experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.run_v8 status --run-name v8_reproduction
```

安装器固定 Harbor 0.23.0、Codex CLI 0.153.4 等运行依赖，并写入本目录的 `.venv/`、`runtime/` 与传输配置；`setup_runtime --check-only` 可检查安装，私有 CA 可用 `--ca-file /path/to/ca.pem` 加入。认证默认读取 `~/.codex/auth.json`，也可向运行入口传 `--auth-file`。认证文件留在本机。

[config.json](config.json) 保存默认 solver 配置；`--model` 和 `--effort` 可覆盖。模型名称须由当前 Codex CLI 和自己的账户支持；查看计划和实际运行时传入相同的 `--model` / `--effort`。修改模型、设置或输入后使用新的 run name。每次运行从同一冻结 seed 重新复制 memory，不共享题后更新。下载已生成的 seed 后可直接求解；只有重新构建分析时才需要 Claude Code 登录。

查看本轮保存的证据，无需登录或调用模型：

```bash
python3 -m experiment.benchmarks.terminal_bench_4.scripts.download_data --include-evidence
python3 -m experiment.benchmarks.terminal_bench_4.scripts.download_data --verify-only --include-evidence
```

数据固定到 Hugging Face commit `db28540352886750100bd94e45a09bb80adae92b`。任务 ID、题包版本、3,755 条查询轨迹、291 条 seed 与 175 次结果的来源见 [DATA.md](../../../DATA.md)。该证据包省略私有 home 和大型工作区缓存；保留历史协议及原始来源哈希，公开处理另行记录。原源码快照位于 `source_snapshots/v8_original/`；第 1–3 次运行控制器源码的已知缺口见数据说明。不要用迁移后的源码续跑冻结的旧实验目录。

以下各节说明如何从官方来源重新建库及分段运行。复跑已发布的固定输入时，无需重新选题或重新生成 seed；从头构建应使用独立工作目录与新的 build ID。

## 1. 冻结官方数据并选题

数据来自 Harbor Hub 上的官方榜单 [Terminal-Bench 4.0](https://hub.harborframework.com)（`terminal-bench` 包第 4 版，66 道题，35 个参赛条件，每题各 5 次）。选题规则：官方 GPT-5.6 Sol（Codex，max）在这道题上通过不到一半。排除需要 GPU、TPU、MCP 附属服务、多个步骤或额外环境变量的题，因为 runner 只支持单服务的 CPU 题。

```bash
experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.v8_sources

experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.download_tasks
```

`v8_sources` 只读公开表，使用 Harbor 自带的匿名公开 key，不需要个人凭据；它把榜单、全部 trial 元数据、题目版本和选题结果写入 `manifests/`。已有清单内容不同时拒绝覆盖，需显式 `--replace`。`download_tasks` 用 `harbor download` 取回入选题的题包，并用 Harbor 自己的打包哈希逐题核对，必须与官方 trial 评分时的题目版本完全一致。

本次冻结结果：66 题中有 39 题低于 50%，排除 2 道 GPU 题（jax-speedrun-gpu、math-eval-grader）和 1 道需要 playwright MCP 服务的题（medical-claims-processing），**入选 36 题**。官方 GPT-5.6 Sol 在这 36 题上合计 17/180（9.4%）。

## 2. 构建逐题查询池

以下示例新建 `rebuild_local`；不要覆盖下载的 `final_20261008` 冻结语料。

```bash
experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.v8_corpus download

experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.v8_corpus scan

experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.v8_corpus refine

experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.v8_corpus decide \
  --reviews experiment/benchmarks/terminal_bench_4/manifests/v8_decontamination_reviews.json

experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.v8_corpus build --build-id rebuild_local
```

- `download`：从官方 Hub 取回每条失败轨迹，或官方的“无正文”证明；只取已评分、得分为 0、并且在榜单所用题目版本上评分的记录，成功轨迹从不列出。
- `scan`：在宿主机上逐条渲染、脱敏，并计算与隐藏测试、参考解的重合；只记录行号、指标和命中的来源字符串，不写出测试或参考解的内容。
- `refine`：对被 V7 固定阈值标记的轨迹复查一遍：与隐藏测试的重合改为按词计数，题目说明里给出的路径（如 `/app/solution/`）不算评分文件名。
- `decide`：按 V7 校准后的规则自动判定每条被标记的轨迹，`--reviews` 中的人工复核结论优先；复核结论在 `manifests/v8_decontamination_reviews.json`。
- `build`：把查询池写成与 V7 相同的格式（`prepared/v8_corpus/<build_id>/`），V7 的 DCI 工具、Opus 写手和运行时可以直接使用；`verify --build-id ...` 重新核对。

## 3. Opus 5.5 High 写错题分析

写手逻辑沿用 V7，当前入口为本目录的 `build_memory`；`--base` 默认指向本目录：

```bash
experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.build_memory build \
  --base experiment/benchmarks/terminal_bench_4 \
  --corpus prepared/v8_corpus/rebuild_local --work-dir runs/v8_memory_rebuild_local --workers 6

experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.build_memory export \
  --base experiment/benchmarks/terminal_bench_4 \
  --corpus prepared/v8_corpus/rebuild_local --work-dir runs/v8_memory_rebuild_local
```

需要本机 Claude Code 登录和 Linux 原生 Claude Code 可执行文件；安装器不安装该写手。默认凭据为 `~/.claude/.credentials.json`，可向 `build` 传 `--credentials /absolute/path/credentials.json`。默认从 VS Code Server 扩展目录寻找原生 binary；其他安装方式需向 `build` 传 `--claude-binary /absolute/path/to/native/claude`，指向实际可执行文件而非符号链接或 npm 启动脚本。

每题一个容器，只读挂载本题查询池，看不到测试、参考解、其他题或成功轨迹；导出前的隔离闸门与 V7 相同。重新提炼会产生新的模型输出与费用，不会逐字重现冻结 seed。

## 4. 分段运行与查看结果

需要沿用历史“先 3 次、再补 2 次”的运行方式时，可对上面重新构建的语料使用以下新运行名；快速复跑部分则一次计划全部 5 次。

```bash
experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.run_v8 run \
  --run-name v8_new_first3 --corpus rebuild_local --workers 4

experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.run_v8 status \
  --run-name v8_new_first3

# 补到每题 5 次：第 4、5 次，沿用原运行冻结的全部输入
experiment/benchmarks/terminal_bench_4/.venv/bin/python -m \
  experiment.benchmarks.terminal_bench_4.scripts.run_v8 run \
  --run-name v8_new_first3_r45 --corpus rebuild_local \
  --rollouts 5 --extends v8_new_first3 --workers 4 --open-tasks 3
```

- **执行顺序：** 逐题打开：先拉取这道题的环境镜像和评分镜像，用官方参考解跑一次预检，必须得 1 分，否则跳过这道题的求解并单列；然后跑 3 次求解，跑完删除镜像。预检没有给出分数（例如镜像拉取失败）时，只写 `oracle_error.json`，这道题留待检查，不算预检失败，其他题照常进行。同时打开的题不超过 `--open-tasks`（默认 2），`--workers` 限制同时运行的任务数。
- **时限与重试：** 保留官方 agent 时限和官方评分时限。求解不重试；中断留下的目录保留待查，不会被静默重跑。
- **补跑：** `--extends` 只加跑 `--rollouts` 以内、原运行之后的轮次（这里是第 4、5 次）。题包、查询池、seed、设置和求解运行时必须与原运行一致，否则拒绝启动；原运行预检未通过的题不补跑。
- **status：** 重新计算每次求解的有效性，汇总通过率，并给出官方 sol 在同一批题上的成绩；对补跑运行查询时，会把原运行的结果合并进来，给出每题 k/5。

## 目录

```text
terminal_bench_4/
├── README.md, docs/PROTOCOL.md      运行说明与方法边界
├── docs/V8_REPORT.md                V8 结果报告
├── scripts/                         安装、下载、建库、运行与测试
├── engine/                          V8 使用的求解、语料与 memory 构建实现
├── manifests/                       选题、官方榜单快照、去污染与审计记录
├── prepared/                        HF 恢复的题包、查询池与 seed（git 忽略）
├── data/                            官方失败轨迹原文（本地，git 忽略）
├── source_snapshots/                历史源码快照（HF 下载，git 忽略）
└── runs/                            建库、运行与下载的公开证据（git 忽略）
```
