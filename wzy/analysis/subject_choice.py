"""Which table is the question about: one JEV Choice per question over the kept tables.

Usage:
    python subject_choice.py --limit 12 --out <pilot.jsonl>    (pilot)
    python subject_choice.py                                   (every question with >= 2 kept tables)
    python subject_choice.py --report

The keep set is the 2b work point (column Score >= 0.5 plus forced FK columns) of run
jev_minidev_no_mention_tab0.1. Options are the kept tables ranked by table Noul, at most five, each
described by its columns and types only -- the Noul values themselves are not shown, so the answer
can be compared with the "highest Noul" baseline on equal terms.

The target is the gold principal table: the table of the outermost FROM, descending into derived
tables. Questions whose target is not among the options, or whose gold SQL has no plain FROM, are
recorded but excluded from the accuracy figures.

Outputs:
    outputs/runs/subject_choice/jev.jsonl   one record per question; reruns skip ok records
    outputs/runs/subject_choice/report.json, report.md
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from choice_disambig import JEV_MODEL, MAX_API_CONCURRENCY, JevGenerator
from rolekeep_check import keep_at, output_tables, principal_table, subject_table
from sl_lib import OUTPUTS, QUESTIONS, load_gold, load_run, table_nouls
from sql_ex import schema_index

from jev.config import load_environment

RUN = "jev_minidev_no_mention_tab0.1"
THRESHOLD = 0.5
MAX_OPTIONS = 5
OUT_DIR = OUTPUTS / "runs" / "subject_choice"


def load_items() -> dict:
    items = {}
    for item in json.loads(QUESTIONS.read_text(encoding="utf-8")):
        items.setdefault(str(item["question_id"]), item)
    return items


def option_text(name: str, columns: list) -> str:
    shown = [f"{c} {ty}" for c, ty in columns]
    tail = ", ..." if len(shown) > 10 else ""
    return f"{name}({', '.join(shown[:10])}{tail})"


def build_payload(item: dict, db: dict, candidates: list) -> tuple[dict, dict, dict]:
    """JEV state, the single question, and the option key -> table name map."""
    table_columns = {name.casefold(): (name, [(c, ty) for c, ty, _ in columns])
                     for name, columns in db["tables"]}
    criteria, keyed = {}, {}
    for i, table in enumerate(candidates, start=1):
        name, columns = table_columns[table]
        keyed[f"t{i}"] = name
        criteria[f"t{i}"] = option_text(name, columns)
    questions = {
        "q1": {
            "type": "choice",
            "instructions": (
                "Which table is the main entity of the question -- the table whose rows the query "
                "counts, aggregates or lists as the result? The other options are only joined to it."
            ),
            "criteria": criteria,
        },
    }
    state = {
        "task": "subject_table_selection",
        "instruction": (
            "One of the tables below is the subject of the question. Choose the table whose rows the "
            "query counts, aggregates or lists; every other table is reached from it by a join. "
            "Answer only the provided question."
        ),
        "context": {
            "database": item["db_id"],
            "question": item["question"],
            "evidence": item.get("evidence") or "",
        },
        "output_schema": {"choice": "one option key of the question's criteria"},
    }
    return state, questions, keyed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default="")
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    args = parser.parse_args()

    out = Path(args.out) if args.out else OUT_DIR / "jev.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    gold = load_gold()
    items = load_items()
    records = load_run(RUN)[0]
    index = schema_index()

    done = {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("status") == "ok":
                    done[record["question_id"]] = record

    todo = []
    for qid in sorted(gold, key=int):
        if qid not in records or qid not in items or qid in done:
            continue
        keep = keep_at(records[qid], args.threshold)
        tables = {t for t, _ in keep}
        if len(tables) < 2:
            continue
        todo.append(qid)
    todo = todo[: args.limit or None]
    print(f"{len(done)} done, {len(todo)} to run -> {out}", flush=True)
    if not todo:
        return report(out)

    generate = JevGenerator(MAX_API_CONCURRENCY)
    load_environment()

    def solve(qid: str) -> dict:
        item = items[qid]
        record = records[qid]
        keep = keep_at(record, args.threshold)
        tables = {t for t, _ in keep}
        nouls = table_nouls(record)
        _, ranked = subject_table(tables, nouls)
        candidates = [t for _, t in ranked][:MAX_OPTIONS]
        state, questions, keyed = build_payload(item, index[item["db_id"]], candidates)
        started = time.perf_counter()
        try:
            answers = generate(state, questions)
        except Exception as exc:
            return {"question_id": qid, "status": "failed", "error": str(exc)[:300]}
        answer = answers.get("q1") or {}
        chosen = keyed.get(answer.get("choice"))
        target, reason = principal_table(item["SQL"])
        return {
            "question_id": qid, "status": "ok", "db_id": item["db_id"],
            "options": keyed, "n_tables": len(tables), "n_options": len(candidates),
            "choice": answer.get("choice"), "chosen": chosen,
            "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities"),
            "target": target, "target_reason": reason,
            "target_in_options": target in candidates if target else None,
            "max_noul": candidates[0] if candidates else None,
            "seconds": round(time.perf_counter() - started, 3),
        }

    finished = failed = 0
    with out.open("a", encoding="utf-8") as handle, ThreadPoolExecutor(MAX_API_CONCURRENCY) as pool:
        for future in as_completed([pool.submit(solve, q) for q in todo]):
            record = future.result()
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            finished += 1
            failed += record["status"] != "ok"
            if finished % 25 == 0 or finished == len(todo):
                print(f"  {finished}/{len(todo)} failed={failed}", flush=True)
    print(f"calls {generate.calls}, peak in-flight {generate.peak}, "
          f"tokens in/out {generate.tokens_in}/{generate.tokens_out}", flush=True)
    return report(out)


def report(out: Path) -> int:
    records = {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("status") == "ok":
                    records[record["question_id"]] = record
    if not records:
        print("no usable records")
        return 0
    gold = load_gold()
    items = load_items()
    for record in records.values():
        outputs, _ = output_tables(items[record["question_id"]]["SQL"])
        record["output_tables"] = sorted(outputs)
        chosen = (record.get("chosen") or "").casefold()
        record["chosen_in_gold"] = chosen in gold[record["question_id"]]["tables"]

    # two candidate definitions of the subject: the outermost FROM table, and the tables that
    # contribute columns to the SELECT list. Neither is authoritative, so both are reported.
    def summarize(rows, target_of) -> dict:
        if not rows:
            return {"n": 0}
        chosen = (lambda r: (r.get("chosen") or "").casefold())
        return {
            "n": len(rows),
            "choice_acc": sum(chosen(r) in target_of(r) for r in rows) / len(rows),
            "max_noul_acc": sum(r["max_noul"].casefold() in target_of(r) for r in rows) / len(rows),
            "chosen_in_gold": sum(r["chosen_in_gold"] for r in rows) / len(rows),
        }

    from_band = [r for r in records.values() if r.get("target") and r["target_in_options"]]
    out_band = [r for r in records.values() if r["output_tables"]]
    from_target = lambda r: {r["target"]}
    out_target = lambda r: set(r["output_tables"])

    rows = {
        "n_recorded": len(records),
        "avg_options": sum(r["n_options"] for r in records.values()) / len(records),
        "by_from": summarize(from_band, from_target),
        "by_output": summarize(out_band, out_target),
    }
    for threshold in (0.4, 0.5, 0.6, 0.8):
        sub = [r for r in from_band if (r.get("confidence") or 0.0) >= threshold]
        rows[f"from_conf_{threshold}"] = summarize(sub, from_target)

    path = out.parent / "report.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{rows['n_recorded']} records, avg options {rows['avg_options']:.2f}")
    for key, label in (("by_from", "target = outermost FROM"), ("by_output", "target = SELECT-list tables")):
        v = rows[key]
        if not v["n"]:
            print(f"  {label}: no usable rows")
            continue
        print(f"  {label}: n={v['n']:>3}  JEV choice {100 * v['choice_acc']:.1f}%  "
              f"max-Noul {100 * v['max_noul_acc']:.1f}%  chosen∈gold {100 * v['chosen_in_gold']:.1f}%")
    for threshold in (0.4, 0.5, 0.6, 0.8):
        v = rows[f"from_conf_{threshold}"]
        if v["n"]:
            print(f"    confidence >= {threshold}: n={v['n']:>3} JEV {100 * v['choice_acc']:.1f}% "
                  f"max-Noul {100 * v['max_noul_acc']:.1f}%")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
