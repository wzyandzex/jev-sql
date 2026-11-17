"""Downstream EX for any split prepared in the jev-sql layout (BIRD dev, Spider dev).

Usage:
    python sql_ex_split.py --questions <questions.json> --databases <db dir> --schema <schema.json> \
        --variants full gold jev_dev_tab0.1_shard0 --out-dir outputs/runs/sql_ex_dev
    python sql_ex_split.py ... --summary-only                (recompute the summary)

Question records need question_id, db_id, question, evidence and SQL. Gold columns are read with the
SQLite authorizer (jev.evaluation). Variants: full (whole schema), gold (gold columns), <run> (final
columns of outputs/runs/<run>/batch_answers.jsonl), <run>~s<thr> (re-threshold its column Scores).
EX, the 30 s / 200k-row budget and the prompt are the same as sql_ex.py; at most 5 requests in flight.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sl_lib import OUTPUTS, final_columns, forced_columns, load_run, scored_columns
from sql_ex import (
    MAX_API_CONCURRENCY,
    PROMPT,
    Generator,
    extract_sql,
    full_columns,
    gold_results,
    render,
    run_sql,
    schema_index,
)

from jev.config import load_environment
from jev.evaluation import extract_sql_schema


def load_questions(path: Path) -> dict:
    out = {}
    for item in json.loads(path.read_text(encoding="utf-8")):
        out.setdefault(str(item["question_id"]), item)
    return out


def load_gold_split(questions: dict, db_dir: Path) -> dict:
    gold = {}
    for q, item in questions.items():
        tables, columns, error = extract_sql_schema(item["SQL"], db_dir / item["db_id"] / f"{item['db_id']}.sqlite")
        if error:
            continue
        gold[q] = {"db_id": item["db_id"], "tables": set(tables), "columns": set(columns)}
    return gold


def keep_sets(variant: str, gold: dict, schema_path: Path) -> dict:
    if variant == "full":
        return {q: full_columns(g["db_id"], schema_path) for q, g in gold.items()}
    if variant == "gold":
        return {q: g["columns"] for q, g in gold.items()}
    base = variant.split("~s", 1)[0]
    records = load_run(base)[0]
    if "~s" in variant:
        threshold = float(variant.split("~s", 1)[1])
        return {
            q: {k for k, (_, s) in scored_columns(r).items() if s >= threshold} | forced_columns(r)
            for q, r in records.items()
        }
    return {q: final_columns(r) for q, r in records.items()}


def bootstrap(a: list, b: list, rounds: int = 2000, seed: int = 0) -> dict:
    rng = random.Random(seed)
    n = len(a)
    diffs = sorted(sum(a[i] - b[i] for i in (rng.randrange(n) for _ in range(n))) / n for _ in range(rounds))
    return {"diff": (sum(a) - sum(b)) / n, "ci95": [diffs[int(0.025 * rounds)], diffs[int(0.975 * rounds) - 1]]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", required=True)
    parser.add_argument("--databases", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--api-workers", type=int, default=MAX_API_CONCURRENCY)
    args = parser.parse_args()

    load_environment()
    questions = load_questions(Path(args.questions))
    db_dir = Path(args.databases)
    schema_path = Path(args.schema)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gold = load_gold_split(questions, db_dir)
    keeps = {v: keep_sets(v, gold, schema_path) for v in args.variants}
    qids = sorted(set(gold).intersection(*(set(k) for k in keeps.values())), key=int)[: args.limit or None]
    print(f"{len(qids)} questions x {len(args.variants)} variants; {len(gold)} gold parsed of {len(questions)}", flush=True)

    if not args.summary_only:
        golds = gold_results(qids, questions, gold, 6, db_dir=db_dir, cache_path=out_dir / "gold_results.pkl")
        generate = Generator(args.api_workers)
        index = schema_index(schema_path)
        for variant in args.variants:
            path = out_dir / f"{variant.replace('~', '_')}.jsonl"
            done = {}
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        record = json.loads(line)
                        if record.get("status") == "ok":
                            done[record["question_id"]] = record
            todo = [q for q in qids if q not in done]
            print(f"{variant}: {len(qids) - len(todo)} done, {len(todo)} to run", flush=True)
            if not todo:
                continue

            def solve(q: str, variant=variant) -> dict:
                item = questions[q]
                schema_text, fk_text, shown = render(index[item["db_id"]], keeps[variant][q])
                prompt = (PROMPT.replace("{schema}", schema_text).replace("{fks}", fk_text)
                          .replace("{evidence}", item.get("evidence") or "(none)").replace("{question}", item["question"]))
                started = time.perf_counter()
                try:
                    text, usage = generate(prompt)
                except Exception as exc:
                    return {"question_id": q, "variant": variant, "status": "failed", "error": str(exc)[:300]}
                sql = extract_sql(text)
                rows, error = run_sql(item["db_id"], sql, db_dir)
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
            with path.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(args.api_workers) as pool:
                for future in as_completed([pool.submit(solve, q) for q in todo]):
                    record = future.result()
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    finished += 1
                    failed += record["status"] != "ok"
                    if finished % 100 == 0 or finished == len(todo):
                        print(f"  {variant}: {finished}/{len(todo)} failed={failed}", flush=True)
        print(f"generator calls {generate.calls}, peak in-flight {generate.peak}", flush=True)

    records = {}
    for variant in args.variants:
        path = out_dir / f"{variant.replace('~', '_')}.jsonl"
        recs = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    if record.get("status") == "ok":
                        recs[record["question_id"]] = record
        records[variant] = recs
    common = [q for q in qids if all(q in records[v] for v in args.variants) and records[args.variants[0]][q]["gold_error"] is None]
    out = {"n": len(common), "variants": {}, "contrasts": []}
    for v in args.variants:
        rs = [records[v][q] for q in common]
        out["variants"][v] = {
            "ex": sum(r["correct"] for r in rs) / len(rs),
            "exec_error_rate": sum(bool(r["exec_error"]) for r in rs) / len(rs),
            "avg_columns_shown": sum(r["columns_shown"] for r in rs) / len(rs),
            "avg_prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rs) / len(rs),
            "full_recall": sum(gold[q]["columns"] <= keeps[v][q] for q in common) / len(common),
        }
    for v in args.variants:
        if v == "full":
            continue
        out["contrasts"].append({"a": v, "b": "full", **bootstrap(
            [int(records[v][q]["correct"]) for q in common], [int(records["full"][q]["correct"]) for q in common])})
    summary_path = Path(args.summary) if args.summary else out_dir / "summary.json"
    summary_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(common)} scored questions")
    for v, row in out["variants"].items():
        print(f"{v:>22}: EX {100 * row['ex']:.1f}  err {100 * row['exec_error_rate']:.1f}  cols {row['avg_columns_shown']:.1f}  "
              f"tokens {row['avg_prompt_tokens']:.0f}  full_recall {100 * row['full_recall']:.1f}")
    for c in out["contrasts"]:
        print(f"{c['a']} - full: {100 * c['diff']:+.1f} [{100 * c['ci95'][0]:+.1f}, {100 * c['ci95'][1]:+.1f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
