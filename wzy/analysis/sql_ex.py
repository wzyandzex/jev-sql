"""Downstream check: generate SQL from each method's linked schema and measure execution accuracy (EX).

Usage:
    ../.venv/Scripts/python.exe sql_ex.py --variants full gold exp2_query [--limit N] [--workers 6]

Variants: "full" (whole schema), "gold" (tables/columns of the gold SQL), or any run name in
sl_lib.RUNS, plus "llm_direct+fk". The schema shown to the generator contains exactly the linked
columns (with type and one sample value) and the FK pairs among them. The generator is the LLM
configured by OPENAI_API_MODEL / OPENAI_API_BASE / OPENAI_API_KEY in jev-sql/.env, temperature 0.
EX follows BIRD: predicted and gold result rows compared as sets; each query gets a 30 s budget.
Outputs: <this dir>/sql_ex/<variant>.jsonl (resumable) and a summary printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sl_lib import DB_DIR, QUESTIONS, RUNS, SCHEMA, fk_closure, final_columns, load_gold, load_run

from jev.config import load_environment, require_env
from jev.utils import message_text
from langchain_openai import ChatOpenAI

OUT_DIR = Path(__file__).with_name("sql_ex")
TIMEOUT = 30.0
PROMPT = """Write one SQLite query that answers the question, using only the schema below.
Quote identifiers that contain spaces or special characters with backticks. Return only the SQL, with no explanation.

Schema (table(column type [sample], ...)):
{schema}

Foreign keys:
{fks}

Evidence:
{evidence}

Question:
{question}"""

def schema_index() -> dict:
    """db_id -> ordered tables with column (name, type, sample) and FK pairs, keyed by casefolded names."""
    index = {}
    for db in json.loads(SCHEMA.read_text(encoding="utf-8"))["databases"]:
        tables, fks = [], set()
        for t in db["tables"]:
            tables.append((t["name"], [(c["name"], c["type"], c.get("sample")) for c in t["columns"]]))
            for adj in t.get("adjacent_tables") or []:
                for child, parent in adj.get("foreign_keys") or []:
                    fks.add((tuple(x.casefold() for x in child.rsplit(".", 1)), tuple(x.casefold() for x in parent.rsplit(".", 1)), child, parent))
        index[db["db_id"]] = {"tables": tables, "fks": fks}
    return index


def render(db: dict, keep: set | None) -> tuple[str, str]:
    """Render only the kept (table, column) keys; keep=None renders the whole database."""
    lines = []
    for table, columns in db["tables"]:
        cols = [
            f"{name} {dtype}" + (f" [{str(sample)[:40]}]" if sample is not None else "")
            for name, dtype, sample in columns
            if keep is None or (table.casefold(), name.casefold()) in keep
        ]
        if cols:
            lines.append(f"{table}({', '.join(cols)})")
    fks = sorted(
        f"{child} = {parent}"
        for c, p, child, parent in db["fks"]
        if keep is None or (c in keep and p in keep)
    )
    return "\n".join(lines), "\n".join(fks) or "(none)"


def run_sql(db_id: str, sql: str):
    """Execute read-only with a time budget; return (frozenset of rows, None) or (None, error)."""
    conn = sqlite3.connect(f"{(DB_DIR / db_id / f'{db_id}.sqlite').resolve().as_uri()}?mode=ro", uri=True)
    deadline = time.monotonic() + TIMEOUT
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        return frozenset(conn.execute(sql).fetchall()), None
    except Exception as exc:  # syntax errors, missing columns, timeouts
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        conn.close()


def extract_sql(text: str) -> str:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else parts[0]
        if text.lower().startswith("sql"):
            text = text[3:]
    return text.strip().rstrip(";")

def keep_sets(variant: str, gold: dict) -> dict:
    """question_id -> set of (table, column) keys to show, or None for the full schema."""
    if variant == "full":
        return {q: None for q in gold}
    if variant == "gold":
        return {q: g["columns"] for q, g in gold.items()}
    if variant == "llm_direct+fk":
        records = fk_closure(load_run(RUNS["llm_direct"])[0])
    else:
        records = load_run(RUNS[variant])[0]
    return {q: final_columns(r) for q, r in records.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variants", nargs="+", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    load_environment()
    OUT_DIR.mkdir(exist_ok=True)
    gold = load_gold()
    questions = {}
    for item in json.loads(QUESTIONS.read_text(encoding="utf-8")):
        questions.setdefault(str(item["question_id"]), item)
    keeps = {v: keep_sets(v, gold) for v in args.variants}
    qids = sorted(set(gold).intersection(*(set(k) for k in keeps.values())), key=int)
    if args.limit:
        qids = qids[: args.limit]
    print(f"{len(qids)} questions x {len(args.variants)} variants")

    with ThreadPoolExecutor(args.workers) as pool:
        gold_results = dict(zip(qids, pool.map(lambda q: run_sql(gold[q]["db_id"], questions[q]["SQL"]), qids)))
    print("gold SQL errors:", sum(1 for rows, err in gold_results.values() if err))

    llm = ChatOpenAI(
        model=require_env("OPENAI_API_MODEL"),
        base_url=require_env("OPENAI_API_BASE"),
        api_key=require_env("OPENAI_API_KEY"),
        temperature=0.0,
        timeout=120,
        max_retries=3,
        extra_body={"enable_thinking": False},
    )
    index = schema_index()
    summary = {}
    for variant in args.variants:
        path = OUT_DIR / f"{variant}.jsonl"
        done = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("status") == "ok":
                    done[record["question_id"]] = record

        def solve(q: str) -> dict:
            if q in done:
                return done[q]
            item = questions[q]
            schema_text, fk_text = render(index[item["db_id"]], keeps[variant][q])
            prompt = (PROMPT.replace("{schema}", schema_text).replace("{fks}", fk_text)
                      .replace("{evidence}", item.get("evidence") or "(none)").replace("{question}", item["question"]))
            try:
                sql = extract_sql(message_text(llm.invoke([("human", prompt)]).content))
            except Exception as exc:  # network failure; retried on the next run
                return {"question_id": q, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            rows, error = run_sql(item["db_id"], sql)
            gold_rows, gold_error = gold_results[q]
            return {"question_id": q, "status": "ok", "sql": sql, "exec_error": error, "gold_error": gold_error,
                    "correct": gold_error is None and error is None and rows == gold_rows}

        with ThreadPoolExecutor(args.workers) as pool:
            results = list(pool.map(solve, qids))
        with path.open("w", encoding="utf-8") as handle:
            for record in results:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        scored = [r for r in results if r["status"] == "ok" and not r["gold_error"]]
        row = {"n": len(scored), "failed": sum(r["status"] != "ok" for r in results),
               "ex": sum(r["correct"] for r in scored) / len(scored),
               "exec_error_rate": sum(bool(r["exec_error"]) for r in scored) / len(scored)}
        for level in ("simple", "moderate", "challenging"):
            sub = [r for r in scored if gold[r["question_id"]]["difficulty"] == level]
            row[level] = sum(r["correct"] for r in sub) / len(sub) if sub else None
        summary[variant] = row
        print(variant, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()}, flush=True)

    (OUT_DIR / "summary.json").write_text(json.dumps({"n_questions": len(qids), "variants": summary}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
