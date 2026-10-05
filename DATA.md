# 数据与恢复

代码仓库只保留 V1 / V1.1 baseline、V5 工作流、配置和知识文档。
数据集位于 [FlyPig23/memory_dci](https://huggingface.co/datasets/FlyPig23/memory_dci)，公开下载无需 token。

## TerminalBench 2.1 数据

同一个 [Hugging Face 数据集](https://huggingface.co/datasets/FlyPig23/memory_dci/tree/main/terminal_bench_2_1) 的 `terminal_bench_2_1/` 子目录保存 TB2.1 revision 6 的任务包、训练历史查询池及固定 **53 training / 36 test** 清单。下载、校验和运行见 [TB2.1 说明](experiment/benchmarks/terminal_bench_2_1/README.md)。下方原有数据说明均属于 WildClawBench 的 **36 training / 24 test**，两套划分独立保存。

WildClawBench 数据分为两个压缩包：

| 文件 | 内容 |
| --- | --- |
| `inputs.tar.gz` | 固定 36/24 划分、432 条 training trajectories、381 张引用图片、任务和分数来源、24 道测试题的输入与独立评分资料、官方接口说明 |
| `evidence.tar.gz` | V1 / V1.1 / V5 各 24 次原始结果、转录与工具事件、V5 独立 memory、复盘决定、历史协议 |

每个文件及压缩包的 SHA-256 都记录在数据集的 `manifest.json`。
不上传鉴权文件、Codex home、缓存、重复 workspace、模型权重，以及废弃的 V2–V4 / TerminalBench 4.0 数据。
完整原始会话和 workspace 未包含在精简导出中，因此完整历史审计仍需本地原始证据。

公开导出会清除检测到的凭据和私钥内容；`redactions.json` 只记录文件路径、替换类别和修改前后的哈希。
上游安全测试轨迹中出现的私钥块也会移除，并同步更新导出 corpus 的内容哈希。
因此公开数据属于经过清理的复现版本，与原实验输入并非逐字节相同；原始分数不据此改写。

## 下载

在新克隆的代码仓库根目录运行：

```bash
python3 scripts/download_data.py --fetch-benchmark
python3 -m experiment.variants.dci_memory.pool
```

需要检查历史运行记录时，再加 `--include-evidence`：

```bash
python3 scripts/download_data.py --include-evidence
python3 scripts/report_results.py
```

可用 `--revision <dataset-commit>` 固定数据版本。下载器会验证压缩包和逐文件哈希，
恢复原相对路径，并为 24 道测试题建立独立的公开输入目录及 grader-only ground-truth 引用。
它不会覆盖内容不同的现有文件；请使用新 checkout 或 `--root` 指定空目标目录。
已下载的包可用 `--local-dataset /absolute/path/to/dataset` 离线验证和恢复。

Docker、Codex 登录和实验镜像须按运行文档单独准备；数据恢复不会启动模型或重新评分。
历史冻结协议放在 `experiment/evidence/frozen_protocols/`，新实验使用当前工作流重新 prepare。

## 固定同一组 36 / 24 任务

划分已经上传，不能只按 seed 重新抽样：

- [split.json](experiment/manifests/split.json) 的 `build_task_ids` 是固定的 36 个训练任务，`test_task_ids` 是固定的 24 个测试任务；该文件也包含在 Hugging Face 的 `inputs.tar.gz` 中。
- `inputs.tar.gz` 中的 `task_manifest.json` 保存任务 ID、类别与输入来源；`task_families.json` 保存近重复任务的 family 分组。
- 测试顺序取自 [formal_protocol.json](experiment/manifests/formal_protocol.json) 中 `schedule` 的 A0 项。V1、V1.1、V5 沿用这个顺序，准备阶段检查任务集合是否与 `test_task_ids` 一致。

| 类别 | 训练 | 测试 |
| --- | ---: | ---: |
| Productivity Flow | 6 | 4 |
| Code Intelligence | 7 | 5 |
| Social Interaction | 4 | 2 |
| Search Retrieval | 7 | 4 |
| Creative Synthesis | 6 | 5 |
| Safety Alignment | 6 | 4 |
| 合计 | 36 | 24 |

给新的运行者使用独立 checkout，固定到已验证的数据集提交：

```bash
python3 scripts/download_data.py \
  --revision 579f094855032f95bb7b380f1914f6f157b23107 \
  --fetch-benchmark
```

复跑只需下载 inputs；不要加 `--include-evidence` 将旧结果放进新运行目录，也不要重新执行 `split_wildclaw.py`。环境就绪后按 [实验运行入口](experiment/README.md) 启动所需版本。Docker、镜像、推理服务和操作者自己的登录仍需准备。

可在项目根目录独立核验清单，以下命令不调用模型：

```bash
python3 - <<'PYVERIFY'
import hashlib
import json
from pathlib import Path

path = Path("experiment/manifests/split.json")
assert hashlib.sha256(path.read_bytes()).hexdigest() == "fcadbdb0126b1831d2e23139fb01fad8456dc7033de5efec3f677487a53fee25"
split = json.loads(path.read_text())
assert len(split["build_task_ids"]) == len(set(split["build_task_ids"])) == 36
assert len(split["test_task_ids"]) == len(set(split["test_task_ids"])) == 24
assert set(split["build_task_ids"]).isdisjoint(split["test_task_ids"])
protocol_path = Path("experiment/manifests/formal_protocol.json")
assert hashlib.sha256(protocol_path.read_bytes()).hexdigest() == "809fe8dcff3b585e48d5ca8a8d6b4c17823a397222a5ff9749400312192779e7"
protocol = json.loads(protocol_path.read_text())
order = [row["task_id"] for row in protocol["schedule"] if row["condition"] == "A0"]
assert len(order) == 24 and set(order) == set(split["test_task_ids"])
print("Verified: original 36 training tasks and 24 test tasks")
for index, task_id in enumerate(order, 1):
    print(f"{index:02d} {task_id}")
PYVERIFY
```

原划分文件里的 `formal_conditions: ["A0", "A3"]` 和 `formal_solve_count: 48` 是首轮两组条件的历史记录，不表示有 48 道测试题。当前测试题以 `test_task_ids` 中的 24 个 ID 为准。

固定清单保证任务身份与顺序一致；模型、工具服务和外部网页等运行条件仍会影响新一次执行的结果。公开训练语料的脱敏不改变 36/24 任务划分。

## 数据源

| 来源 | 固定版本 |
| --- | --- |
| [WildClawBench 代码](https://github.com/InternLM/WildClawBench) | `316334ccc4a87b9b5635ad73da99b4dfc0b3887e` |
| [WildClawBench 任务](https://huggingface.co/datasets/internlm/WildClawBench) | `75f945578aa00cbdb8f46e4d42e4f4e98f704b4f` |
| [官方 trajectories](https://huggingface.co/datasets/internlm/WildClawBench-Trajectories) | `d2816016a7a7b41fa6b7ba368b28ddafcb54fd93` |

上游代码和数据卡标明 MIT；数据集附带来源说明及上游许可证。
题目中第三方资产仍遵循其原始权利条件，项目不另行声明统一授权。
原始上游快照、下载清单中的固定 revision 可用于追溯；复现 24 道正式题无需重新下载整个数据集的镜像和训练题视频。

## 重新打包

在保有原始输入及 72 次实验记录的机器上运行：

```bash
python3 scripts/package_dataset.py --destination /absolute/path/outside/repository
```

打包器只读取明确允许的路径，清理公开副本并检查实际导出字节；不改写原始实验。
它生成压缩包、文件清单、清理记录、来源说明和 Hugging Face 数据卡，不执行上传。
