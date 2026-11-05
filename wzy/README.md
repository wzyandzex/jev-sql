# wzy/schema-linking-analysis（王铮宇的分析代码与报告）

2026-11-05 在 BIRD mini-dev（SQLite，498 个唯一 question_id）上运行的 JEV schema linking 实验
（实验1 `dev_tc`、实验2 `dev_tc_no_mention`、实验3 `dev_tc_filter`）的离线分析、参照基线与组会报告。
仓库其他目录保持师兄原样，未做任何修改。

## 目录

```text
wzy/
├── reports/2026-11-06_jev_schema_linking_report.md   周五组会报告（结论、置信区间、瓶颈分析、下游 EX）
├── analysis/
│   ├── sl_lib.py            公共库：gold 抽取、运行记录加载、指标、校准、bootstrap
│   ├── analyze_runs.py      主分析：生成 results.json（默认写到脚本目录）
│   ├── render_tables.py     把 results.json 渲染成 tables.md
│   ├── results.json         2026-11-05 全量运行的分析结果
│   ├── tables.md            对应的 markdown 表格
│   ├── sql_ex.py            下游 EX：按各方法选出的 schema 让 qwen 写 SQL 并执行比对
│   ├── sql_ex_summary.json  EX 结果（11 种 schema 输入 × 498 题）
│   ├── sql_ex_tables.md     对应的 markdown 表格
│   └── configs/exp2b_no_mention_tab0.1.yaml   补充实验 2b 配置（表 Noul 阈值 0.1）
├── baselines/llm_schema_linking.py   qwen 一次调用直接选列的参照基线
├── notes/                   中长期调研笔记（jev-like 开源决策模型与微调；SQL 模板化生成）
│   └── scripts/skeleton_stats.py     mini-dev gold SQL 骨架统计
└── run_exp.sh               批量运行脚本（循环 run_batch.py --resume 直到全部成功）
```

## 复现

Python 3.11，依赖与 `requirements.txt` 相同（sqlparse、PyYAML、requests、langchain-openai 等）；
`wzy/notes/scripts/skeleton_stats.py` 另需 `sqlglot`（笔记用的是 30.29.0）。
`.env`、`outputs/`、`BIRD/` 按主 README 准备（不入库）。

```powershell
# 某个实验的全量运行（失败自动重跑）
./run_exp.sh <worktree> configs/pipeline_minidev_table_first_no_mention.yaml <log>

# 官方评测 / 错误分析（师兄原脚本）
python scripts/evaluate_schema_linking.py --config configs/...
python scripts/analyze_errors.py --config configs/...

# 离线分析（只读 outputs/runs/*/batch_answers.jsonl，不调用任何 API）
cd wzy/analysis
python analyze_runs.py                # -> results.json
python render_tables.py > tables.md

# 下游 EX（调用 .env 里的生成模型；在途请求硬上限 5，已完成的题自动跳过）
python sql_ex.py --variants full gold exp1_mention exp2_query exp3_ambiguous llm_direct+fk
```

`analyze_runs.py --runs` 可选 exp1_mention / exp2_query / exp3_ambiguous / exp2b_tab0.1 / llm_direct，
与 `outputs/runs/` 下的目录名对应（映射见 sl_lib.py 的 RUNS）。

## 主要结论（详见报告）

- 列 F1：实验1 84.5、实验2 86.5、实验3 87.3（与 `evaluate_schema_linking.py` 一致）。
- 下游 EX（qwen3.8-flash 单次生成，496 题）：全 schema 63.1、gold schema 71.6；三个实验在当前阈值下都低于全 schema
  （57.9 / 55.6 / 58.7），原因是漏列；列阈值放宽到 0.5 后 63.5，与全 schema 打平，prompt 只有 1/4。
- 阈值应按下游 EX 或列全召回率选，不能按 F1 选；qwen 一次调用 + 同样的外键补全在 F1 和 EX 上都不比实验3 差，
  所以 JEV 的价值要靠可调概率、校准、与 LLM 互补和大 schema 来体现。
