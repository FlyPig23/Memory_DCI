# 数据与恢复

代码仓库只保留 V1 / V1.1 baseline、V5 工作流、配置和知识文档。
数据集位于 [FlyPig23/memory_dci](https://huggingface.co/datasets/FlyPig23/memory_dci)，公开下载无需 token。

数据分为两个压缩包：

| 文件 | 内容 |
| --- | --- |
| `inputs.tar.gz` | 固定 36/24 划分、432 条 training trajectories、381 张引用图片、任务和分数来源、24 道测试题的输入与独立评分资料、官方接口说明 |
| `evidence.tar.gz` | V1 / V1.1 / V5 各 24 次原始结果、转录与工具事件、V5 独立 memory、复盘决定、历史协议 |

每个文件及压缩包的 SHA-256 都记录在数据集的 `manifest.json`。
不上传鉴权文件、Codex home、缓存、重复 workspace、模型权重、V2–V4 或 Terminal Bench 数据。
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
