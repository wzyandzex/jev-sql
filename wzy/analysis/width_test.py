"""Schema-width stress test: keep the question and gold SQL, widen the schema with other databases.

Usage:
    python width_test.py --variants width30 width60 width60oracle
    python width_test.py --variants width30 --limit 5 --summary <tmp.json>   (smoke test)
    python width_test.py --summary-only --variants ...                       (recompute summary)

W1/W2 append whole tables from other BIRD mini-dev databases (no table-name collisions across the
11 databases) until the schema has at least 30 / 60 tables. The gold SQL still runs on the original
database, so any SQL that uses a distractor table fails to execute. width60oracle additionally applies
oracle same-name disambiguation over the widened column set.

EX follows BIRD (rows as sets, 30 s / 200k rows). At most MAX_API_CONCURRENCY (5) requests in flight.

Outputs:
    outputs/runs/sql_ex/<variant>.jsonl
    <this dir>/width_tables.md and the --summary json
"""

from __future__ import annotations

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sl_lib import QUESTIONS, load_gold, set_metrics
from sql_ex import (
    MAX_API_CONCURRENCY,
    OUT_DIR,
    PROMPT,
    Generator,
    extract_sql,
    full_columns,
    gold_results,
    load_records,
    oracle_disambiguate,
    render,
    run_sql,
    schema_index,
)

from jev.config import load_environment

HERE = Path(__file__).resolve().parent
WIDTHS = {30: "width30", 60: "width60"}


def widened_db(index: dict, db_id: str, target: int) -> dict:
    """Target database plus whole tables of other databases until >= target tables."""
    base = index[db_id]
    tables = list(base["tables"])
    for other in sorted(index):
        if len(tables) >= target:
            break
        if other != db_id:
            tables += index[other]["tables"]
    return {"tables": tables, "fks": base["fks"]}


def widened_columns(db: dict) -> set:
    return {(table.casefold(), name.casefold()) for table, columns in db["tables"] for name, _, _ in columns}


def keep_sets(variant: str, index: dict, gold: dict) -> dict:
    base = variant.split("@", 1)[0]
    target = 30 if base.startswith("width30") else 60
    keep = {}
    for q, g in gold.items():
        db = widened_db(index, g["db_id"], target)
        cols = widened_columns(db)
        keep[q] = oracle_disambiguate(cols, g["columns"]) if variant.endswith("oracle") else cols
    return keep


def run_variants(variants, qids, keeps, questions, golds, index, generate, workers) -> None:
    for variant in variants:
        path = OUT_DIR / f"{variant}.jsonl"
        done = load_records(path)
        todo = [q for q in qids if q not in done]
        print(f"{variant}: {len(qids) - len(todo)} done, {len(todo)} to run", flush=True)
        if not todo:
            continue
        target = 30 if variant.startswith("width30") else 60
        variant_keeps = keeps[variant]

        def solve(q: str) -> dict:
            item = questions[q]
            db = widened_db(index, item["db_id"], target)
            schema_text, fk_text, shown = render(db, variant_keeps[q])
            prompt = (PROMPT.replace("{schema}", schema_text).replace("{fks}", fk_text)
                      .replace("{evidence}", item.get("evidence") or "(none)").replace("{question}", item["question"]))
            started = time.perf_counter()
            try:
                text, usage = generate(prompt)
            except Exception as exc:
                return {"question_id": q, "variant": variant, "status": "failed", "error": str(exc)[:300]}
            sql = extract_sql(text)
            rows, error = run_sql(item["db_id"], sql)
            gold_rows, gold_error = golds[q]
            return {
                "question_id": q, "variant": variant, "status": "ok", "sql": sql,
                "exec_error": error, "gold_error": gold_error,
                "correct": gold_error is None and error is None and rows == gold_rows,
                "columns_shown": shown,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "seconds": round(time.perf_counter() - started, 3),
            }

        finished = failed = 0
        with path.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(workers) as pool:
            for future in as_completed([pool.submit(solve, q) for q in todo]):
                record = future.result()
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                finished += 1
                failed += record["status"] != "ok"
                if finished % 100 == 0 or finished == len(todo):
                    print(f"  {variant}: {finished}/{len(todo)} failed={failed} peak_inflight={generate.peak}", flush=True)


def bootstrap(a: list, b: list, rounds: int = 2000, seed: int = 0) -> dict:
    rng = random.Random(seed)
    n = len(a)
    diffs = sorted(sum(a[i] - b[i] for i in (rng.randrange(n) for _ in range(n))) / n for _ in range(rounds))
    return {"diff": (sum(a) - sum(b)) / n, "ci95": [diffs[int(0.025 * rounds)], diffs[int(0.975 * rounds) - 1]]}


def summarize(variants, qids, gold, keeps) -> dict:
    records = {v: load_records(OUT_DIR / f"{v}.jsonl") for v in variants}
    records["full"] = load_records(OUT_DIR / "full.jsonl")
    records["full@oracle"] = load_records(OUT_DIR / "full@oracle.jsonl")
    common = [q for q in qids if all(q in records[v] for v in records) and records["full"][q]["gold_error"] is None]
    out = {"n": len(common), "variants": {}, "contrasts": []}
    for name, recs in records.items():
        rs = [recs[q] for q in common]
        keep = keeps.get(name)
        out["variants"][name] = {
            "ex": sum(r["correct"] for r in rs) / len(rs),
            "exec_error_rate": sum(bool(r["exec_error"]) for r in rs) / len(rs),
            "avg_columns_shown": sum(r["columns_shown"] for r in rs) / len(rs),
            "avg_prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rs) / len(rs),
            "full_recall": (sum(gold[q]["columns"] <= keep[q] for q in common) / len(common)) if keep else None,
        }
    for a, b in ((v, "full") for v in variants):
        out["contrasts"].append({"a": a, "b": b, **bootstrap([int(records[a][q]["correct"]) for q in common],
                                                             [int(records[b][q]["correct"]) for q in common])})
    out["contrasts"].append({"a": "width60oracle", "b": "full@oracle",
                             **bootstrap([int(records["width60oracle"][q]["correct"]) for q in common],
                                         [int(records["full@oracle"][q]["correct"]) for q in common])})
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--summary", default=str(HERE / "width_summary.json"))
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    load_environment()
    gold = load_gold()
    questions = {}
    for item in json.loads(QUESTIONS.read_text(encoding="utf-8")):
        questions.setdefault(str(item["question_id"]), item)
    index = schema_index()
    keeps = {v: keep_sets(v, index, gold) for v in args.variants}
    qids = sorted(set(gold) & set(questions), key=int)[: args.limit or None]
    print(f"{len(qids)} questions x {len(args.variants)} variants", flush=True)

    if not args.summary_only:
        golds = gold_results(qids, questions, gold, 6)
        generate = Generator(MAX_API_CONCURRENCY)
        run_variants(args.variants, qids, keeps, questions, golds, index, generate, MAX_API_CONCURRENCY)
        print(f"generator calls {generate.calls}, tokens in/out {generate.tokens_in}/{generate.tokens_out}", flush=True)

    summary = summarize(args.variants, qids, gold, keeps)
    Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [
        f"共同题数 {summary['n']}；合成扩宽（问题与 gold 不变，只加宽 schema）。",
        "",
        "| 变体 | EX | 执行报错率 | 平均列数 | 平均 prompt tokens | gold 全召回 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, row in summary["variants"].items():
        fr = "-" if row["full_recall"] is None else f"{100 * row['full_recall']:.1f}"
        lines.append(f"| {name} | {100 * row['ex']:.1f} | {100 * row['exec_error_rate']:.1f} | "
                     f"{row['avg_columns_shown']:.1f} | {row['avg_prompt_tokens']:.0f} | {fr} |")
    lines += ["", "| 对比 | ΔEX | 95% CI |", "|---|---:|---|"]
    for c in summary["contrasts"]:
        lines.append(f"| {c['a']} − {c['b']} | {100 * c['diff']:+.1f} | [{100 * c['ci95'][0]:+.1f}, {100 * c['ci95'][1]:+.1f}] |")
    Path(args.summary).with_name(Path(args.summary).stem.replace("summary", "tables") + ".md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
