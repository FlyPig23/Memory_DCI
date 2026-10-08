# 难题错题回放（side experiment）

**问题：** 官方榜单上 GPT 系列几乎做不出的 5 道 Terminal-Bench 2.1 难题，如果 `gpt-5.6-sol / medium` 解题时能看到**同一道题**的官方失败记录，能不能做出来？失败记录怎么给（原始轨迹、另一个模型写的错题分析，或两者都给），以及由哪个模型来写错题分析，影响有多大？

本目录独立于主实验：只读引用主实验的任务包、历史轨迹原始记录和固定运行环境，不写入主实验目录，也不修改主实验代码。这里验证过的“本题失败轨迹 + 错题分析”设定，后来在主实验的 36 道测试题上做成了 [TB2.1 V7](../../experiment/benchmarks/terminal_bench_2_1/docs/V7_REPORT.md)。

**完整报告：[REPORT.md](REPORT.md)**，包括流程、代码说明、全部结果、每道题的机制分析，以及哪个模型写错题分析最好。

## 组别

所有组的 solver 都是 `gpt-5.6-sol / medium`，每次求解一个独立进程，不重试。对照组直接调用主实验的 baseline 适配器，一行未改；其余组只多两样东西：只读挂载在 `/experience` 下的本题材料，以及提示词里的一段说明。

| 组 | solver 看到什么 | 次数 |
|---|---|---:|
| `control` | 只有题面和解题时限 | 15 |
| `failure_replay` | 本题失败轨迹 + gpt-5.6-luna / xhigh 用 v1 提示词写的经验（第一轮） | 10 |
| `traj_only` | 本题全部官方失败轨迹，不给分析 | 15 |
| `mem_<builder>` | 只有一份错题分析（贴进提示词，并挂载在 `/experience/memory`），不给轨迹 | 每组 15 |
| `trajmem_<builder>` | 本题全部失败轨迹 + 与 `mem_<builder>` 逐字节相同的错题分析 | 每组 15 |

写错题分析的 builder 共 7 个，都用 medium 思考：`luna56`（gpt-5.6-luna）、`terra56`（gpt-5.6-terra）、`sol56`（gpt-5.6-sol）、`sonnet55`（claude-sonnet-5-5）、`astra6`（gpt-6-astra）、`opus55`（claude-opus-5-5，v3 提示词）、`opus55v4`（claude-opus-5-5，v4 提示词：v3 加一条“不得推荐靠改动检查环境来过关的做法”）。另有两组同时段复测，单独报告：`v3r4`（`mem_sonnet55`）和 `tma1`（`traj_only`、`mem_opus55v4`）。

## 结果一览

| 写分析的模型 | 只给分析 | 轨迹 + 分析 |
|---|---:|---:|
| gpt-5.6-luna | 6/15 | 9/15 |
| gpt-5.6-terra | 8/15 | 6/15 |
| gpt-5.6-sol | 9/15 | 9/15 |
| claude-sonnet-5-5 | 9/15 | 9/15 |
| gpt-6-astra | 7/15 | 6/15 |
| claude-opus-5-5（v3）† | 12/15 | 12/15 |
| **claude-opus-5-5（v4）** | 10/15 | **12/15** |

对照组 1/15，只给轨迹 8/15，第一轮的 `failure_replay` 3/10。† v3 Opus 的 pypi-server 通过都装了 Python 启动钩子，照常计分并标明；不含灰色做法的最好结果是 Opus v4。解读见 [REPORT 第 0 节](REPORT.md#0-结论摘要)。

## 运行

这些命令依赖原始研究工作目录，当前发布的代码和 Hugging Face 数据包尚不足以从空目录重现全部 side experiment。除主实验环境、任务包和 `manifests/pool.json` 外，还需 TB2.1 的 `reports/official_test_task_means.csv`、`reports/official_test_trials.csv`，以及 `data/historical_trials/` 下与来源哈希匹配的原始轨迹和 `trial_metadata.json`。这些原始记录、生成的 `corpus/` 与完整 `runs/` 未随本次 GitHub 发布；已提交的 `manifests/` 是来源和结果索引，不是轨迹正文。

以下 run name 对应历史运行。已有工作目录中不要直接重跑 `select_tasks`、`build_corpus` 或历史批次：它们会重写选题/语料清单或拒绝覆盖已有 trial。新实验使用独立 checkout、已核对的输入和新的 run name；历史清单与三次作废重跑的证据单独保留。

在仓库根目录、用主实验的 TB2.1 环境运行。`distill`、`distill_v2`、`probe_models` 和求解会调用模型，其余命令不调用：

```bash
PY=experiment/benchmarks/terminal_bench_2_1/.venv/bin/python
M=side_experiments.hard_task_failure_replay.scripts
R=side_experiments/hard_task_failure_replay/runs

$PY -m $M.select_tasks                     # 按 config.json 的冻结规则选题
$PY -m $M.build_corpus                     # 渲染本题失败轨迹
$PY -m $M.run preflight                    # 官方参考解必须得 1 分
$PY -m $M.probe_models                     # 确认各 builder 可调用、实际作答的是请求的模型

# 第一轮：Luna v1 经验；对照组与 failure_replay 每题同时启动
$PY -m $M.distill
setsid nohup side_experiments/hard_task_failure_replay/scripts/launch_both_arms.sh r2_20261007 \
  > $R/launcher_r2.log 2>&1 < /dev/null &

# 第二至四轮：七个 builder 写分析；只给轨迹与只给分析的各组
$PY -m $M.distill_v2 --builders luna56 terra56 sol56 sonnet55 astra6 opus55
$PY -m $M.distill_v2 --builders opus55v4
$PY -m $M.distill_v2 --recollect opus55 dna-insert --accept-fallback     # 收下安全拒答后换模型的产物
$PY -m $M.distill_v2 --recollect opus55v4 dna-insert --accept-fallback
setsid nohup $PY -m $M.campaign --prefix v3r --rounds 3 --extra-first-round control \
  --arms traj_only mem_luna56 mem_terra56 mem_sol56 mem_sonnet55 mem_astra6 mem_opus55 mem_opus55v4 \
  > $R/campaign_v3.log 2>&1 < /dev/null &

# 第五轮：轨迹 + 分析，以及同时段锚点
setsid nohup $PY -m $M.campaign --prefix tmr --rounds 3 \
  --arms trajmem_luna56 trajmem_terra56 trajmem_sol56 trajmem_sonnet55 trajmem_astra6 trajmem_opus55 trajmem_opus55v4 \
  > $R/campaign_trajmem.log 2>&1 < /dev/null &
setsid nohup $PY -m $M.campaign --prefix tma --rounds 1 --arms traj_only mem_opus55v4 --parallel 4 \
  > $R/campaign_trajmem_anchor.log 2>&1 < /dev/null &

# 汇总与事后分析
$PY -m $M.summarize v3r1 v3r2 v3r2x v3r3 tmr1 tmr1x tmr2 tmr2x tmr3       # 总表，自动包含第一轮 r2、r3
$PY -m $M.run report --run-name tma1                                       # 锚点单独看
$PY -m $M.mechanisms r2_20261007 r3_20261007 v3r1 v3r2 v3r2x v3r3 v3r4 tmr1 tmr1x tmr2 tmr2x tmr3 tma1
python3 -m pytest -q side_experiments/hard_task_failure_replay/scripts/test_side.py
```

- 实际运行中第一轮用了 `r2_20261007`、`r3_20261007` 两个 run name。`v3r2x`、`tmr1x`、`tmr2x` 是三次按规则重跑的单格，原因见 REPORT 第 8 节。
- 第五轮的计分规则在开跑前写在 `manifests/trajmem_preregistration.json`。

## 目录

```text
side_experiments/hard_task_failure_replay/
├── README.md                 本文件：问题、组别、运行
├── REPORT.md                 完整报告
├── config.json               冻结参数：选题规则、solver、builders、组别说明、评分时限策略
├── prompts/                  v1/v3/v4 提炼提示词；各组的 solver 说明（preamble_*）
├── scripts/                  选题、渲染、提炼、builder 引擎、运行时、控制器、汇总、事后分析、测试
├── manifests/                选题、语料哈希、每份分析的来源、预检、模型探测、汇总结果、事后分析、第五轮预登记
├── corpus/<task>/            渲染后的失败轨迹与各 builder 的分析（生成数据，git 忽略）
└── runs/                     提炼尝试、求解 trial、日志（git 忽略）；runs/invalid/ 保存被作废并重跑的三次 trial
```

## 局限

- 这是同题回放，回答的是上限式问题：连本题自己的失败记录都给了，能不能做出来。它不是主实验那种对未见题的迁移。
- 5 道题、每格 3 次，组间差异大多在噪声范围内。选题时看过榜单成绩，v3 的提炼结构也是在看过第一轮评分结果后定的，不是留出集上的评估。
- filter-js-from-html 和 pypi-server 是主实验测试集的题。以后若在主实验里把它们当作“未见题”报告，应注明它们在本实验中出现过。

更多局限见 [REPORT 第 9 节](REPORT.md#9-局限)。删除本目录不影响主实验。
