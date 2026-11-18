"""Downstream EX on Spider 2.0-lite SQLite tasks, compared with the official result CSVs.

Usage:
    python sql_ex_spider2.py --variants full <run> <run>~s0.5 --out-dir outputs/runs/sql_ex_spider2
    python sql_ex_spider2.py --variants full --limit 5 --summary <tmp.json>     (smoke test)

Spider2-lite publishes gold *results* (exec_result/<id>_{a,b,c}.csv), not gold SQL, so EX mirrors the
official comparator (spider2-lite/evaluation_suite/evaluate_utils.py compare_pandas_table): each gold
column vector must match some predicted column vector, element-wise with 1e-2 tolerance, optionally
order-insensitive; a task passes if any gold variant matches. condition_cols and ignore_order come
from the official spider2lite_eval.jsonl. Predicted rows are read from the SQLite database directly,
with the same 30 s / 200k-row budget and 5 in-flight API cap as the other EX runners.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sl_lib import final_columns, forced_columns, load_run, scored_columns
from sql_ex import MAX_API_CONCURRENCY, PROMPT, Generator, extract_sql, full_columns, render, schema_index

from jev.config import load_environment

TIMEOUT = 30.0
MAX_ROWS = 200_000


def to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def is_na(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def vectors_match(v1, v2, tol=1e-2, ignore_order=False) -> bool:
    if ignore_order:
        key = lambda x: (is_na(x), str(x), isinstance(x, (int, float)))
        v1, v2 = sorted(v1, key=key), sorted(v2, key=key)
    if len(v1) != len(v2):
        return False
    for a, b in zip(v1, v2):
        if is_na(a) and is_na(b):
            continue
        if is_na(a) != is_na(b):
            return False
        na, nb = to_number(a), to_number(b)
        if na is not None and nb is not None:
            if not math.isclose(na, nb, abs_tol=tol):
                return False
        elif str(a) != str(b):
            return False
    return True


def columns_match(pred_rows: list, gold_rows: list, condition_cols: list, ignore_order: bool) -> bool:
    indices = list(condition_cols) if condition_cols else list(range(len(gold_rows[0]) if gold_rows else 0))
    gold_vectors = [[row[j] for row in gold_rows] for j in indices]
    pred_vectors = [[row[j] for row in pred_rows] for j in range(len(pred_rows[0]) if pred_rows else 0)]
    for gold in gold_vectors:
        if not any(vectors_match(gold, pred, ignore_order=ignore_order) for pred in pred_vectors):
            return False
    return True


def task_matches(pred_rows: list, gold_variants: list, condition_cols, ignore_order: bool) -> bool:
    """condition_cols follows the official format: one index list per gold variant (or one shared list)."""
    if not gold_variants:
        return False
    if condition_cols in ([], [[]], [None], None):
        per_variant = [[] for _ in gold_variants]
    elif len(gold_variants) > 1 and not all(isinstance(x, list) for x in condition_cols):
        per_variant = [condition_cols for _ in gold_variants]
    else:
        per_variant = list(condition_cols)
    for gold, cols in zip(gold_variants, per_variant):
        if columns_match(pred_rows, gold, cols or [], ignore_order):
            return True
    return False


def load_gold_csvs(gold_dir: Path, instance_id: str) -> list:
    variants = []
    for tag in "abc":
        path = gold_dir / f"{instance_id}_{tag}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        if not rows:
            continue
        variants.append(rows[1:])  # drop the header
    return variants


def run_rows(db_id: str, sql: str, db_dir: Path) -> tuple[list | None, str | None]:
    uri = f"{(db_dir / db_id / f'{db_id}.sqlite').resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    deadline = time.monotonic() + TIMEOUT
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        rows = conn.execute(sql).fetchmany(MAX_ROWS + 1)
        if len(rows) > MAX_ROWS:
            return None, f"RowLimit: more than {MAX_ROWS} rows"
        return rows, None
    except sqlite3.OperationalError as exc:
        return None, "Timeout" if "interrupted" in str(exc) else f"OperationalError: {str(exc)[:120]}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        conn.close()


def keep_sets(variant: str, tasks: dict, schema_path: Path) -> dict:
    if variant == "full":
        return {q: full_columns(t["db_id"], schema_path) for q, t in tasks.items()}
    base, _, threshold = variant.partition("~s")
    records = load_run(base)[0]
    if threshold:
        return {
            q: {k for k, (_, s) in scored_columns(r).items() if s >= float(threshold)} | forced_columns(r)
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
    parser.add_argument("--questions", default="data/spider2/dev_questions.json")
    parser.add_argument("--databases", default="data/spider2/databases")
    parser.add_argument("--schema", default="outputs/schema/spider2_sqlite_schema.json")
    parser.add_argument("--gold-dir", default="data/spider2_gold")
    parser.add_argument("--eval-jsonl", default="data/spider2lite_eval.jsonl")
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    load_environment()
    tasks = {}
    for item in json.loads(Path(args.questions).read_text(encoding="utf-8")):
        tasks.setdefault(str(item["question_id"]), item)
    meta = {}
    for line in Path(args.eval_jsonl).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            meta[str(row["instance_id"])] = row
    qids = sorted(tasks)[: args.limit or None]
    gold_dir = Path(args.gold_dir)
    db_dir = Path(args.databases)
    schema_path = Path(args.schema)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    keeps = {v: keep_sets(v, tasks, schema_path) for v in args.variants}
    qids = [q for q in qids if all(q in keeps[v] for v in args.variants)]
    golds = {q: load_gold_csvs(gold_dir, q) for q in qids}
    has_gold = [q for q in qids if golds[q]]
    print(f"{len(qids)} tasks ({len(has_gold)} with gold results) x {len(args.variants)} variants", flush=True)

    if not args.summary_only:
        generate = Generator(MAX_API_CONCURRENCY)
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
                item = tasks[q]
                schema_text, fk_text, shown = render(index[item["db_id"]], keeps[variant][q])
                prompt = (PROMPT.replace("{schema}", schema_text).replace("{fks}", fk_text)
                          .replace("{evidence}", "(none)").replace("{question}", item["question"]))
                started = time.perf_counter()
                try:
                    text, usage = generate(prompt)
                except Exception as exc:
                    return {"question_id": q, "variant": variant, "status": "failed", "error": str(exc)[:300]}
                sql = extract_sql(text)
                rows, error = run_rows(item["db_id"], sql, db_dir)
                m = meta.get(q) or {}
                condition_cols = m.get("condition_cols") or []
                if isinstance(condition_cols, str):
                    try:
                        condition_cols = json.loads(condition_cols)
                    except json.JSONDecodeError:
                        condition_cols = []
                correct = error is None and task_matches(
                    rows or [], golds[q], condition_cols, str(m.get("ignore_order", "")).lower() == "true")
                return {
                    "question_id": q, "variant": variant, "status": "ok", "sql": sql,
                    "exec_error": error, "correct": correct, "columns_shown": shown,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "seconds": round(time.perf_counter() - started, 3),
                }

            finished = failed = 0
            with path.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(MAX_API_CONCURRENCY) as pool:
                for future in as_completed([pool.submit(solve, q) for q in todo]):
                    record = future.result()
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    finished += 1
                    failed += record["status"] != "ok"
                    if finished % 20 == 0 or finished == len(todo):
                        print(f"  {variant}: {finished}/{len(todo)} failed={failed}", flush=True)
        print(f"generator calls {generate.calls}", flush=True)

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
    common = [q for q in has_gold if all(q in records[v] for v in args.variants)]
    out = {"n": len(common), "variants": {}, "contrasts": []}
    for v in args.variants:
        rs = [records[v][q] for q in common]
        out["variants"][v] = {
            "ex": sum(r["correct"] for r in rs) / len(rs),
            "exec_error_rate": sum(bool(r["exec_error"]) for r in rs) / len(rs),
            "avg_columns_shown": sum(r["columns_shown"] for r in rs) / len(rs),
            "avg_prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rs) / len(rs),
        }
    for v in args.variants:
        if v == "full":
            continue
        out["contrasts"].append({"a": v, "b": "full", **bootstrap(
            [int(records[v][q]["correct"]) for q in common], [int(records["full"][q]["correct"]) for q in common])})
    summary_path = Path(args.summary) if args.summary else out_dir / "summary.json"
    summary_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(common)} tasks scored")
    for v, row in out["variants"].items():
        print(f"{v:>26}: EX {100 * row['ex']:.1f}  err {100 * row['exec_error_rate']:.1f}  cols {row['avg_columns_shown']:.1f}  tokens {row['avg_prompt_tokens']:.0f}")
    for c in out["contrasts"]:
        print(f"{c['a']} - full: {100 * c['diff']:+.1f} [{100 * c['ci95'][0]:+.1f}, {100 * c['ci95'][1]:+.1f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
