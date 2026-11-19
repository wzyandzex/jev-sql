"""Offline checks for the (table, role) keep-set idea. No API requests.

Usage:
    python rolekeep_check.py [--threshold 0.5] [--out ../../../outputs/runs/rolekeep/check.json]

Check A: for every same-name group that survives the 2b column budget (|members in keep| >= 2),
compare three structural rules against the oracle resolution (keep the members in gold, drop the
group when no member is gold):
    all      keep every member                      (what the pipeline does today)
    tree     keep members on the FK spanning tree rooted at the subject table
    tree+safe   same, but keep all when no member table is reachable from the subject
    dist1    keep members in the subject table or its direct FK neighbours
    subj     keep the member in the subject table, all when there is none
Groups are split by whether the shared column is a join key (two members are an FK pair), because
connectivity can only resolve join keys.

Check B: is the gold principal table (outermost FROM) recoverable? Measures whether it is inside the
kept table set, whether it is the highest-Noul table, and the size of the candidate set.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import sqlglot
from sqlglot import exp

from choice_disambig import build_groups
from choice_disambig import schema_index as group_index
from sl_lib import QUESTIONS, forced_columns, load_gold, load_run, scored_columns, table_nouls
from sql_ex import schema_index as fk_index

RUN = "jev_minidev_no_mention_tab0.1"
OUT_DIR = Path(__file__).resolve().parents[3] / "outputs" / "runs" / "rolekeep"


def keep_at(record: dict, threshold: float) -> set:
    scored = {k for k, (_, s) in scored_columns(record).items() if s >= threshold}
    return scored | forced_columns(record)


def principal_table(sql: str) -> tuple[str | None, str | None]:
    """Casefolded table of the outermost FROM, descending into derived tables; (None, reason) if absent."""
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return None, "parse_error"
    for _ in range(8):
        if isinstance(tree, (exp.Subquery, exp.Paren)):
            tree = tree.this
            continue
        if isinstance(tree, exp.Union):
            tree = tree.this
            continue
        if isinstance(tree, exp.Select):
            source = tree.args.get("from_") or tree.find(exp.From)
            if source is None:
                return None, "no_from"
            source = source.this
            if isinstance(source, exp.Table):
                return source.name.casefold(), None
            tree = source
            continue
        return None, type(tree).__name__
    return None, "too_deep"


def has_aggregate(sql: str) -> bool:
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return False
    return tree.find(exp.AggFunc) is not None


def output_tables(sql: str) -> tuple[set, str | None]:
    """Casefolded tables whose columns appear in the outermost SELECT list, plus an optional reason.

    Only qualified or unambiguous references are attributed. The subject of a question ("the names
    of the schools ...") shows up here, whereas the outermost FROM table is often the one carrying
    the WHERE conditions.
    """
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return set(), "parse_error"
    for _ in range(8):
        if isinstance(tree, (exp.Subquery, exp.Paren, exp.Union)):
            tree = tree.this
            continue
        break
    if not isinstance(tree, exp.Select):
        return set(), type(tree).__name__
    alias = {}
    for name, node in ((s.alias_or_name, s) for s in tree.args.get("joins") or []):
        alias[name.casefold()] = node.this.name.casefold() if isinstance(node.this, exp.Table) else None
    source = tree.args.get("from_") or tree.find(exp.From)
    if source is not None and isinstance(source.this, exp.Table):
        alias[source.this.alias_or_name.casefold()] = source.this.name.casefold()
    names = {t.casefold() for t in alias.values() if t}
    out = set()
    for item in tree.expressions:
        for column in item.find_all(exp.Column):
            ref = column.table.casefold()
            if ref and ref in alias:
                if alias[ref]:
                    out.add(alias[ref])
            elif not ref and len(names) == 1:
                out.add(next(iter(names)))
    if not out:
        return set(), "no_qualified_column"
    return out, None


def tree_columns(tables: set, fks: set, root: str) -> tuple[set, set]:
    """FK column pairs on a BFS spanning tree over the kept tables, plus the tables it reaches.

    Edges are visited in sorted order so the tree (and the result) does not depend on set iteration.
    """
    adj = defaultdict(list)
    for ckey, pkey in sorted({(ckey, pkey) for ckey, pkey, _, _ in fks}):
        ct, pt = ckey[0], pkey[0]
        if ct in tables and pt in tables and ct != pt:
            adj[ct].append((pt, ckey, pkey))
            adj[pt].append((ct, pkey, ckey))
    columns, reached, queue = set(), {root}, [root]
    while queue:
        current = queue.pop(0)
        for other, mine, theirs in sorted(adj[current], key=lambda x: (x[0], x[1])):
            if other in reached:
                continue
            reached.add(other)
            columns |= {mine, theirs}
            queue.append(other)
    return columns, reached


def subject_table(tables: set, nouls: dict) -> tuple[str | None, list]:
    """Highest-Noul kept table (deterministic tie-break by name) and the kept tables ranked by Noul."""
    ranked = sorted(((nouls.get(t, 0.0), t) for t in tables), key=lambda x: (-x[0], x[1]))
    return (ranked[0][1] if ranked else None), ranked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--out", default=str(OUT_DIR / "check.json"))
    args = parser.parse_args()

    gold = load_gold()
    items = {str(q["question_id"]): q for q in json.loads(QUESTIONS.read_text(encoding="utf-8"))}
    records = load_run(RUN)[0]
    groups_index = group_index()
    fk_index_ = fk_index()

    groups = []      # check A rows
    subjects = []    # check B rows
    for qid in sorted(gold, key=int):
        if qid not in records:
            continue
        g = gold[qid]
        record = records[qid]
        keep = keep_at(record, args.threshold)
        tables = {t for t, _ in keep}
        nouls = table_nouls(record)
        root, ranked = subject_table(tables, nouls)
        principal, reason = principal_table(items[qid]["SQL"])
        gold_root = principal if principal in tables else None
        fks = fk_index_[g["db_id"]]["fks"]

        def seed_columns(seed):
            """FK spanning-tree columns, reached tables and direct FK neighbours of one seed, or Nones."""
            if seed is None:
                return None, None, None
            tree, reached = tree_columns(tables, fks, seed)
            near = {b for ckey, pkey, _, _ in fks
                    for a, b in ((ckey[0], pkey[0]), (pkey[0], ckey[0]))
                    if a == seed and b in tables and b != seed}
            return tree, reached, near

        tree_cols, reached, neighbours = seed_columns(root)
        tree_cols_gold, _, neighbours_gold = seed_columns(gold_root)

        for _, group in build_groups(groups_index[g["db_id"]]).items():
            members = [(m["table"].casefold(), m["column"].casefold()) for _, m in group["members"]]
            present = [m for m in members if m in keep]
            if len(present) < 2:
                continue
            oracle = set(present) & g["columns"]
            fk_group = any(frozenset({a, b}) in groups_index[g["db_id"]]["fks"]
                           for i, a in enumerate(present) for b in present[i + 1:])
            rules = {
                "all": set(present),
                "tree": {m for m in present if m in tree_cols},
                "dist1": {m for m in present if m[0] == root or m[0] in neighbours},
            }
            rules["tree+safe"] = rules["tree"] if rules["tree"] else (
                set() if any(m[0] in reached for m in present) else set(present))
            in_subject = {m for m in present if m[0] == root}
            rules["subj"] = in_subject or set(present)
            children = {ckey for ckey, _, _, _ in fks}
            in_child = {m for m in present if m in children}
            rules["child"] = in_child or set(present)
            # prune: never drop a member when the group has a join-tree member; drop the whole group
            # only when nothing in it lies on the join tree and its tables are reachable at all.
            if any(m in tree_cols for m in present):
                rules["prune"] = set(present)
            elif any(m[0] in reached for m in present):
                rules["prune"] = set()
            else:
                rules["prune"] = set(present)
            if tree_cols_gold is not None:
                rules["tree@gold"] = {m for m in present if m in tree_cols_gold}
                rules["dist1@gold"] = {m for m in present
                                       if m[0] == gold_root or m[0] in neighbours_gold}
            groups.append({
                "qid": qid, "column": group["column_name"], "fk_group": fk_group,
                "oracle": sorted(oracle), "oracle_drops": oracle != set(present),
                "n_members": len(present),
                **{r: sorted(v) for r, v in rules.items()},
            })

        row = {
            "qid": qid, "principal": principal, "reason": reason,
            "n_tables": len(tables), "subject": root,
            "aggregate": has_aggregate(items[qid]["SQL"]),
            "n_gold_tables": len(g["tables"]),
        }
        if principal is None:
            subjects.append({**row, "in_keep": None, "is_subject": None, "rank": None})
        else:
            ranked_names = [t for _, t in ranked]
            row.update({
                "in_keep": principal in tables,
                "is_subject": principal == root,
                "rank": ranked_names.index(principal) + 1 if principal in ranked_names else None,
                "single_gold_table": len(g["tables"]) == 1,
            })
            subjects.append(row)

    # ---- check A summary
    def summarize(rule: str, rows: list) -> dict:
        rows = [r for r in rows if rule in r]
        if not rows:
            return {"n": 0, "exact": 0.0, "dropped_gold": 0.0, "dropped_non_gold": 0.0}
        exact = sum(set(r[rule]) == set(r["oracle"]) for r in rows)
        harm = sum(1 for r in rows if set(r["oracle"]) - set(r[rule]))
        benefit = sum(1 for r in rows if set(r[rule]) - set(r["oracle"]))
        return {
            "n": len(rows), "exact": exact / len(rows),
            "dropped_gold": harm / len(rows),
            "dropped_non_gold": benefit / len(rows),
        }

    rules = ["all", "prune", "tree", "tree+safe", "dist1", "subj", "child", "tree@gold", "dist1@gold"]
    by_split = {
        "fk": [r for r in groups if r["fk_group"]],
        "non_fk": [r for r in groups if not r["fk_group"]],
        "effective": [r for r in groups if r["oracle_drops"]],
    }
    check_a = {name: {rule: summarize(rule, rows) for rule in rules} for name, rows in by_split.items()}

    # ---- check B summary
    valid = [s for s in subjects if s["in_keep"] is not None]
    agg = [s for s in valid if s["aggregate"]]
    multi = [s for s in valid if s["n_tables"] > 1]
    check_b = {
        "n": len(valid),
        "in_keep": sum(s["in_keep"] for s in valid) / len(valid),
        "is_subject": sum(s["is_subject"] for s in valid) / len(valid),
        "in_top5": sum(1 for s in valid if s["rank"] and s["rank"] <= 5) / len(valid),
        "avg_tables": sum(s["n_tables"] for s in valid) / len(valid),
        "agg_n": len(agg),
        "agg_in_keep": sum(s["in_keep"] for s in agg) / len(agg) if agg else 0.0,
        "agg_is_subject": sum(s["is_subject"] for s in agg) / len(agg) if agg else 0.0,
        "multi_n": len(multi),
        "multi_in_keep": sum(s["in_keep"] for s in multi) / len(multi) if multi else 0.0,
        "multi_is_subject": sum(s["is_subject"] for s in multi) / len(multi) if multi else 0.0,
        "single_n": len(valid) - len(multi),
        "missing": {r: sum(1 for s in subjects if s["reason"] == r)
                    for r in {s["reason"] for s in subjects if s["reason"]}},
    }

    out = {
        "threshold": args.threshold, "run": RUN,
        "groups": {"total": len(groups), "fk": len(by_split["fk"]), "non_fk": len(by_split["non_fk"]),
                   "effective": len(by_split["effective"]), "rules": check_a},
        "subject": check_b,
        "rows": {"groups": groups, "subjects": subjects},
    }
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"keep = {RUN} @ column score >= {args.threshold}; {len(valid)} subjects, {len(groups)} groups "
          f"({len(by_split['fk'])} fk / {len(by_split['non_fk'])} non-fk, {len(by_split['effective'])} where oracle drops)")
    print(f"{'split':>10} {'rule':>10} {'n':>5} {'exact':>7} {'dropG':>7} {'dropN':>7}")
    for name, per_rule in check_a.items():
        for rule, m in per_rule.items():
            print(f"{name:>10} {rule:>10} {m['n']:>5} {100 * m['exact']:>6.1f}% {100 * m['dropped_gold']:>6.1f}% "
                  f"{100 * m['dropped_non_gold']:>6.1f}%")
    print()
    print(f"subject table: in keep {100 * check_b['in_keep']:.1f}%  argmax-noul {100 * check_b['is_subject']:.1f}%  "
          f"top-5 {100 * check_b['in_top5']:.1f}%  avg kept tables {check_b['avg_tables']:.2f}")
    print(f"  multi-table (n={check_b['multi_n']}): in keep {100 * check_b['multi_in_keep']:.1f}%  "
          f"argmax-noul {100 * check_b['multi_is_subject']:.1f}%   single-table questions {check_b['single_n']}")
    print(f"  aggregate questions (n={check_b['agg_n']}): in keep {100 * check_b['agg_in_keep']:.1f}%  "
          f"argmax-noul {100 * check_b['agg_is_subject']:.1f}%")
    print(f"  skipped: {check_b['missing']}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
