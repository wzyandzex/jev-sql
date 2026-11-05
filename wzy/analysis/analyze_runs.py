"""Offline analysis of the jev-sql schema-linking runs (no new JEV calls).

Usage:
    ../.venv/Scripts/python.exe analyze_runs.py [--out results.json]

Reads <outputs>/runs/*/batch_answers.jsonl for the runs in sl_lib.RUNS and
evaluates every run on the same set of questions (those finished by all runs).
Gold tables/columns come from jev-sql's SQLite-authorizer extractor, so the
headline numbers match scripts/evaluate_schema_linking.py on that set.

Sections in the output JSON:
    final        P/R/F1, full-recall rate, exact match, compression, by difficulty
    attribution  why gold columns were missed / why extra columns were added
    table_stage  Noul AUROC, calibration, threshold x top-k sweep
    column_stage Score AUROC, positive rate per Score bin, threshold sweep
    ambiguous    exp3 extra-round decisions and counterfactual band rules
    noise        exp2 vs exp3 (identical step 1/2 requests): run-to-run variation
    compare      paired bootstrap 95% CIs for the main contrasts
    cost         JEV tokens, calls, latency, estimated USD
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from statistics import correlation, mean

from sl_lib import (
    QUESTIONS,
    RUNS,
    SCHEMA,
    ambiguous_nouls,
    auroc,
    bootstrap_diff,
    calibration,
    final_columns,
    fk_closure,
    forced_columns,
    load_gold,
    load_run,
    scored_columns,
    selected_tables,
    set_metrics,
    table_nouls,
)

JEV_USD_PER_INPUT_TOKEN = 0.000017976 / 428  # observed on the gateway for one smoke request; output tokens billed 0
METRICS = ("precision", "recall", "f1", "full_recall", "exact")

def tables_of(columns: set) -> set:
    return {table for table, _ in columns}


def column_pairs(records: dict, gold: dict, qids: list) -> list:
    return [(final_columns(records[q]), gold[q]["columns"]) for q in qids]


def threshold_pairs(records: dict, gold: dict, qids: list, threshold: float) -> list:
    """Counterfactual hard rule on the recorded Scores: Score >= threshold, plus forced FK columns."""
    pairs = []
    for q in qids:
        scored = scored_columns(records[q])
        pred = {k for k, (_, s) in scored.items() if s >= threshold} | forced_columns(records[q])
        pairs.append((pred, gold[q]["columns"]))
    return pairs


def final_metrics(records: dict, gold: dict, qids: list) -> dict:
    cols = column_pairs(records, gold, qids)
    tabs = [(tables_of(p), gold[q]["tables"]) for (p, _), q in zip(cols, qids)]
    by_difficulty = {}
    for level in ("simple", "moderate", "challenging"):
        idx = [i for i, q in enumerate(qids) if gold[q]["difficulty"] == level]
        by_difficulty[level] = {
            "column": set_metrics([cols[i] for i in idx]),
            "table": set_metrics([tabs[i] for i in idx]),
        }
    return {
        "column": set_metrics(cols),
        "table": set_metrics(tabs),
        "exact_both": sum(c[0] == c[1] and t[0] == t[1] for c, t in zip(cols, tabs)) / len(qids),
        "compression": mean(len(p) / gold[q]["db_columns"] for (p, _), q in zip(cols, qids)),
        "empty_predictions": sum(not p for p, _ in cols),
        "by_difficulty": by_difficulty,
    }


def attribution(records: dict, gold: dict, qids: list) -> dict:
    """Stage that lost each missed gold column, and source of each extra column."""
    misses, extras = Counter(), Counter()
    for q in qids:
        record = records[q]
        final = final_columns(record)
        scored = scored_columns(record)
        ambiguous = ambiguous_nouls(record)
        chosen = selected_tables(record)
        for col in gold[q]["columns"] - final:
            if col[0] not in chosen:
                misses["table_not_selected"] += 1
            elif col not in scored:
                misses["column_not_scored"] += 1
            elif col in ambiguous:
                misses["ambiguous_rejected"] += 1
            else:
                misses["score_below_threshold"] += 1
        sources = {
            (i["table"].casefold(), i["column"].casefold()): set(i.get("sources") or [])
            for i in record.get("linked_columns_union") or []
        }
        for col in final - gold[q]["columns"]:
            src = sources.get(col, set())
            if "score" in src:
                extras["score"] += 1
            elif "ambiguous" in src:
                extras["ambiguous_accepted"] += 1
            elif "forced_foreign_key" in src:
                extras["forced_fk_only"] += 1
            else:
                extras["other"] += 1
    return {"missed_gold_columns": dict(misses), "extra_columns": dict(extras)}

def table_stage(records: dict, gold: dict, qids: list) -> dict:
    """Table-level Noul as a classifier of gold tables, and a threshold x top-k sweep."""
    probs, labels = [], []
    for q in qids:
        for table, p in table_nouls(records[q]).items():
            probs.append(p)
            labels.append(int(table in gold[q]["tables"]))
    sweep = []
    for threshold in (0.0, 0.1, 0.2, 0.3, 0.5, 0.7):
        for k in (1, 2, 3, 5, 0):
            pairs = []
            for q in qids:
                ranked = sorted(table_nouls(records[q]).items(), key=lambda kv: -kv[1])
                if threshold > 0:
                    ranked = [kv for kv in ranked if kv[1] >= threshold]
                if k > 0:
                    ranked = ranked[:k]
                pairs.append(({t for t, _ in ranked}, gold[q]["tables"]))
            m = set_metrics(pairs)
            sweep.append({"threshold": threshold, "top_k": k, **{x: m[x] for x in ("recall", "precision", "full_recall", "avg_pred")}})
    selected = [(selected_tables(records[q]), gold[q]["tables"]) for q in qids]
    reachable = sum(len({c for c in gold[q]["columns"] if c[0] in selected_tables(records[q])}) for q in qids)
    return {
        "auroc": auroc(probs, labels),
        "calibration": calibration(probs, labels),
        "selected": set_metrics(selected),
        "column_recall_ceiling": reachable / sum(len(gold[q]["columns"]) for q in qids),
        "sweep": sweep,
    }


def column_stage(records: dict, gold: dict, qids: list) -> dict:
    """Best Score per scored column as a classifier of gold columns; end-to-end hard-threshold sweep."""
    scores, labels = [], []
    for q in qids:
        for col, (_, s) in scored_columns(records[q]).items():
            scores.append(s)
            labels.append(int(col in gold[q]["columns"]))
    bins = []
    for lo, hi in ((0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.01)):
        idx = [i for i, s in enumerate(scores) if lo <= s < hi]
        bins.append({"range": f"[{lo},{min(hi, 3.0)})", "n": len(idx), "gold_rate": sum(labels[i] for i in idx) / len(idx) if idx else None})
    sweep = []
    for threshold in (0.5, 1.0, 1.5, 2.0, 2.25, 2.5, 2.75):
        m = set_metrics(threshold_pairs(records, gold, qids, threshold))
        sweep.append({"threshold": threshold, **{x: m[x] for x in ("precision", "recall", "f1", "full_recall", "avg_pred")}})
    return {"auroc": auroc(scores, labels), "n_scored": len(scores), "gold_rate": sum(labels) / len(labels), "score_bins": bins, "sweep": sweep}


def ambiguous_stage(records: dict, gold: dict, qids: list) -> dict:
    """exp3 only: quality of the extra Noul round and band rules on the same recorded Scores."""
    nouls, labels, per_question = [], [], []
    for q in qids:
        band = ambiguous_nouls(records[q])
        per_question.append(len(band))
        for col, p in band.items():
            nouls.append(p)
            labels.append(int(col in gold[q]["columns"]))
    accepted = [y for p, y in zip(nouls, labels) if p >= 0.5]
    rejected = [y for p, y in zip(nouls, labels) if p < 0.5]
    actual = column_pairs(records, gold, qids)
    reject_band = threshold_pairs(records, gold, qids, 2.5)
    accept_band = threshold_pairs(records, gold, qids, 1.0)
    noul_sweep = []
    for t in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
        pairs = []
        for (base, g), q in zip(reject_band, qids):
            pairs.append((base | {c for c, p in ambiguous_nouls(records[q]).items() if p >= t}, g))
        m = set_metrics(pairs)
        noul_sweep.append({"noul_threshold": t, **{x: m[x] for x in ("precision", "recall", "f1", "full_recall", "avg_pred")}})
    return {
        "band_columns": len(nouls),
        "avg_band_per_question": mean(per_question),
        "questions_with_band": sum(1 for n in per_question if n),
        "band_gold_rate": sum(labels) / len(labels) if labels else None,
        "accepted": {"n": len(accepted), "gold": sum(accepted)},
        "rejected": {"n": len(rejected), "gold": sum(rejected)},
        "noul_auroc": auroc(nouls, labels) if labels else None,
        "noul_threshold_sweep": noul_sweep,
        "rules": {
            "actual_extra_round": set_metrics(actual),
            "reject_whole_band(score>=2.5)": set_metrics(reject_band),
            "hard_2.0": set_metrics(threshold_pairs(records, gold, qids, 2.0)),
            "accept_whole_band(score>=1.0)": set_metrics(accept_band),
        },
        "pairs": {"actual": actual, "reject_band": reject_band, "accept_band": accept_band},
    }

def noise(run_a: dict, run_b: dict, qids: list) -> dict:
    """exp2 and exp3 send identical step-1/2 requests, so their differences there are JEV run-to-run variation."""
    table_diffs, same_selection = [], 0
    xs, ys, flips = [], [], 0
    for q in qids:
        na, nb = table_nouls(run_a[q]), table_nouls(run_b[q])
        table_diffs += [abs(na[t] - nb[t]) for t in na if t in nb]
        same_selection += selected_tables(run_a[q]) == selected_tables(run_b[q])
        sa, sb = scored_columns(run_a[q]), scored_columns(run_b[q])
        for col in sa.keys() & sb.keys():
            xs.append(sa[col][1])
            ys.append(sb[col][1])
            flips += (sa[col][1] >= 2.0) != (sb[col][1] >= 2.0)
    return {
        "table_noul_mean_abs_diff": mean(table_diffs),
        "table_noul_max_abs_diff": max(table_diffs),
        "same_table_selection_rate": same_selection / len(qids),
        "column_pairs": len(xs),
        "column_score_mean_abs_diff": mean(abs(x - y) for x, y in zip(xs, ys)),
        "column_score_pearson": correlation(xs, ys),
        "column_flip_rate_at_2.0": flips / len(xs),
    }


def cost(records: dict, qids: list, jev: bool) -> dict:
    tokens, calls, seconds, mention_seconds = [], [], [], []
    for q in qids:
        r = records[q]
        usage = r.get("usage") or {}
        elapsed = r.get("elapsed") or {}
        if jev:
            tokens.append(sum((usage.get(s) or {}).get("input_tokens", 0) for s in ("table", "column", "ambiguous")))
            n = 1 + sum(t["num_batches"] for t in ((r.get("stage_column") or {}).get("tables") or {}).values())
            calls.append(n + (1 if (r.get("stage_ambiguous") or {}).get("num_ambiguous") else 0))
            mention_seconds.append(elapsed.get("mentions_seconds", 0.0))
        else:
            tokens.append(usage.get("input_tokens") or 0)
            calls.append(1)
        seconds.append(elapsed.get("total_seconds", 0.0))
    ordered = sorted(seconds)
    out = {
        "avg_input_tokens": mean(tokens),
        "max_input_tokens": max(tokens),
        "avg_calls": mean(calls),
        "avg_seconds": mean(seconds),
        "p90_seconds": ordered[int(0.9 * (len(ordered) - 1))],
    }
    if jev:
        out["est_jev_usd_total"] = sum(tokens) * JEV_USD_PER_INPUT_TOKEN
        out["avg_mention_seconds"] = mean(mention_seconds)
    return out


def fk_gaps(gold: dict, qids: list) -> dict:
    """Multi-table gold queries whose gold tables are not connected by the schema's FK edges."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    edges = {}
    for db in schema["databases"]:
        edges[db["db_id"]] = {
            frozenset((table["name"].casefold(), adj["table"].casefold()))
            for table in db["tables"]
            for adj in table.get("adjacent_tables") or []
        }
    multi, examples = 0, []
    for q in qids:
        tables = gold[q]["tables"]
        if len(tables) < 2:
            continue
        multi += 1
        seen, stack = set(), [min(tables)]
        while stack:
            t = stack.pop()
            if t not in seen:
                seen.add(t)
                stack += [u for u in tables if frozenset((t, u)) in edges[gold[q]["db_id"]]]
        if seen != tables:
            examples.append({"question_id": q, "db_id": gold[q]["db_id"], "gold_tables": sorted(tables)})
    return {"multi_table_questions": multi, "not_fk_connected": len(examples), "examples": examples[:10]}


def combine(run_a: dict, run_b: dict, qids: list, op) -> dict:
    """Per-question set union/intersection of two runs' final columns."""
    out = {}
    for q in qids:
        cols = op(final_columns(run_a[q]), final_columns(run_b[q]))
        out[q] = {"linked_columns_union": [{"table": t, "column": c} for t, c in sorted(cols)]}
    return out


def named_split(runs: dict, gold: dict, qids: list) -> dict:
    """Recall on gold columns whose name appears verbatim in question/evidence vs. the rest."""
    questions = {str(q["question_id"]): q for q in json.loads(QUESTIONS.read_text(encoding="utf-8"))}
    names = {}
    for db in json.loads(SCHEMA.read_text(encoding="utf-8"))["databases"]:
        for t in db["tables"]:
            for c in t["columns"]:
                names[(db["db_id"], t["name"].casefold(), c["name"].casefold())] = c["name"].casefold()

    def named(q, col):
        name = names.get((gold[q]["db_id"],) + col, col[1])
        text = (questions[q]["question"] + " " + (questions[q].get("evidence") or "")).casefold()
        return len(name) > 2 and re.search(r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])", text) is not None

    flags = {q: {col: named(q, col) for col in gold[q]["columns"]} for q in qids}
    out = {}
    for run, records in runs.items():
        hit = {True: [0, 0], False: [0, 0]}
        for q in qids:
            pred = final_columns(records[q])
            for col, is_named in flags[q].items():
                hit[is_named][0] += 1
                hit[is_named][1] += col in pred
        out[run] = {
            "named": {"n": hit[True][0], "recall": hit[True][1] / hit[True][0]},
            "not_named": {"n": hit[False][0], "recall": hit[False][1] / hit[False][0]},
        }
    return out


def table_aware(records: dict, gold: dict, qids: list) -> list:
    """2b only: columns of tables with Noul < 0.5 need Score >= s_hi; FK-forced columns kept only in active tables."""
    out = []
    for s_hi in (2.0, 2.25, 2.5, 2.75, 3.01):
        pairs = []
        for q in qids:
            r = records[q]
            nouls = table_nouls(r)
            kept = {
                col for col, (_, s) in scored_columns(r).items()
                if s >= (2.0 if nouls.get(col[0], 0.0) >= 0.5 else s_hi)
            }
            active = {t for t, _ in kept} | {t for t, p in nouls.items() if p >= 0.5}
            kept |= {col for col in forced_columns(r) if col[0] in active}
            pairs.append((kept, gold[q]["columns"]))
        m = set_metrics(pairs)
        out.append({"low_table_score_threshold": s_hi, **{x: m[x] for x in ("precision", "recall", "f1", "full_recall", "avg_pred")}})
    return out


def wins(run_a: dict, run_b: dict, gold: dict, qids: list) -> dict:
    """Per-question column F1: how often A beats, ties or loses to B, by difficulty."""
    out = {}
    for level in ("all", "simple", "moderate", "challenging"):
        tally = Counter()
        for q in qids:
            if level != "all" and gold[q]["difficulty"] != level:
                continue
            fa = set_metrics([(final_columns(run_a[q]), gold[q]["columns"])])["f1"]
            fb = set_metrics([(final_columns(run_b[q]), gold[q]["columns"])])["f1"]
            tally["a_better" if fa > fb + 1e-9 else "b_better" if fb > fa + 1e-9 else "tie"] += 1
        out[level] = dict(tally)
    return out

def compare(runs: dict, gold: dict, qids: list, ambiguous: dict | None) -> dict:
    pairs = {name: column_pairs(records, gold, qids) for name, records in runs.items()}
    if "exp2_query" in runs:
        pairs["exp2_hard2.0_recomputed"] = threshold_pairs(runs["exp2_query"], gold, qids, 2.0)
    if ambiguous:
        pairs["exp3_hard2.0_counterfactual"] = threshold_pairs(runs["exp3_ambiguous"], gold, qids, 2.0)
        pairs["exp3_reject_band"] = ambiguous["pairs"]["reject_band"]
    contrasts = [
        ("exp2_query", "exp1_mention", "drop mention extraction (exp2 - exp1)"),
        ("exp3_ambiguous", "exp2_query", "ambiguous band vs hard 2.0, different JEV draws (exp3 - exp2)"),
        ("exp3_ambiguous", "exp3_hard2.0_counterfactual", "ambiguous band vs hard 2.0, same JEV draws"),
        ("exp3_ambiguous", "exp3_reject_band", "accepted band columns only, same JEV draws"),
        ("exp3_hard2.0_counterfactual", "exp2_query", "noise floor: same rule, two JEV runs"),
        ("llm_direct", "exp2_query", "direct LLM linking vs exp2"),
        ("llm_direct+fk", "exp2_query", "direct LLM linking + FK closure vs exp2"),
        ("llm_direct+fk", "exp1_mention", "direct LLM linking + FK closure vs exp1"),
        ("exp2|llm+fk(union)", "exp2_query", "union of exp2 and LLM+FK vs exp2"),
        ("exp2b_tab0.1", "exp2_query", "table Noul threshold 0.1 vs 0.5 (exp2b - exp2)"),
    ]
    out = []
    for a, b, label in contrasts:
        if a in pairs and b in pairs:
            out.append({"a": a, "b": b, "label": label, **bootstrap_diff(pairs[a], pairs[b], METRICS)})
    sanity = {}
    for name in ("exp1_mention", "exp2_query"):
        if name in runs:
            recomputed = threshold_pairs(runs[name], gold, qids, 2.0)
            sanity[name] = sum(p == q for (p, _), (q, _) in zip(recomputed, pairs[name])) / len(qids)
    return {"contrasts": out, "sanity_hard2.0_reproduces_final": sanity}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    parser.add_argument("--runs", nargs="*", default=list(RUNS), help="run names from sl_lib.RUNS")
    args = parser.parse_args()

    gold = load_gold()
    runs, coverage = {}, {}
    for name in args.runs:
        records, failed = load_run(RUNS[name])
        coverage[name] = {"ok": len(records), "failed_last_record": failed}
        if records:
            runs[name] = records
    if "llm_direct" in runs:
        runs["llm_direct+fk"] = fk_closure(runs["llm_direct"])
    qids = sorted(set(gold).intersection(*(set(r) for r in runs.values())), key=int)
    print(f"common questions: {len(qids)} (gold extractable: {len(gold)}) coverage: {coverage}")

    jev_runs = [n for n in runs if not n.startswith("llm")]
    combos = {}
    if {"exp2_query", "llm_direct+fk"} <= set(runs):
        combos["exp2|llm+fk(union)"] = combine(runs["exp2_query"], runs["llm_direct+fk"], qids, set.union)
        combos["exp2&llm+fk(intersection)"] = combine(runs["exp2_query"], runs["llm_direct+fk"], qids, set.intersection)
    ambiguous = ambiguous_stage(runs["exp3_ambiguous"], gold, qids) if "exp3_ambiguous" in runs else None
    result = {
        "n_questions": len(qids),
        "coverage": coverage,
        "final": {n: final_metrics(runs[n], gold, qids) for n in runs},
        "combos": {n: final_metrics(r, gold, qids) for n, r in combos.items()},
        "attribution": {n: attribution(runs[n], gold, qids) for n in jev_runs},
        "table_stage": {n: table_stage(runs[n], gold, qids) for n in jev_runs},
        "column_stage": {n: column_stage(runs[n], gold, qids) for n in jev_runs},
        "ambiguous": {k: v for k, v in ambiguous.items() if k != "pairs"} if ambiguous else None,
        "noise": noise(runs["exp2_query"], runs["exp3_ambiguous"], qids) if {"exp2_query", "exp3_ambiguous"} <= set(runs) else None,
        "compare": compare({**runs, **combos}, gold, qids, ambiguous),
        "wins_exp2_vs_exp1": wins(runs["exp2_query"], runs["exp1_mention"], gold, qids) if {"exp1_mention", "exp2_query"} <= set(runs) else None,
        "cost": {n: cost(runs[n], qids, not n.startswith("llm")) for n in runs},
        "fk_gaps": fk_gaps(gold, qids),
        "named_split": named_split(runs, gold, qids),
        "table_aware_2b": table_aware(runs["exp2b_tab0.1"], gold, qids) if "exp2b_tab0.1" in runs else None,
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{'run':<16}{'colP':>7}{'colR':>7}{'colF1':>7}{'fullR':>7}{'tabR':>7}{'tabFull':>8}{'EM':>6}{'avgCols':>8}")
    for n, m in result["final"].items():
        c, t = m["column"], m["table"]
        print(f"{n:<16}{c['precision']:>7.3f}{c['recall']:>7.3f}{c['f1']:>7.3f}{c['full_recall']:>7.3f}"
              f"{t['recall']:>7.3f}{t['full_recall']:>8.3f}{m['exact_both']:>6.3f}{c['avg_pred']:>8.2f}")
    print("sanity:", result["compare"]["sanity_hard2.0_reproduces_final"])
    print(f"saved -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
