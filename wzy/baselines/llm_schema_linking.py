"""Reference baseline: a generative LLM picks tables/columns directly (no JEV).

Usage:
    ../.venv/Scripts/python.exe llm_schema_linking.py --out ../outputs/runs/llm_direct_qwen [--limit N]

Reads the jev-sql schema JSON and BIRD questions, asks the LLM configured by
OPENAI_API_MODEL / OPENAI_API_BASE / OPENAI_API_KEY (loaded from jev-sql/.env)
for the minimal set of columns, and writes batch_answers.jsonl records in the
same shape as jev-sql (linked_columns_union), so jev-sql's evaluator can score it.
Questions with status=ok in an existing output are skipped (resume).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT
sys.path.insert(0, str(REPO))

from jev.config import load_environment, require_env  # noqa: E402
from jev.utils import message_text, parse_json_object  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

PROMPT = """You are the schema linking module of a text-to-SQL system.
Given a question, its evidence, and a SQLite database schema, select the minimal set of columns needed to write the SQL query that answers the question. Include columns used in SELECT, WHERE, JOIN ON, GROUP BY, ORDER BY and inside aggregate functions. Include join key columns on both sides of every join you would need.

Return only a JSON object, with no explanation and no markdown fence:
{"columns": ["table.column", ...]}

Schema (table(column type [sample value], ...)):
{schema}

Foreign keys:
{fks}

Question:
{question}

Evidence:
{evidence}"""


def render_schema(database: dict) -> tuple[str, str]:
    tables, fks = [], set()
    for table in database["tables"]:
        cols = []
        for column in table["columns"]:
            sample = column.get("sample")
            sample_text = f" [{str(sample)[:40]}]" if sample is not None else ""
            pk = " PK" if column["name"] in table["primary_key"] else ""
            cols.append(f"{column['name']} {column['type']}{pk}{sample_text}")
        tables.append(f"{table['name']}({', '.join(cols)})")
        for adjacent in table.get("adjacent_tables") or []:
            for child, parent in adjacent.get("foreign_keys") or []:
                fks.add(f"{child} = {parent}")
    return "\n".join(tables), "\n".join(sorted(fks)) or "(none)"


def canonical_columns(database: dict, names: list) -> tuple[list[dict], list[str]]:
    lookup = {}
    for table in database["tables"]:
        for column in table["columns"]:
            lookup[(table["name"].casefold(), column["name"].casefold())] = (table["name"], column["name"], column["type"])
    kept, invalid, seen = [], [], set()
    for name in names:
        if not isinstance(name, str) or "." not in name:
            invalid.append(str(name))
            continue
        table, column = (part.strip().strip('`"[]') for part in name.rsplit(".", 1))
        key = (table.casefold(), column.casefold())
        if key not in lookup:
            invalid.append(name)
            continue
        if key in seen:
            continue
        seen.add(key)
        t, c, dtype = lookup[key]
        kept.append({"table": t, "column": c, "dtype": dtype, "best_score": None, "mentions": [], "sources": ["llm"]})
    return kept, invalid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    load_environment()
    schema = json.loads((REPO / "outputs/schema/minidev_schema.json").read_text(encoding="utf-8"))
    databases = {db["db_id"]: db for db in schema["databases"]}
    questions = json.loads((REPO / "BIRD/MINIDEV/mini_dev_sqlite.json").read_text(encoding="utf-8"))
    if args.limit > 0:
        questions = questions[: args.limit]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "batch_answers.jsonl"
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("status") == "ok":
                    done.add(str(record["question_id"]))

    llm = ChatOpenAI(
        model=require_env("OPENAI_API_MODEL"),
        base_url=require_env("OPENAI_API_BASE"),
        api_key=require_env("OPENAI_API_KEY"),
        temperature=0.0,
        timeout=120,
        max_retries=3,
        extra_body={"enable_thinking": False},
    )
    rendered = {db_id: render_schema(db) for db_id, db in databases.items()}

    ok = failed = 0
    with out_path.open("a", encoding="utf-8") as handle:
        for index, item in enumerate(questions, start=1):
            qid = item["question_id"]
            if str(qid) in done:
                continue
            database = databases[item["db_id"]]
            schema_text, fk_text = rendered[item["db_id"]]
            prompt = (
                PROMPT.replace("{schema}", schema_text)
                .replace("{fks}", fk_text)
                .replace("{question}", item["question"])
                .replace("{evidence}", item.get("evidence") or "(none)")
            )
            started = time.perf_counter()
            try:
                response = llm.invoke([("human", prompt)])
                data = parse_json_object(message_text(response.content))
                kept, invalid = canonical_columns(database, data.get("columns") or [])
                usage = (response.response_metadata or {}).get("token_usage") or {}
                record = {
                    "question_id": qid,
                    "db_id": item["db_id"],
                    "question": item["question"],
                    "linked_columns_union": kept,
                    "linked_tables": sorted({entry["table"] for entry in kept}),
                    "invalid_columns": invalid,
                    "usage": {
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                    },
                    "elapsed": {"total_seconds": time.perf_counter() - started},
                    "status": "ok",
                }
                ok += 1
            except Exception as exc:  # network or parse failure; retried on the next run
                record = {"question_id": qid, "db_id": item["db_id"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                failed += 1
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            print(f"[{index}/{len(questions)}] qid={qid} {record['status']} cols={len(record.get('linked_columns_union', []))}", flush=True)
    print(f"done ok={ok} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
