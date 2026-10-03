# V5：DCI + 每题独立 memory

状态：complete；有效评分 24/24。

36 个 training tasks / 432 条带分数命名的原始轨迹只读；24 个 test tasks 每题从空 memory 开始，不跨题传递。
GPT-5.6 Sol / medium，单次 solver rollout；求解中可以反复检索和蒸馏，结束后独立复盘，再按原规则评分。

最终复盘发生在答案冻结之后，不计为帮助本题。token 为累计 input+output（包括缓存输入），不代表订阅扣费。

| 指标 | V5 |
|---|---|
| 平均分 | 0.7410 |
| 平均求解 tokens | 841637.1667 |
| 平均求解秒数 | 329.4498 |
| 平均复盘 tokens | 209683.5833 |
| 平均复盘秒数 | 68.9890 |
| 平均总 tokens | 1051320.7500 |
| 平均总秒数 | 398.4389 |
| solver 结束快照有 memory 条目的题数 | 11.0000 |

完整逐题对比：`per_task.csv`（见 [数据与恢复](../../../DATA.md)）；原始汇总：`summary.json`（见 [数据与恢复](../../../DATA.md)）。

V5 同时改变评分可见性、检索策略、工具接口和每题 memory，不能单独将分差归因于 memory。

实际在线写入以 `stage=online` 的 write 事件为准；快照还可能包含 solver 的 `stage=final` 写入。详见 [同题对照与 memory 行为统计](comparison.md)。
