"""Prepare Spider 1.0 dev for the jev-sql pipeline (BIRD-format questions + schema JSON).

Usage:
    python spider_prep.py --questions <spider dev.json> --databases <database dir> \
        --out-questions <questions.json> --out-schema <schema.json>

Spider has no BIRD-style tables.json, so the schema is introspected from each SQLite file: table and
column names, declared types normalised to integer/text/real, one sample value per column, primary
keys and foreign keys. Questions are rewritten to the BIRD field names (SQL <- query, evidence empty,
difficulty "unknown"); ids are 0..n-1.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import sl_lib  # noqa: F401  (adds the repo root to sys.path)
from jev.schema.bird_json import fetch_sample, infer_date_format

TYPE_MAP = {
    "int": "integer", "integer": "integer", "bigint": "integer", "smallint": "integer", "tinyint": "integer",
    "real": "real", "double": "real", "float": "real", "numeric": "real", "decimal": "real",
}


def normalise_type(declared: str | None) -> str:
    base = (declared or "").strip().lower().split("(")[0].split()[0] if declared else ""
    return TYPE_MAP.get(base, "text" if base in ("", "varchar", "nvarchar", "char", "text", "clob") else base)


def build_schema(db_root: Path, dataset: str, split: str) -> dict:
    databases = []
    for db_dir in sorted(p for p in db_root.iterdir() if p.is_dir()):
        sqlite_path = db_dir / f"{db_dir.name}.sqlite"
        if not sqlite_path.exists():
            continue
        conn = sqlite3.connect(f"{sqlite_path.as_uri()}?mode=ro", uri=True)
        tables = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        adjacency = {t: {} for t in tables}
        table_entries = []
        for table in tables:
            info = conn.execute(f'pragma table_info("{table}")').fetchall()
            columns = []
            for _, name, ctype, _, _, _ in info:
                sample = fetch_sample(conn, table, name)
                columns.append({
                    "name": name,
                    "type": normalise_type(ctype),
                    "sample": sample,
                    "date_format": infer_date_format(sample),
                })
            for row in conn.execute(f'pragma foreign_key_list("{table}")').fetchall():
                parent_table, parent_column, child_column = row[2], row[4] or row[3], row[3]
                pair = [f"{table}.{child_column}", f"{parent_table}.{parent_column}"]
                for source, target in ((table, parent_table), (parent_table, table)):
                    if target in adjacency.get(source, {}):
                        if pair not in adjacency[source][target]:
                            adjacency[source][target].append(pair)
                    else:
                        adjacency.setdefault(source, {})[target] = [pair]
            table_entries.append({
                "name": table,
                "columns": columns,
                "primary_key": [c[1] for c in info if c[5]],
                "adjacent_tables": [],
            })
        by_name = {t["name"]: t for t in table_entries}
        for source, neighbors in adjacency.items():
            if source in by_name:
                by_name[source]["adjacent_tables"] = [
                    {"table": target, "foreign_keys": pairs} for target, pairs in sorted(neighbors.items())
                ]
        conn.close()
        databases.append({"db_id": db_dir.name, "tables": table_entries})
    return {"dataset": dataset, "split": split, "databases": databases}


def convert_questions(src: Path, dst: Path) -> int:
    items = json.loads(src.read_text(encoding="utf-8"))
    out = [
        {
            "question_id": i,
            "db_id": item["db_id"],
            "question": item["question"],
            "evidence": "",
            "SQL": item["query"],
            "difficulty": "unknown",
        }
        for i, item in enumerate(items)
    ]
    dst.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return len(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", required=True, help="Spider dev.json (validation.json)")
    parser.add_argument("--databases", required=True, help="directory with <db>/<db>.sqlite")
    parser.add_argument("--out-questions", required=True)
    parser.add_argument("--out-schema", required=True)
    args = parser.parse_args()

    n = convert_questions(Path(args.questions), Path(args.out_questions))
    schema = build_schema(Path(args.databases), "SPIDER", "DEV")
    Path(args.out_schema).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_schema).write_text(json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8")
    total_tables = sum(len(db["tables"]) for db in schema["databases"])
    total_cols = sum(len(t["columns"]) for db in schema["databases"] for t in db["tables"])
    print(f"questions {n} -> {args.out_questions}")
    print(f"schema: {len(schema['databases'])} databases, {total_tables} tables, {total_cols} columns -> {args.out_schema}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
