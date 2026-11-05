"""Render markdown tables from analyze_runs.py output.

Usage:
    ../.venv/Scripts/python.exe render_tables.py [results.json] > tables.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LABELS = {
    "exp1_mention": "实验1 先表后列 + mention",
    "exp2_query": "实验2 无 mention（query 打分）",
    "exp3_ambiguous": "实验3 无 mention + 模糊列二次判定",
    "exp2b_tab0.1": "补充 2b：实验2 + 表阈值 0.1",
    "llm_direct": "参照：qwen 直接选列",
    "llm_direct+fk": "参照：qwen 直接选列 + 外键补全",
    "exp2|llm+fk(union)": "组合：实验2 ∪ qwen+外键",
    "exp2&llm+fk(intersection)": "组合：实验2 ∩ qwen+外键",
}
CONTRASTS = {
    "drop mention extraction (exp2 - exp1)": "去掉 mention（实验2 − 实验1）",
    "ambiguous band vs hard 2.0, different JEV draws (exp3 - exp2)": "模糊带 vs 硬阈值 2.0，不同 JEV 调用（实验3 − 实验2）",
    "ambiguous band vs hard 2.0, same JEV draws": "模糊带 vs 硬阈值 2.0，同一批分数（实验3 − 实验3 反事实）",
    "accepted band columns only, same JEV draws": "二次判定接受的列的贡献（实验3 − 实验3 拒绝整个带）",
    "noise floor: same rule, two JEV runs": "噪声下限：同一规则、两次 JEV 调用",
    "direct LLM linking vs exp2": "qwen 直接选列 − 实验2",
    "direct LLM linking + FK closure vs exp2": "qwen+外键 − 实验2",
    "direct LLM linking + FK closure vs exp1": "qwen+外键 − 实验1",
    "union of exp2 and LLM+FK vs exp2": "实验2 ∪ qwen+外键 − 实验2",
    "table Noul threshold 0.1 vs 0.5 (exp2b - exp2)": "表阈值 0.1 vs 0.5（2b − 实验2）",
}
RULES = {
    "actual_extra_round": "实验3 实际（≥2.5 直接入选 + 带内二次判定）",
    "reject_whole_band(score>=2.5)": "拒绝整个带（只保留 ≥2.5）",
    "hard_2.0": "硬阈值 2.0（即实验2 的规则）",
    "accept_whole_band(score>=1.0)": "接受整个带（保留 ≥1.0）",
}


def pct(x, digits=1):
    return "-" if x is None else f"{100 * x:.{digits}f}"


def main_table(r):
    rows = ["| 方法 | 列 P | 列 R | 列 F1 | 列全召回率 | 表 R | 表全召回率 | 表+列 EM | 平均列数 | 保留列占全库比例 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, m in {**r["final"], **r.get("combos", {})}.items():
        c, t = m["column"], m["table"]
        rows.append(f"| {LABELS.get(name, name)} | {pct(c['precision'])} | {pct(c['recall'])} | {pct(c['f1'])} | "
                    f"{pct(c['full_recall'])} | {pct(t['recall'])} | {pct(t['full_recall'])} | {pct(m['exact_both'])} | "
                    f"{c['avg_pred']:.2f} | {pct(m['compression'])} |")
    return "\n".join(rows)


def difficulty_table(r):
    rows = ["| 方法 | simple F1 / 全召回 | moderate F1 / 全召回 | challenging F1 / 全召回 |", "|---|---:|---:|---:|"]
    for name, m in r["final"].items():
        cells = [f"{pct(m['by_difficulty'][d]['column']['f1'])} / {pct(m['by_difficulty'][d]['column']['full_recall'])}"
                 for d in ("simple", "moderate", "challenging")]
        rows.append(f"| {LABELS.get(name, name)} | " + " | ".join(cells) + " |")
    n = {d: r["final"]["exp2_query"]["by_difficulty"][d]["column"]["n"] for d in ("simple", "moderate", "challenging")}
    return "\n".join(rows) + f"\n\n题数：simple {n['simple']} / moderate {n['moderate']} / challenging {n['challenging']}。"


def contrast_table(r):
    rows = ["| 对比 | Δ列 F1 | Δ列 R | Δ列 P | Δ列全召回率 | Δ EM(列) |", "|---|---|---|---|---|---|"]
    for c in r["compare"]["contrasts"]:
        cell = lambda m: f"{100 * c[m]['diff']:+.1f} [{100 * c[m]['ci95'][0]:+.1f}, {100 * c[m]['ci95'][1]:+.1f}]"
        rows.append(f"| {CONTRASTS.get(c['label'], c['label'])} | {cell('f1')} | {cell('recall')} | {cell('precision')} | {cell('full_recall')} | {cell('exact')} |")
    return "\n".join(rows)


def attribution_table(r):
    keys_miss = ["table_not_selected", "score_below_threshold", "ambiguous_rejected", "column_not_scored"]
    keys_extra = ["score", "ambiguous_accepted", "forced_fk_only"]
    rows = ["| 方法 | 漏选：表未入选 | 漏选：分数低于阈值 | 漏选：模糊列被拒 | 多选：Score 入选 | 多选：模糊列接受 | 多选：仅强制外键 |",
            "|---|---:|---:|---:|---:|---:|---:|"]
    for name, a in r["attribution"].items():
        miss, extra = a["missed_gold_columns"], a["extra_columns"]
        rows.append(f"| {LABELS.get(name, name)} | {miss.get(keys_miss[0], 0)} | {miss.get(keys_miss[1], 0)} | {miss.get(keys_miss[2], 0)} | "
                    f"{extra.get(keys_extra[0], 0)} | {extra.get(keys_extra[1], 0)} | {extra.get(keys_extra[2], 0)} |")
    return "\n".join(rows)


def table_sweep(r, name="exp2_query"):
    rows = ["| 表 Noul 阈值 | top-k | 表 R | 表全召回率 | 平均保留表数 |", "|---:|---:|---:|---:|---:|"]
    for x in r["table_stage"][name]["sweep"]:
        if x["top_k"] in (3, 5):
            rows.append(f"| {x['threshold']} | {x['top_k']} | {pct(x['recall'])} | {pct(x['full_recall'])} | {x['avg_pred']:.2f} |")
    return "\n".join(rows)


def column_sweep(r, name):
    rows = ["| 列 Score 阈值 | 列 P | 列 R | 列 F1 | 列全召回率 | 平均列数 |", "|---:|---:|---:|---:|---:|---:|"]
    for x in r["column_stage"][name]["sweep"]:
        rows.append(f"| {x['threshold']} | {pct(x['precision'])} | {pct(x['recall'])} | {pct(x['f1'])} | {pct(x['full_recall'])} | {x['avg_pred']:.2f} |")
    return "\n".join(rows)


def score_bins(r):
    names = [n for n in ("exp1_mention", "exp2_query") if n in r["column_stage"]]
    rows = ["| Score 区间 | " + " | ".join(f"{LABELS[n]}：列数 / gold 比例" for n in names) + " |", "|---|" + "---:|" * len(names)]
    for i, b in enumerate(r["column_stage"][names[0]]["score_bins"]):
        cells = [f"{r['column_stage'][n]['score_bins'][i]['n']} / {pct(r['column_stage'][n]['score_bins'][i]['gold_rate'])}" for n in names]
        rows.append(f"| {b['range']} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def ambiguous_table(r):
    a = r["ambiguous"]
    rows = ["| 规则（同一批 JEV 分数） | 列 P | 列 R | 列 F1 | 列全召回率 | 平均列数 |", "|---|---:|---:|---:|---:|---:|"]
    for name, m in a["rules"].items():
        rows.append(f"| {RULES.get(name, name)} | {pct(m['precision'])} | {pct(m['recall'])} | {pct(m['f1'])} | {pct(m['full_recall'])} | {m['avg_pred']:.2f} |")
    head = (f"模糊带（1.0 ≤ Score < 2.5）共 {a['band_columns']} 列，出现在 {a['questions_with_band']} 题中，其中 gold 列占 {pct(a['band_gold_rate'])}%。"
            f"二次 Noul 接受 {a['accepted']['n']} 列（其中 gold {a['accepted']['gold']}），拒绝 {a['rejected']['n']} 列（其中 gold {a['rejected']['gold']}），"
            f"二次 Noul 在带内的 AUROC = {a['noul_auroc']:.3f}。")
    sweep = ["| 二次 Noul 阈值 | 列 P | 列 R | 列 F1 | 列全召回率 | 平均列数 |", "|---:|---:|---:|---:|---:|---:|"]
    for x in a["noul_threshold_sweep"]:
        sweep.append(f"| {x['noul_threshold']} | {pct(x['precision'])} | {pct(x['recall'])} | {pct(x['f1'])} | {pct(x['full_recall'])} | {x['avg_pred']:.2f} |")
    return head + "\n\n" + "\n".join(rows) + "\n\n二次 Noul 阈值离线扫描（同一批记录，0.5 为实验3 实际配置）：\n\n" + "\n".join(sweep)


def cost_table(r):
    rows = ["| 方法 | 平均输入 tokens/题 | 最大 tokens/题 | 平均调用数/题 | 平均耗时 s/题 | P90 耗时 s | JEV 估算总费用 (USD) |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name, c in r["cost"].items():
        usd = f"{c['est_jev_usd_total']:.3f}" if "est_jev_usd_total" in c else "-"
        rows.append(f"| {LABELS.get(name, name)} | {c['avg_input_tokens']:.0f} | {c['max_input_tokens']} | {c['avg_calls']:.2f} | {c['avg_seconds']:.2f} | {c['p90_seconds']:.2f} | {usd} |")
    return "\n".join(rows)


def stage_quality(r):
    rows = ["| 方法 | 表 Noul AUROC | 表 Noul ECE | 表 Noul Brier | 入选表的列召回上限 | 列 Score AUROC |", "|---|---:|---:|---:|---:|---:|"]
    for name, t in r["table_stage"].items():
        c = r["column_stage"][name]
        rows.append(f"| {LABELS.get(name, name)} | {t['auroc']:.3f} | {t['calibration']['ece']:.3f} | {t['calibration']['brier']:.3f} | "
                    f"{pct(t['column_recall_ceiling'])} | {c['auroc']:.3f} |")
    return "\n".join(rows)


def table_aware_table(r):
    rows = ["| Noul<0.5 的表，其列需要的 Score | 列 P | 列 R | 列 F1 | 列全召回率 | 平均列数 |", "|---:|---:|---:|---:|---:|---:|"]
    for x in r["table_aware_2b"]:
        label = "不收（=只用 Noul≥0.5 的表）" if x["low_table_score_threshold"] > 3 else f"≥ {x['low_table_score_threshold']}"
        rows.append(f"| {label} | {pct(x['precision'])} | {pct(x['recall'])} | {pct(x['f1'])} | {pct(x['full_recall'])} | {x['avg_pred']:.2f} |")
    return "\n".join(rows)


def named_table(r):
    rows = ["| 方法 | 列名出现在问题/evidence 中：列数 / 召回 | 未出现：列数 / 召回 |", "|---|---:|---:|"]
    for name, s in r["named_split"].items():
        rows.append(f"| {LABELS.get(name, name)} | {s['named']['n']} / {pct(s['named']['recall'])} | {s['not_named']['n']} / {pct(s['not_named']['recall'])} |")
    return "\n".join(rows)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("results.json")
    r = json.loads(path.read_text(encoding="utf-8"))
    n = r["noise"]
    sections = [
        f"共同评测题数：{r['n_questions']}",
        "### 主结果\n\n" + main_table(r),
        "### 按难度\n\n" + difficulty_table(r),
        "### 配对 bootstrap 对比（百分点，95% CI，1000 次重采样）\n\n" + contrast_table(r),
        "### 误差归因（列数，全部题目累计）\n\n" + attribution_table(r),
        "### 各阶段判别力与校准\n\n" + stage_quality(r),
        "### 表阶段离线扫描（实验2 记录的表 Noul）\n\n" + table_sweep(r),
        "### 列阶段离线扫描（硬阈值 + 强制外键）\n\n" + "\n\n".join(f"**{LABELS[k]}**\n\n" + column_sweep(r, k) for k in r["column_stage"]),
        "### 列 Score 区间内的 gold 比例\n\n" + score_bins(r),
        "### 实验3 模糊带\n\n" + ambiguous_table(r),
        (f"### JEV 重复调用的波动（实验2 与实验3 的步骤1/2 请求完全相同）\n\n表 Noul 平均绝对差 {n['table_noul_mean_abs_diff']:.4f}（最大 {n['table_noul_max_abs_diff']:.2f}），"
         f"入选表集合一致率 {pct(n['same_table_selection_rate'])}%；共同列 {n['column_pairs']} 个，列 Score 平均绝对差 {n['column_score_mean_abs_diff']:.3f}，"
         f"Pearson r = {n['column_score_pearson']:.4f}，在 2.0 阈值处翻转的比例 {pct(n['column_flip_rate_at_2.0'], 2)}%。"),
        "### 成本与时延\n\n" + cost_table(r),
        "### gold 列名是否直接出现在问题/evidence 中\n\n" + named_table(r),
        ("### 2b 记录上的表感知列阈值（离线；Noul≥0.5 的表仍用 Score≥2.0；强制外键列只保留在仍有入选列的表里）\n\n" + table_aware_table(r)) if r.get("table_aware_2b") else "",
        (f"### 外键元数据缺口\n\n多表 gold 查询 {r['fk_gaps']['multi_table_questions']} 题，其中 {r['fk_gaps']['not_fk_connected']} 题的 gold 表在 schema 外键图里不连通"
         f"（强制外键无法覆盖这些 join）。示例：" + "，".join(f"q{e['question_id']} {e['db_id']} {e['gold_tables']}" for e in r["fk_gaps"]["examples"][:5])),
    ]
    print("\n\n".join(sections))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
