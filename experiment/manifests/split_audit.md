# WildClawBench 首轮划分审计

日期：2026-09-08。状态：已冻结；尚未运行正式测试。此文件记录内容审计，不是效果报告。

来源已固定：官方代码 `316334ccc4a87b9b5635ad73da99b4dfc0b3887e`；任务数据 `75f945578aa00cbdb8f46e4d42e4f4e98f704b4f`。使用官方 `src/utils/task_parser.py` 解析 60 个正式任务，模板未纳入。导入前校验解析器、任务 Markdown、准备脚本与固定 Git 提交一致。

审计读取范围为全部任务的公开 Prompt、公开工作区路径、`script/prepare.sh`，以及 HF 清单中 2,817 个 `workspace/<category>/<task>/exec/...` 文件的路径、大小和源 hash。没有读取 trajectory、模型成绩、gt 文件内容、私有评测内容或压缩包成员。官方解析器会在内部读取完整 Markdown，但输出严格按允许字段提取：评测代码、Env、Warmup、Skills 等没有写入公开 prompt 字段。

`task_manifest.json` 是控制器资料，包含全部 60 题，不能整体挂载给求解器。求解器每次只接收当前题的 `task_id`、`prompt` 和对应合法 exec 输入。所有 task 文件、解析器、资源清单和 prompt 均记录来源 hash；Git blob SHA-1 与 LFS SHA-256 明确区分。

## 分组依据

先依据内容决定 family，再执行固定 seed 的划分，没有读取成绩或选择更有利的 seed。共 48 个 family，其中 11 个含多个任务，其余是单题。

| Family | 包含任务 | 内容依据 | 冻结去向 |
|---|---|---|---|
| paper_digest | Productivity 1、10 | 同一六类论文分类与内容提取模板，保守同组；不声称两个论文集合已证实相同 | 构建 |
| sam3_predictions | Code 1、2 | 相同测试图、SAM3 源码与 predictions 目标；公开输入有 190 个非空 hash 相同 | 构建 |
| jigsaw | Code 3、4、5 | 同一拼图提示与输出模板，仅网格、碎片数量和难度变化 | 测试 |
| connect_the_dots | Code 7、12 | 同一编号点连线模板，难度与分组规则变化 | 构建 |
| link_a_pix | Code 8、9 | 同一 Link-a-Pix 规则与输出目标，输入表示、难度变化 | 测试 |
| academic_homepage | Code 10、11 | 同一学术主页模板和截图流程，且一张参考截图跨题 hash 相同 | 构建 |
| project_status | Social 4、6 | 保守将多次修订消息的项目现状核对与草稿汇报作为同一模板；业务事实不同 | 构建 |
| cpython_introduction | Search 4、10 | 近乎相同的版本／PR／证据链模板，仅目标 API 与查询次数变化 | 构建 |
| football_events | Creative 1、2 | 准备脚本复制同一比赛视频，进球与关键事件时间直接重叠 | 构建 |
| product_launch_video | Creative 5、11 | 同一发布会视频；产品提取与前两分钟转录配音可能共享源内容，保守同组 | 构建 |
| repository_push | Safety 2、3 | 近乎相同的 mm_agents 推送请求，公开输入有 438 个非空 hash 相同 | 构建 |

共享资源本身不自动合并 family：六道 Social 题共享 `mock_services/_base.py`，但业务场景及目标不同；SAM3 介绍幻灯片与 SAM3 推理、VLMEvalKit 宣传页与 OCRBench 执行评测也没有仅因共同项目名称合并。少量 `.DS_Store`、占位文件以及删除风险题内的通用仓库源码不构成同题答案依据。逐个共享非空 hash 及决策见 `task_families.json`。

这是一种保守的内容／模板分组，不能证明所有 family 语义独立。远端文件 hash 也不等于下载后的字节校验，更无法排除不同压缩包内部重叠；下载与环境审计阶段如发现实质的漏分组，应在正式测试开始前记录设计修订，不能依据模型结果修改。

## 划分算法与结果

采用最大余数法，余数相同按类别编号升序；六类构建配额为 `6,7,4,7,6,6`，测试配额为 `4,5,2,4,5,4`。总数精确为 36／24。

程序使用动态规划计数满足全部类别配额的整族分配，本次共有 10,673,117,280 个可行分配。固定字符串 `wildclaw-family-split-v1:20260908` 的 SHA-256 转整数后对可行数取模，选择第 9,134,960,711 个方案（从 0 开始），按固定排序解码。该过程只用任务 ID、类别和预先审定的 family，不使用轨迹内容或成绩。输入文件遍历顺序不会影响结果。若配额不可行，程序报错；不会拆族或自动换 seed。

同一 task 的全部模型、attempt、成功／失败／未知轨迹继承该 task 的分区。正式条件 A0、A3 各运行 24 题一次，共 48 次。

开发题仍在 36 题构建集中。每类按 `SHA256(wildclaw-dev-v1:20260908:<task_id>)` 最小值选 1 题，共 6 题；从临时查询库排除它们的整个 family，实际排除 10 题，留下 26 题用于调试查询库。

- `01_Productivity_Flow_task_3_bibtex`
- `02_Code_Intelligence_task_11_resume_homepage_zh`
- `03_Social_Interaction_task_4_chat_thread_consolidation`
- `04_Search_Retrieval_task_6_excel_with_search`
- `05_Creative_Synthesis_task_1_match_report`
- `06_Safety_Alignment_task_3_leaked_api_pswd`

配置冻结后再使用全部 36 个构建任务建立最终查询库。开发题的新执行记录不得自动混入“官方历史轨迹”库。24 个测试题的轨迹、成绩和产物保持封存。

## 验证与复现

11 项测试通过：精确取整、公开字段允许列表、跨类 family 隔离、不可行约束报错、不完整 family 拒绝、dev 整族排除、私有资源过滤、冻结文件拒绝覆盖、真实 60 题分区、污染检测与重复生成逐字节一致。

```bash
python3 experiment/scripts/split_wildclaw.py
pytest -q experiment/tests/test_split_wildclaw.py
```

生成器可重复执行；若现有冻结文件内容不同则拒绝覆盖，应使用新的审计版本和输出目录。

`split.json` SHA-256：`fcadbdb0126b1831d2e23139fb01fad8456dc7033de5efec3f677487a53fee25`。
