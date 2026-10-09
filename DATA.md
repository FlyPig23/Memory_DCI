# V8 数据与来源

V8 的固定输入和结果证据发布在公开数据集 [FlyPig23/memory_dci](https://huggingface.co/datasets/FlyPig23/memory_dci/tree/main/terminal_bench_4) 的 `terminal_bench_4/` 子目录。下载无需 Hugging Face token；下载器默认固定到发布 commit `db28540352886750100bd94e45a09bb80adae92b`。GitHub 保存当前 V8 代码、运行说明、协议和报告。

数据集内已有的 `wildclaw_bench/` 与 `terminal_bench_2_1/` 保留为历史数据归档，当前代码不再提供这些旧工作流。查看旧数据时应使用对应历史 Git 提交。

## 下载

从项目根目录执行；只恢复与核对文件，不启动 Docker 或调用模型：

```bash
# 固定任务、查询池、seed、来源清单与历史源码快照。
python3 -m experiment.benchmarks.terminal_bench_4.scripts.download_data

# 同时恢复精简的结果与审计证据。
python3 -m experiment.benchmarks.terminal_bench_4.scripts.download_data --include-evidence

# 核对已恢复文件；有证据时重算 66/175 与官方 17/175。
python3 -m experiment.benchmarks.terminal_bench_4.scripts.download_data --verify-only --include-evidence
```

`--root /absolute/path` 可恢复到独立仓库根目录；`--local-dataset /path/to/terminal_bench_4` 用于本地已经下载的档案；`--revision <Hugging-Face-commit>` 显式选择发布版本。恢复不重新抽题，保留固定题目 ID、题包版本及顺序。校验清单记录压缩包与逐文件 SHA256；恢复拒绝覆盖内容不同的现有文件，`--verify-only` 离线检查已经恢复的数据。

## 数据内容

| 文件 | 内容 |
|---|---|
| `metadata.tar.gz` | 冻结榜单、全部官方 trial 元数据、36 题选取结果、去污染决定及审计清单。 |
| `tasks.tar.gz` | 36 个固定版本的官方任务包，包括用于评分和参考解预检的文件。 |
| `corpus.tar.gz` | `final_20261008` 的逐题查询池与 seed：3,755 条失败轨迹、36 题共 291 条初始经验。 |
| `sources.tar.gz` | 历史实际运行所用源码的可用快照及来源对应关系。 |
| `evidence.tar.gz` | 两段正式运行的 175 次得分与有效性回执、可分享的求解/复盘记录、memory、建库日志及审计引用的提交产物与其他证据；通过 `--include-evidence` 获取。 |
| `manifest.json` | 档案与文件校验、原始/公开版本哈希及处理记录。 |

五个压缩包共约 4.54 GB（含可选证据）。[task_selection.json](https://huggingface.co/datasets/FlyPig23/memory_dci/blob/main/terminal_bench_4/task_selection.json) 可直接查看固定的 36 个入选 ID 与 35 个已评估 ID；[results_summary.json](https://huggingface.co/datasets/FlyPig23/memory_dci/blob/main/terminal_bench_4/results_summary.json) 提供结果摘要。

解压后进入 `experiment/benchmarks/terminal_bench_4/` 下的 `manifests/`、`prepared/`、`runs/` 和 `source_snapshots/` 等目录。任务包、查询池、运行结果、运行环境及认证文件不提交 GitHub。

36 道入选题中，cad-model 的官方参考解预检未通过，因此实际求解为 **35 题 × 5 次**。查询池和 seed 仍保留入选的全部 36 题；不能把 cad-model 从输入清单静默删掉，或把未执行的 5 次写成真实零分。查询池只含同题、同版本、已评分且得 0 的轨迹：3,775 条来源中，10 条没有正文、10 条经去污染排除，最终 3,755 条。成功轨迹不会挂载给 solver。

## 历史证据与当前代码

当前发布将 V8 依赖从旧 TB2.1 路径迁到 `terminal_bench_4/engine/`，安装与运行入口也独立放在 TB4 下。内部部分 `v7_*` 名称沿用运行时实现的历史名称，不代表另保留一套 V7 实验。模型提示词与工具语义沿用原实现。

冻结协议、官方得分和审计记录没有为了适配新目录而改写。它们的源文件路径及 SHA256 仍指向**实际执行时**的文件。原源码快照恢复到 `source_snapshots/v8_original/<原仓库相对路径>`，不要把当前迁移后的代码哈希填回历史协议，也不要用当前代码续跑冻结的旧目录；新实验应使用新 run name，重新冻结当前源码、模型和输入。

源码快照共保存 28 份来源文件，存在一项历史缺口：第 1–3 次运行冻结的控制器版本目前未找到；第 4、5 次运行的控制器及已核对的 solver / builder 源文件单独保存。公开包可以核对保存的输入、得分与可用源码，不宣称完整恢复了缺失的历史控制器。其余发布映射与缺项以数据清单为准。

证据包是精简公开导出，保留 175 份原始评分回执、主求解与复盘记录、memory 和报告明确引用的 121 个来源文件；不包含认证文件、私有 home、未被审计引用的大型提交产物或全部工作区缓存。公开恢复目录已经通过哈希验证、原有效性检查与离线结果复算：66/175、官方 17/175，以及 158 条官方 Sol 失败记录进入查询池。这是保存结果的复核，没有重新求解或重新评分。涉及凭据的处理另记原始与公开文件哈希；如果文件经过处理，不能再声称其公开正文与历史私有文件逐字节相同。本次公开处理仅在 5 份会话日志中各清理 1 处凭据样式匹配，记录在 `redactions.json`；冻结题包、查询池、seed、协议、评分回执及 121 份明确引用源均保持原字节。报告中保存的原始哈希不因公开导出而替换，来源映射用于对应二者。

## 来源与使用边界

任务、榜单及失败轨迹来自 [Harbor Hub](https://hub.harborframework.com) 的 Terminal-Bench 4.0（`terminal-bench` 包第 4 版）。具体题包 revision、内容哈希、来源 trial ID 与下载地址保存在冻结清单及导出来源记录中。重复的上游原始下载文件 `data/historical_trials/` 不另行打包；需要从头重建语料时，可用 `v8_corpus download` 按冻结清单重新获取。重新获取官方原文时，应使用这些固定来源，不以今天的榜单或重新选题替换本轮输入。

保留任务文件和素材自带的来源声明。任务包、第三方历史轨迹、模型生成分析和其他素材各有来源；本项目不为它们统一追加 Apache 或其他再许可。凭据清理不改变上游权利归属。

任务包包含隐藏评分与参考解，供宿主机进行预检和独立评分。运行时只将本题允许的查询池、seed 与任务环境提供给 solver。结果审计记录和成功运行证据用于检查报告，不作为新的求解材料。

66/175 是官方评分结果，其中 bun r1 暴露出可逆编码的测试覆盖缺口，保留原分并单列说明。官方 Sol 的 158 条失败全部进入本题查询池，历史成绩既参与选题又提供材料，不能作为独立留出的对照。详见 [V8 报告](experiment/benchmarks/terminal_bench_4/docs/V8_REPORT.md)与 [协议](experiment/benchmarks/terminal_bench_4/docs/PROTOCOL.md)。
