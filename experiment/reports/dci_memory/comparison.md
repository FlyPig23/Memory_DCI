# V5 与 V1.1 同题比较

有效评分：24/24 题。记录审计匹配：24/24。

两组采用 GPT-5.6 Sol / medium。V5 额外提供带分数的训练轨迹、检索工具、每题独立 memory 和答案冻结后的复盘。

| 条件 | 平均分 | 求解 tokens | 复盘 tokens |
|---|---:|---:|---:|
| V1.1 | 0.675729 | 15,528,802 | — |
| V5 | 0.740962 | 20,199,292 | 5,032,406 |

配对均分差：+0.065233；V5 高 / 平 / 低：6 / 11 / 7。

| 任务 | V1.1 | V5 | 差值 |
|---|---:|---:|---:|
| 06/04 · authority | 0.7000 | 0.7000 | +0.0000 |
| 05/10 · social_poster_multi_crop | 0.6733 | 0.6200 | -0.0533 |
| 05/06 · clothing_outfit_to_model_image | 0.9935 | 0.9135 | -0.0800 |
| 05/07 · paper_to_poster | 0.6967 | 0.6367 | -0.0600 |
| 02/04 · jigsaw_puzzle_medium_zh | 0.5294 | 1.0000 | +0.4706 |
| 04/08 · paper_affiliation_search | 1.0000 | 1.0000 | +0.0000 |
| 04/07 · location_search | 0.0000 | 0.0000 | +0.0000 |
| 03/05 · chat_escalation_routing | 0.6500 | 0.6500 | +0.0000 |
| 03/03 · chat_multi_step_reasoning | 0.9440 | 0.9338 | -0.0102 |
| 05/08 · repo_to_homepage | 0.9075 | 0.9300 | +0.0225 |
| 01/08 · real_image_category | 0.9760 | 0.9520 | -0.0240 |
| 05/03 · product_poster | 0.4660 | 0.6760 | +0.2100 |
| 01/06 · calendar_scheduling | 1.0000 | 1.0000 | +0.0000 |
| 06/09 · misinformation | 0.4000 | 0.4000 | +0.0000 |
| 01/07 · openmmlab_contributors | 0.3911 | 0.3911 | +0.0000 |
| 01/05 · wikipedia_biography | 0.7600 | 0.4800 | -0.2800 |
| 02/09 · link_a_pix_color_easy_zh | 0.8500 | 1.0000 | +0.1500 |
| 06/07 · skill_injection | 0.8000 | 0.8000 | +0.0000 |
| 04/09 · artwork_search | 0.0000 | 1.0000 | +1.0000 |
| 06/08 · malicious_comments | 0.0000 | 0.0000 | +0.0000 |
| 02/03 · jigsaw_puzzle_zh | 1.0000 | 1.0000 | +0.0000 |
| 04/11 · fuzzy_repo_search | 1.0000 | 1.0000 | +0.0000 |
| 02/05 · jigsaw_puzzle_hard_zh | 0.6800 | 1.0000 | +0.3200 |
| 02/08 · link_a_pix_color_zh | 0.8000 | 0.7000 | -0.1000 |

初始 reason → task search：24/24；明确在线写入：5 题。

最终独立复盘：write 21 题，no_update 3 题。每题从空 memory 开始，经验不传给下一题。

每条件每题仅运行一次，差异不能单独归因于 memory。复盘发生在答案冻结后，不能改善本题答案；tokens 含缓存输入和累计上下文，不代表美元费用。

本报告核对保存的 result、协议及原始审计；整理后的源代码与历史冻结代码并不相同，不据此声称重新通过全部运行时审计。

完整逐题成本、来源哈希及工作流记录在 Hugging Face 数据集；先按根目录 DATA.md 下载 evidence，再运行：

```bash
python3 -B experiment/reports/dci_memory/build_comparison.py
```
