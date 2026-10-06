"""Report published historical scores only; never add test traces to the corpus."""
import csv
import hashlib
import html
import json
import statistics
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EXP = ROOT / 'experiment/benchmarks/wildclaw_bench'
OUT = EXP / 'reports/source_model_scores'
SNAP = EXP / 'trajectory library/WildClawBench/trajectory/hf_snapshot'


def main():
    split = json.loads((EXP / 'manifests/split.json').read_text())
    build = set(split['build_task_ids'])
    test = set(split['test_task_ids'])
    tasks = build | test
    manifest = json.loads((EXP / 'corpus/frozen_build/manifest.json').read_text())
    names = {e['model_alias']: e['model'] for e in manifest['episodes']}
    download = json.loads((EXP / 'manifests/download_trajectories.json').read_text())
    hashes = {f['path']: f['sha256'] for f in download['files']}

    def extract(alias):
        archive = next(SNAP.glob('output_' + alias + '.*'))
        records = []
        def consume(name, raw):
            parts = Path(name).parts
            matches = [(i, p) for i, p in enumerate(parts) if p in tasks]
            if len(matches) != 1:
                return
            i, task = matches[0]
            if len(parts) != i + 3:
                return
            score = json.loads(raw)
            metrics = {k: v for k, v in score.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            value = metrics.get('overall_score')
            assert isinstance(value, (int, float)) and 0 <= value <= 1, (alias, task, metrics)
            records.append(dict(model=names[alias], model_alias=alias, task_id=task,
                split='test' if task in test else 'build', overall_score=value, metrics=metrics,
                run_id=parts[i+1], archive=archive.name, member=name,
                archive_sha256=hashes[archive.name], score_sha256=hashlib.sha256(raw).hexdigest()))
        if archive.suffix == '.zip':
            with zipfile.ZipFile(archive) as z:
                for name in z.namelist():
                    if name.endswith('/score.json'):
                        consume(name, z.read(name))
        else:
            with tarfile.open(archive, 'r|gz') as z:
                for member in z:
                    if member.isfile() and member.name.endswith('/score.json'):
                        consume(member.name, z.extractfile(member).read())
        assert len(records) == 60 and len({r['task_id'] for r in records}) == 60, (alias, len(records))
        print(alias, '60/60 scores extracted', flush=True)
        return records

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = [r for group in pool.map(extract, sorted(names)) for r in group]
    lookup = {(r['model_alias'], r['task_id']): r for r in records}
    for e in manifest['episodes']:
        r = lookup[e['model_alias'], e['task_id']]
        assert r['overall_score'] == e['outcome']['metrics']['overall_score']
        assert r['score_sha256'] == e['outcome']['source']['sha256']
    summary = []
    for alias, name in names.items():
        rows = [r for r in records if r['model_alias'] == alias]
        summary.append(dict(model=name, model_alias=alias, count_60=len(rows),
            count_test=sum(r['split'] == 'test' for r in rows),
            mean_60=statistics.mean(r['overall_score'] for r in rows),
            mean_test_24=statistics.mean(r['overall_score'] for r in rows if r['split'] == 'test')))
    summary.sort(key=lambda r: -r['mean_test_24'])
    OUT.mkdir(parents=True, exist_ok=True)
    payload = dict(revision=manifest['dataset_revision'], metric='official score.json overall_score',
        aggregation='unweighted arithmetic mean of task scores; all runs retained',
        validated_build_scores=432, records=records, summary=summary)
    (OUT / 'scores.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n')
    with (OUT / 'scores.csv').open('w') as f:
        fields = ['task_id','split','model','overall_score','run_id','archive','member','score_sha256']
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore'); w.writeheader(); w.writerows(records)
    def table(headers, rows):
        lines = ['<table header-row="true">']
        for row in [headers] + rows:
            lines.append('\t<tr>' + ''.join('<td>'+html.escape(str(v))+'</td>' for v in row) + '</tr>')
        return '\n'.join(lines + ['</table>'])
    text = ['### Historical trajectory source models — 60-task scores（2026-09-25）',
        '我们使用的历史轨迹来自以下 **12 个模型**，每个模型覆盖 60 题，共 **720 条**。其中 36 个构建任务的 432 条进入检索库；24 个测试任务的 288 条历史轨迹不对 solver 开放。以下仅整理官方历史评分，未重新运行模型。',
        '**统计口径：** 读取固定快照中每个 task × model 的 `score.json → overall_score`（0–1）；均分按任务等权计算，保留零分与错误运行，不筛选成功轨迹。720 个组合均有评分，没有缺失。24 题严格使用项目 `split.json` 的 test task IDs。60 题均分是逐题平均，不声称等于官方榜单可能使用的其他汇总口径。',
        '**可比性：** 这些是官方历史 OpenClaw 运行，V1.1/V4 是我们自己的 Codex harness 运行；推理配置、预算、运行环境及 grader 条件不能假定相同。因此，即使模型名同为 GPT-5.6 Sol，也只能作参考，不能将分差直接归因于 DCI 或 skills。',
        '#### Source models and means',
        table(['来源模型','全量评分数','60 题均分','测试评分数','我们的 24 题测试集均分'],
            [[r['model'],'60/60',f"{r['mean_60']:.4f}",'24/24',f"{r['mean_test_24']:.4f}"] for r in summary]),
        '#### Every task × every model',
        '以下按模型展开，每个表包含全部 60 题；Test 标记表示该题属于我们的固定 24 题测试集。分数保留原始数值，未按执行状态改写。']
    for model in summary:
        alias = model['model_alias']
        rows = sorted([r for r in records if r['model_alias']==alias], key=lambda r:(r['task_id'][:2],int(r['task_id'].split('_task_')[1].split('_')[0])))
        text.extend(['<details>', '<summary>'+html.escape(model['model'])+' — 60 题逐题成绩</summary>',
            '\n'.join('\t'+line for line in table(['Task ID','Split','Official score'],[[r['task_id'],r['split'].title(),r['overall_score']] for r in rows]).splitlines()), '</details>'])
    text.extend(['#### Historical score traceability',
        f"- 数据快照：`internlm/WildClawBench-Trajectories@{manifest['dataset_revision']}`；分数来自本地 `output_<model>.tar.gz/.zip` 内的原始 `score.json`。",
        '- 本地完整数据与每条分数的 archive/member/SHA256：`experiment/benchmarks/wildclaw_bench/reports/source_model_scores/scores.json`；可导出表格：同目录 `scores.csv`。',
        '- 432 条构建集分数与原冻结 manifest 的分数及 score 文件哈希逐条一致；所有模型均恰好 60 题、其中 24 题为测试集。分析输出仅保存在 reports，未写入 solver 的 corpus 或 skills。'])
    (OUT / 'notion_section.md').write_text('\n\n'.join(text)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
