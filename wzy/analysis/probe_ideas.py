"""Offline probes for new research directions (no API calls; reads recorded runs and sql_ex results).

Usage:
    python probe_ideas.py [--out probe_ideas.json]

Probes:
    P1 routing   per-question signals from JEV records that predict whether the linked schema
                 misses a gold column, and the EX of "linked schema if confident, else full schema"
    P2 budget    columns kept vs. full-recall for a per-question "minimal set" rule driven by the
                 recorded Scores, compared with global thresholds (the conformal-style baseline)
    P3 disagree  where JEV (exp2) and the qwen one-call linker disagree, which one was right
    P4 oracle    upper bounds: per-question best of {full, linked} schema EX
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from sl_lib import (
    RUNS,
    auroc,
    fk_closure,
    final_columns,
    forced_columns,
    load_gold,
    load_run,
    scored_columns,
    set_metrics,
    table_nouls,
)
from sql_ex import OUT_DIR, load_records

HERE = Path(__file__).resolve().parent

def risk_signals(record: dict) -> dict:
    """Per-question features computable at inference time from a JEV record (no gold needed)."""
    scored = scored_columns(record)
    scores = sorted((s for _, s in scored.values()), reverse=True)
    nouls = sorted(table_nouls(record).values(), reverse=True)
    kept = final_columns(record)
    band = [s for s in scores if 1.0 <= s < 2.0]  # just below the 2.0 cut: likely misses
    near = [p for p in nouls if 0.1 <= p < 0.5]    # tables just below the 0.5 cut
    # binary entropy of table Noul, summed: total uncertainty of the table decision
    ent = sum(-(p * math.log(p) + (1 - p) * math.log(1 - p)) for p in nouls if 0 < p < 1)
    return {
        "n_band_cols": len(band),
        "max_band_score": max(band) if band else 0.0,
        "n_near_tables": len(near),
        "max_near_table": max(near) if near else 0.0,
        "table_entropy": ent,
        "n_kept": len(kept),
        "risk": (max(band) / 2.0 if band else 0.0) + (max(near) / 0.5 if near else 0.0),
    }


def probe_routing(records: dict, gold: dict, ex: dict, qids: list) -> dict:
    """Do the signals predict a miss, and does routing uncertain questions to the full schema help EX?"""
    rows = []
    for q in qids:
        sig = risk_signals(records[q])
        miss = int(not gold[q]["columns"] <= final_columns(records[q]))
        rows.append({"q": q, "miss": miss, **sig})
    out = {"n": len(rows), "miss_rate": sum(r["miss"] for r in rows) / len(rows), "auroc": {}}
    for name in ("risk", "n_band_cols", "max_band_score", "n_near_tables", "max_near_table", "table_entropy", "n_kept"):
        out["auroc"][name] = auroc([r[name] for r in rows], [r["miss"] for r in rows])

    linked, full = ex["linked"], ex["full"]
    order = sorted(rows, key=lambda r: -r["risk"])  # most at-risk first
    curve = []
    for frac in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0):
        k = round(frac * len(order))
        to_full = {r["q"] for r in order[:k]}
        correct = sum(full[r["q"]] if r["q"] in to_full else linked[r["q"]] for r in rows)
        curve.append({"share_routed_to_full": frac, "ex": correct / len(rows)})
    out["routing_curve"] = curve
    out["ex_linked"] = sum(linked[r["q"]] for r in rows) / len(rows)
    out["ex_full"] = sum(full[r["q"]] for r in rows) / len(rows)
    out["oracle_best_of_two"] = sum(max(linked[r["q"]], full[r["q"]]) for r in rows) / len(rows)
    return out

def kept_at(record: dict, threshold: float) -> set:
    return {k for k, (_, s) in scored_columns(record).items() if s >= threshold} | forced_columns(record)


def probe_budget(records: dict, gold: dict, qids: list) -> dict:
    """Global threshold vs. per-question rules at matched average size: which keeps full recall cheaper?

    Rules compared (all on the same recorded Scores; forced FK columns always kept):
      global t      Score >= t for every question
      relative r    Score >= r * (max Score in the question); adapts to how confident the question is
      gap g         keep columns in descending Score until the next drop exceeds g (largest-gap cut)
    """
    def evaluate(rule) -> dict:
        pairs = [(rule(records[q]), gold[q]["columns"]) for q in qids]
        m = set_metrics(pairs)
        return {"avg_cols": m["avg_pred"], "full_recall": m["full_recall"], "recall": m["recall"], "precision": m["precision"]}

    def relative(r):
        def rule(rec):
            sc = scored_columns(rec)
            top = max((s for _, s in sc.values()), default=0.0)
            return {k for k, (_, s) in sc.items() if s >= r * top} | forced_columns(rec)
        return rule

    def gap(g, floor=0.3):
        def rule(rec):
            ranked = sorted(((s, k) for k, (_, s) in scored_columns(rec).items()), reverse=True)
            keep = set()
            for i, (s, k) in enumerate(ranked):
                if s < floor:
                    break
                keep.add(k)
                if i + 1 < len(ranked) and s - ranked[i + 1][0] > g:
                    break
            return keep | forced_columns(rec)
        return rule

    out = {"global": [], "relative": [], "gap": []}
    for t in (2.5, 2.0, 1.5, 1.0, 0.75, 0.5, 0.3, 0.2):
        out["global"].append({"param": t, **evaluate(lambda rec, t=t: kept_at(rec, t))})
    for r in (0.9, 0.7, 0.5, 0.35, 0.25, 0.15, 0.1):
        out["relative"].append({"param": r, **evaluate(relative(r))})
    for g in (1.5, 1.0, 0.7, 0.5, 0.3):
        out["gap"].append({"param": g, **evaluate(gap(g))})
    return out

def probe_disagreement(jev: dict, llm: dict, gold: dict, qids: list) -> dict:
    """Column-level disagreement between JEV (exp2) and qwen+FK: who is right, and what kind of column."""
    stats = {"jev_only": [0, 0], "llm_only": [0, 0], "both": [0, 0]}  # [count, of which gold]
    kinds = {"jev_only_gold_is_key": 0, "llm_only_gold_is_key": 0}
    for q in qids:
        a, b, g = final_columns(jev[q]), final_columns(llm[q]), gold[q]["columns"]
        for name, cols in (("jev_only", a - b), ("llm_only", b - a), ("both", a & b)):
            stats[name][0] += len(cols)
            stats[name][1] += len(cols & g)
        for name, cols in (("jev_only_gold_is_key", (a - b) & g), ("llm_only_gold_is_key", (b - a) & g)):
            kinds[name] += sum(1 for _, c in cols if c.endswith("id") or c in ("cds", "cdscode", "uuid", "code"))
    out = {k: {"cols": v[0], "gold": v[1], "gold_rate": v[1] / v[0] if v[0] else None} for k, v in stats.items()}
    out.update(kinds)
    return out


def probe_ex_by_size(ex_rows: dict, gold: dict, qids: list) -> dict:
    """EX of full vs gold schema split by database width: where does pruning help most?"""
    buckets = {}
    for q in qids:
        width = gold[q]["db_columns"]
        b = "<=30 cols" if width <= 30 else "31-90 cols" if width <= 90 else ">90 cols"
        d = buckets.setdefault(b, {"n": 0, "full": 0, "gold": 0, "linked": 0, "dbs": set()})
        d["n"] += 1
        d["dbs"].add(gold[q]["db_id"])
        for v in ("full", "gold", "linked"):
            d[v] += ex_rows[v][q]
    return {b: {"n": d["n"], "dbs": sorted(d["dbs"]), **{v: d[v] / d["n"] for v in ("full", "gold", "linked")}} for b, d in sorted(buckets.items())}


def probe_confusable(records: dict, gold: dict, ex_raw: dict, qids: list, rng_seed: int = 0) -> dict:
    """Where does pruning to the gold schema help, and what goes wrong with the full schema?

    groups      by JEV's recorded Scores: 'unscored' = some gold column was never scored (table missed);
                'confusable' = all gold columns scored and some non-gold column scores above the weakest
                gold column; 'clean' = the rest. Uses gold, so it is a diagnostic split, not a method.
    taxonomy    on questions the gold schema fixes (full wrong, gold right): columns the full-schema SQL
                used that are not gold, by kind
    homonyms    same-name distractor vs. the gold column it shadows: JEV Score comparison
    prevalence  questions whose gold uses a non-key column whose name exists in another table
    """
    import random

    from jev.evaluation import extract_sql_schema
    from sl_lib import DB_DIR, SCHEMA

    rng = random.Random(rng_seed)

    def ci(xs, rounds=2000):
        means = sorted(sum(xs[rng.randrange(len(xs))] for _ in xs) / len(xs) for _ in range(rounds))
        return [means[int(0.025 * rounds)], means[int(0.975 * rounds) - 1]]

    full, gold_ex = ex_raw["full"], ex_raw["gold"]
    groups = {"unscored": [], "confusable": [], "clean": []}
    for q in qids:
        sc = {c: s for c, (_, s) in scored_columns(records[q]).items()}
        if not gold[q]["columns"] <= sc.keys():
            groups["unscored"].append(q)
            continue
        weakest = min(sc[c] for c in gold[q]["columns"])
        hit = any(s > weakest for c, s in sc.items() if c not in gold[q]["columns"])
        groups["confusable" if hit else "clean"].append(q)
    gain = {g: [int(gold_ex[q]["correct"]) - int(full[q]["correct"]) for q in qs] for g, qs in groups.items()}
    out = {"groups": {
        g: {"n": len(qs), "ex_full": sum(full[q]["correct"] for q in qs) / len(qs),
            "ex_gold": sum(gold_ex[q]["correct"] for q in qs) / len(qs),
            "gain": sum(gain[g]) / len(qs), "gain_ci95": ci(gain[g])}
        for g, qs in groups.items() if qs}}
    diffs = []
    for _ in range(2000):
        a = [gain["confusable"][rng.randrange(len(gain["confusable"]))] for _ in gain["confusable"]]
        b = [gain["clean"][rng.randrange(len(gain["clean"]))] for _ in gain["clean"]]
        diffs.append(sum(a) / len(a) - sum(b) / len(b))
    diffs.sort()
    out["confusable_minus_clean_gain_ci95"] = [diffs[50], diffs[1949]]

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    name_tables, keys = {}, {}
    for db in schema["databases"]:
        nt, ks = {}, set()
        for t in db["tables"]:
            tname = t["name"].casefold()
            ks |= {(tname, p.casefold()) for p in t["primary_key"]}
            for c in t["columns"]:
                nt.setdefault(c["name"].casefold(), set()).add(tname)
            for adj in t.get("adjacent_tables") or []:
                for child, parent in adj.get("foreign_keys") or []:
                    ks |= {tuple(x.casefold() for x in child.rsplit(".", 1)), tuple(x.casefold() for x in parent.rsplit(".", 1))}
        name_tables[db["db_id"]], keys[db["db_id"]] = nt, ks

    kinds, fixed, failure = {}, 0, {}
    homonym_pairs = []
    kept = keep_sets_s05(records)
    for q in qids:
        if not (gold_ex[q]["correct"] and not full[q]["correct"]):
            continue
        fixed += 1
        if full[q]["exec_error"]:
            failure["exec_error"] = failure.get("exec_error", 0) + 1
            continue
        db = gold[q]["db_id"]
        _, used, err = extract_sql_schema(full[q]["sql"], DB_DIR / db / f"{db}.sqlite")
        if err:
            failure["unparsable"] = failure.get("unparsable", 0) + 1
            continue
        used = set(used)
        extra, omitted = used - gold[q]["columns"], gold[q]["columns"] - used
        label = "used_non_gold_column" if extra else "omitted_gold_column" if omitted else "same_columns_wrong_logic"
        failure[label] = failure.get(label, 0) + 1
        sc = {c: s for c, (_, s) in scored_columns(records[q]).items()}
        gold_by_name = {}
        for t, c in gold[q]["columns"]:
            gold_by_name.setdefault(c, []).append((t, c))
        for t, c in extra:
            if c in gold_by_name:
                kind = "homonym_of_gold_column"
                for g in gold_by_name[c]:
                    if (t, c) in sc and g in sc:
                        homonym_pairs.append({"q": q, "distractor": f"{t}.{c}", "gold": f"{g[0]}.{g[1]}",
                                              "s_distractor": sc[(t, c)], "s_gold": sc[g], "kept_at_0.5": (t, c) in kept[q]})
            elif len(name_tables[db].get(c, ())) > 1:
                kind = "name_shared_across_tables"
            elif t in {gt for gt, _ in gold[q]["columns"]}:
                kind = "other_column_of_gold_table"
            else:
                kind = "column_of_non_gold_table"
            kinds[kind] = kinds.get(kind, 0) + 1
    out["gold_fixes"] = fixed
    out["gold_breaks"] = sum(1 for q in qids if full[q]["correct"] and not gold_ex[q]["correct"])
    out["full_schema_failure_on_fixed"] = failure
    out["distractor_kinds"] = kinds
    if homonym_pairs:
        d = sorted(p["s_distractor"] - p["s_gold"] for p in homonym_pairs)
        out["homonym_scores"] = {
            "pairs": len(homonym_pairs),
            "share_distractor_scored_at_least_gold": sum(x >= 0 for x in d) / len(d),
            "share_within_0.3": sum(abs(x) < 0.3 for x in d) / len(d),
            "median_distractor_minus_gold": d[len(d) // 2],
            "share_distractor_kept_at_0.5": sum(p["kept_at_0.5"] for p in homonym_pairs) / len(homonym_pairs),
            "examples": homonym_pairs[:8],
        }
    prevalent = 0
    for q in qids:
        db = gold[q]["db_id"]
        prevalent += any(len(name_tables[db].get(c, ())) > 1 and (t, c) not in keys[db] for t, c in gold[q]["columns"])
    out["prevalence_nonkey_shared_name"] = {"questions": prevalent, "share": prevalent / len(qids)}
    return out


def keep_sets_s05(records: dict) -> dict:
    return {q: kept_at(r, 0.5) for q, r in records.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(HERE / "probe_ideas.json"))
    parser.add_argument("--linked", default="exp2b_tab0.1~s0.5", help="sql_ex variant used as the linked schema")
    args = parser.parse_args()

    gold = load_gold()
    exp2 = load_run(RUNS["exp2_query"])[0]
    exp2b = load_run(RUNS["exp2b_tab0.1"])[0]
    llm_fk = fk_closure(load_run(RUNS["llm_direct"])[0])
    ex_raw = {v: load_records(OUT_DIR / f"{v}.jsonl") for v in ("full", "gold", args.linked, "exp2_query")}
    qids = sorted(
        (q for q in gold if q in exp2 and q in exp2b and q in llm_fk
         and all(q in ex_raw[v] and ex_raw[v][q]["gold_error"] is None for v in ex_raw)),
        key=int,
    )
    ex = {
        "full": {q: int(ex_raw["full"][q]["correct"]) for q in qids},
        "gold": {q: int(ex_raw["gold"][q]["correct"]) for q in qids},
        "linked": {q: int(ex_raw[args.linked][q]["correct"]) for q in qids},
    }
    ex_exp2 = {"full": ex["full"], "linked": {q: int(ex_raw["exp2_query"][q]["correct"]) for q in qids}}

    result = {
        "n": len(qids),
        "routing_exp2_default": probe_routing(exp2, gold, ex_exp2, qids),
        "routing_exp2b_s0.5_signals_from_exp2b": probe_routing(exp2b, gold, ex, qids),
        "budget_exp2b": probe_budget(exp2b, gold, qids),
        "disagreement_exp2_vs_llmfk": probe_disagreement(exp2, llm_fk, gold, qids),
        "ex_by_db_width": probe_ex_by_size(ex, gold, qids),
        "confusable": probe_confusable(exp2b, gold, ex_raw, qids),
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    c = result["confusable"]
    print("groups:", json.dumps(c["groups"], ensure_ascii=False))
    print("confusable - clean gain CI:", c["confusable_minus_clean_gain_ci95"])
    print("gold fixes / breaks:", c["gold_fixes"], c["gold_breaks"], "| full failure on fixed:", c["full_schema_failure_on_fixed"])
    print("distractor kinds:", c["distractor_kinds"])
    print("homonym scores:", {k: v for k, v in c.get("homonym_scores", {}).items() if k != "examples"})
    for e in c.get("homonym_scores", {}).get("examples", []):
        print("   ", e)
    print("prevalence:", c["prevalence_nonkey_shared_name"])
    for name in ("routing_exp2_default", "routing_exp2b_s0.5_signals_from_exp2b"):
        r = result[name]
        print(name, "miss_rate", round(r["miss_rate"], 3), "best AUROC", round(max(v for v in r["auroc"].values() if v is not None), 3),
              "| EX linked", round(r["ex_linked"], 3), "full", round(r["ex_full"], 3), "oracle", round(r["oracle_best_of_two"], 3))
    b = result["budget_exp2b"]
    for name in ("global", "relative", "gap"):
        print(name, [(x["param"], round(x["avg_cols"], 2), round(x["full_recall"], 3)) for x in b[name]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
