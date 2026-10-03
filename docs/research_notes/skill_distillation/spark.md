# SPARK：借用证据组织，不把源任务成功当作 skill 已验证

核查日期：2026-09-10。本文是 V4 方法调研，未执行 SPARK、未调用模型蒸馏、未运行 WildClawBench。外部 prompt / `SKILL.md` 只作为研究材料读取。

**建议借用 SPARK 的六类证据输入和执行反馈意识；不要直接接入其完整在线重试流程，也不要用 PDI 作为我们第一批离线 skills 的硬筛选指标。** 一个实际发布样例同时说明它值得借鉴，也说明生成后的 skill 必须另外检查。

## 1. 论文与代码分别做了什么

[论文 v2](https://arxiv.org/html/2605.09192v2) 的主流程是：教师在 Docker 任务中执行，外部 verifier 判定；失败后重写探索 memo 并重试，成功后生成 skill。memo 保存尝试、命令、已知事实、当前错误、下一策略。主实验主要比较同一任务上的有无 skill，另设重采样任务内容的迁移实验。它并不是单纯把现成轨迹压缩成摘要；在线探索和验证是完整方法的组成部分。文中的 teacher/student 表示生成与使用角色，不要求一定存在模型大小差异。

在固定提交的 [skill_evidence.py](https://github.com/EtaYang10th/spark-skills/blob/733f19a9ad6cedef06c3ab31aa1408c5e95bf9ac/spark_skills_gen/skill_evidence.py) 中，蒸馏输入按六块组织：

| 输入块 | 代码实际内容 | 对我们有用的部分 |
|---|---|---|
| Task Pattern | 原任务指令经长度限制 | 提取任务模式与约束，避免只保留题目名 |
| Execution Chain | 策略短句与筛选后的命令摘要 | 保留关键操作顺序；我们应补齐对应 observation |
| Verification | reward、通过测试、trial 标识 | 区分结果证据与模型自述 |
| Lessons | 历次尝试、重复失败、memo 中的注意事项 | 记录阻塞、修复与仍不确定的问题 |
| Environment | Dockerfile 中运行时、包、工具 | 写出操作适用前提 |
| Raw Support Tail | 成功运行 stdout 尾部 | 让摘要能返回原始记录核对；我们不宜只保留尾部 |

代码存在可直接看懂的启发式，而非通用语义证据抽取器：命令按关键词打分，选前 12 条后恢复执行顺序；命令摘要最多 180 字符，多行代码常被压为首行或预设短句；策略短句最多 12 条、每条最多 240 字符。`Confirmed Cautions` 也会从 memo 的 `Next Strategy` 抽句，名称本身不保证内容已被验证。这些都是设计参考，不应等同于逐条证据认证。

[summarizer.py](https://github.com/EtaYang10th/spark-skills/blob/733f19a9ad6cedef06c3ab31aa1408c5e95bf9ac/spark_skills_gen/summarizer.py) 将六块填入 prompt，记录 system/user/response 和 token 使用；[pipeline.py](https://github.com/EtaYang10th/spark-skills/blob/733f19a9ad6cedef06c3ab31aa1408c5e95bf9ac/spark_skills_gen/pipeline.py) 在源执行成功后生成并保存 skill。该保存路径没有在写入前要求对生成后的每段代码进行独立回放；下游 skill 评测属于另一阶段。

## 2. PDI 是什么，以及为何不宜直接移植

论文的离线 PDI 对三个词频分布信号标准化后做差：

`PDI = z(execution grounding) − z(plan copying) − z(memo ossification)`

执行 grounding 比较成功命令与最终 skill；plan copying 比较历次下一策略与最终 skill；ossification 比较相邻尝试的已知事实与失败测试集合。相似度基于加平滑后的词频分布及 Jensen–Shannon divergence。它是词汇分布诊断，并非逐条语义蕴含判断或 skill 正确性的证书；源成功轨迹包含的失败命令也不能因整条最终成功就逐条标为正确。[定义见论文 §4.2.4–4.2.5](https://arxiv.org/html/2605.09192v2#S4.SS2.SSS4)。

[在线实现 PDITracker](https://github.com/EtaYang10th/spark-skills/blob/733f19a9ad6cedef06c3ab31aa1408c5e95bf9ac/spark_skills_gen/context.py) 是另一个代理指标：比较命令与当前 `Verified Facts`、相邻 `Next Strategy`、相邻事实和失败测试；默认 `token_overlap`，可选 `js_divergence`。它在单任务历史内累计并标准化，以 warmup 和阈值触发提示干预。它不需要尚未生成的最终 skill，因此不能把它与论文离线 PDI 当成同一个计算。

**对 WildClawBench 的判断：** 当前 12 个模型对同一任务的独立轨迹不是同一教师连续反思的 memo 序列。不能把它们按模型顺序排列，伪造“前一次计划—后一次修订”，然后报告 PDI。缺少真实 memo / 连续失败测试时，应标为不可计算，而不是补写中间状态。可以借用“已执行操作优先于未执行计划”的原则，并建立逐条引用检查；此类本地检查需独立命名，不能声称复现 PDI。

## 3. 公开数据的真实覆盖与格式

固定 HF 数据版本为 `02e297a366c6a56eb754852cd15a78838546d965`。对[完整发布文件列表](https://huggingface.co/api/datasets/EtaYang10th/SPARK_PDI_Trajectory)计数，本次有 **69 个任务目录、69 个 trajectory.jsonl、64 个 attempts.json、50 个 SKILL.md**；这不等于论文所述 86 个任务全部提供了三件套。这里只下载了 `adaptive-cruise-control` 的三份文件，没有下载整个数据集。

[adaptive-cruise-control 样例](https://huggingface.co/datasets/EtaYang10th/SPARK_PDI_Trajectory/tree/02e297a366c6a56eb754852cd15a78838546d965/all_model_pdi/adaptive-cruise-control)的结构如下：

| 文件/字段 | 本次读取到的事实 |
|---|---|
| `attempts.json` | `task_name / max_retries / exploration_memo / memo_history / attempts / pdi` |
| 尝试结果 | 只有 attempt 0，一次 PASS，reward 1.0；memo 及 memo_history 为空 |
| `trajectory.jsonl` | 顶层只有三行：`execution_result`、`skill_gen_call`、`task_summary` |
| 原始命令流 | `execution_result.agent_stdout_full` 内嵌 JSONL；含 49 个已完成 command_execution，以及 agent_message、todo_list 等 |
| 验证 | 源 execution_result 记录 12/12 测试通过，并保存 test stdout、result JSON 和 CTRF JSON |
| 蒸馏调用 | `skill_gen_call` 保存 model、system_prompt、user_prompt、response、用量和时间 |
| 生成模型/执行框架 | 记录为 `claude-opus-4.6-thinking` / `codex`；这是发布记录中的字段，不是本次运行 |
| 输出一致性 | response 去首尾空白后与发布的 `SKILL.md` 完全相同 |

这个样例很适合研究输入到输出的链路，**不适合证明跨尝试 PDI**，因为它没有反思序列。数据卡描述了命令事件，但实际解析时必须先识别顶层记录，再解析内嵌 stdout，不能假设每一行直接是一个工具调用。

## 4. 沿一个真实案例检查，发现了什么

以下是对固定版本样例的静态核对，未运行它的控制器或验证器。机器可查证摘要保存于 inspection.json（历史文件：`archive/skill_distillation_research_20260910/spark/inspection.json`，不在精简发布中）。

**可追溯的正面例子。** 原执行中初版 PID 的超调较高，agent 检查积分项、加入 anti-windup，随后继续调整。最终 skill 将 anti-windup 放到核心流程和错误说明中。原始记录同时保留动作、诊断输出和后续指标，具备提炼“症状—原因假说—改动—观察结果”的材料。注意这支持历史修复过程，不等于独立消融已证明该改动是唯一原因。

**同一次最终成功中也有中间失败。** 49 个命令包括被终止的搜索、格式字符串错误、pytest 缺失以及未收集到测试。若只读 attempts 的 PASS 标签，会丢掉这些局部失败。我们的提炼单元应覆盖完整操作和结果，而不是按整条成功/失败统一染色。

**输入混有先写 skill 的任务要求。** 生成 prompt 的 Task Pattern 保留了源任务的 `Generate Skills First` 指令；原执行也先在 `environment/skills/` 写了五份知识文档，之后才完成解题。外层发布的最终 SKILL.md 是成功后的另一次生成。两者须区分，不能把该样例称为“全程只从已验证后验内容形成 skill”，也不能把嵌入的源任务指令作为我们的研究工作指令执行。

**生成结果并不等于已验证结果。** 发布的最终 skill 有 706 行、25,952 字节：

- 标为完整的 `Reference Implementation` 在最后的 `PIDController.compute()` 初始分支处结束；只有 25 个三反引号围栏，最后一个未闭合。无法从这份文件证明“完整、可直接运行”的承诺成立。文件与保存的模型 response 一致；本次不能确定是模型长度限制还是其他原因造成。
- 源配置在 `acc_settings` 下使用 `min_distance`，skill 的第 62、196、680 行却读取 `minimum_gap`。直接使用原配置会产生字段不匹配。这是生成时的具体漂移，不是领域知识判断。
- 仍包含特定任务的数值、1501 行输出、固定文件名和调好的 PID gains。它能作为同类任务参考，但不宜把这些数值作为跨任务规则无条件采用。
- YAML 头使用 `title/category/domain/tags/dependencies`；并没有完整的通用 `name/description` 发现字段。若未来要做原生 skill 自动发现，需要单独规范格式；V4 若只是 DCI 读取普通文件，则无需自动注册这些外部文件。

查看[原始样例文件](https://huggingface.co/datasets/EtaYang10th/SPARK_PDI_Trajectory/blob/02e297a366c6a56eb754852cd15a78838546d965/all_model_pdi/adaptive-cruise-control/SKILL.md)或本地原样副本（历史文件：`archive/skill_distillation_research_20260910/spark/sample/SKILL.md`，不在精简发布中）可复核。上述问题只代表本次抽样，不估计整个数据集的缺陷率，也不否定论文总体评测结果。

## 5. 对 V4 的具体建议

1. **先做离线证据编译。** 只用构建集，把每条来源拆为任务条件、工具动作、完整结果、可观察检查、局部失败/修复、环境前提，并保留源路径、事件/行区间、SHA256。不要把生成过程中的计划当作已执行事实。
2. **skill 写可迁移操作，不复述源答案。** 将固定人名、答案、URL 实例、路径、参数替换为输入槽位；有必要保留的实例放入明确标注的参考案例，另附适用条件。
3. **保留来源证据和原轨迹检索入口。** 短 skill 提供触发条件、流程和验证；复杂代码放独立参考文件，连接到完整操作—结果片段。不要为追求短文本再次截掉验证结果。
4. **把几种证据状态分开记录。** `source_verified` 表示源任务/局部动作有观测依据；`statically_checked` 表示新 skill 的字段、引用、围栏等检查通过；`replay_verified` 必须有针对新 skill 的实际回放；`transfer_evaluated` 需未参与蒸馏的数据。前三者不能替代最后一项。
5. **把生成 skill 的检查做在发布前。** 对不存在引用、被截断的代码、配置键名漂移、没有适用条件的固定数值、没有观察证据的“已验证修复”退回修改。若当前不做回放，发布说明要保留“有来源依据，尚未回放”的状态。
6. **跨模型对比保留关联性。** 同一任务的多模型轨迹可用于寻找共同操作、冲突条件和不同失败，但它们不是 12 个独立任务的迁移证据；合并 skill 时保留 task/model 的来源列表。

这是对我们离线设定的设计建议，不是声称完整复现 SPARK。V4 的在线执行仍使用现有原生 Codex 工具读取 skills 和 trajectories；SPARK 的任务生成器、Harbor 调度、在线 PDI 干预都不需要为首版一并引入。

## 6. 固定来源、许可元数据与本地文件

- 论文：arXiv `2605.09192v2`，修订日期 2026-06-03；[PDF 来源](https://arxiv.org/pdf/2605.09192v2)。本地 [SPARK_2605.09192.pdf](https://arxiv.org/pdf/2605.09192v2)，SHA256 `cca13cd5e818cc99dbf0d3c26f9119399d5fe5a5b922687c5975e2d4bc0f8dab`。论文页面标注 CC BY 4.0。
- 代码：`EtaYang10th/spark-skills`，提交 `733f19a9ad6cedef06c3ab31aa1408c5e95bf9ac`。本次完整树未发现 LICENSE 文件，GitHub API 的 license 为 null；只静态阅读相关源文件，不将 HF 的 MIT 标签推及整个代码仓库。
- 数据：`EtaYang10th/SPARK_PDI_Trajectory`，提交 `02e297a366c6a56eb754852cd15a78838546d965`；[数据卡](https://huggingface.co/datasets/EtaYang10th/SPARK_PDI_Trajectory/blob/02e297a366c6a56eb754852cd15a78838546d965/README.md)标注 MIT。
- 阅读用 HTML、元数据、代码和单个数据样例均在 归档目录（历史文件：`archive/skill_distillation_research_20260910/spark`，不在精简发布中）。三个 `*_download_manifest.json` 记录来源 URL、字节数与 SHA256；`inspection.json` 保存本次静态核对结果。未安装或执行外部技能。
