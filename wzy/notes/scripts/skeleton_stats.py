"""Skeleton / slot statistics of BIRD mini-dev gold SQL (for the Jev template-generation notes).

Usage:
    python skeleton_stats.py [--data PATH] [--tables PATH] [--train-json PATH] [--examples] [--keep-duplicates]

Repeated question_ids are dropped by default (mini-dev has 500 rows, 498 unique questions).

Parses every gold SQL with sqlglot (SQLite dialect) and prints a plain-text report to stdout:
clause-level skeleton coverage, grammar tiers, query families, decision counts, arithmetic
templates, evidence formulas, literal grounding and FK-join statistics. Writes no files.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import sqlglot
from sqlglot import exp

# Defaults follow the repo layout (BIRD/ at the repo root, as in the main README); override with --data / --tables.
_REPO = Path(__file__).resolve().parents[3]
DATA = str(_REPO / "BIRD" / "MINIDEV" / "mini_dev_sqlite.json")
TABLES = str(_REPO / "BIRD" / "MINIDEV" / "dev_tables.json")
DIFFS = ("simple", "moderate", "challenging")

AGG = (exp.Count, exp.Sum, exp.Avg, exp.Max, exp.Min)
COND = (exp.If, exp.Case)
ARITH = (exp.Add, exp.Sub, exp.Mul, exp.Div)
CMP_BIN = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
PRED = CMP_BIN + (exp.Like, exp.Between, exp.In, exp.Is)
SETOP = tuple(c for c in (getattr(exp, n, None) for n in ("Union", "Except", "Intersect")) if c)
DATE_FN = (exp.TimeToStr, exp.TsOrDsToTimestamp, exp.Date, exp.CurrentTimestamp)
STR_FN = (exp.Substring, exp.StrPosition, exp.Length, exp.DPipe)
NUM_FN = (exp.Abs, exp.Round)
CLAUSE = {"expressions": "select", "from": "from", "from_": "from", "joins": "from", "where": "where",
          "having": "having", "order": "order", "group": "group"}


def load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100.0 * n / d:.1f}%)" if d else f"{n}/0"


def is_fn(node: exp.Expression) -> bool:
    """Scalar (non-aggregate) function on a column: date/string/number helpers or unknown UDFs."""
    return isinstance(node, DATE_FN + STR_FN + NUM_FN + (exp.Anonymous,))


# ---------------------------------------------------------------- scopes and nesting
def scope_nodes(sel: exp.Select):
    """Nodes of one SELECT scope, not descending into nested SELECTs / set operations."""
    stack = list(sel.iter_expressions())
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Select,) + SETOP):
            continue
        yield node
        stack.extend(node.iter_expressions())


def select_depth(sel: exp.Select) -> int:
    depth, p = 0, sel.parent
    while p is not None:
        if isinstance(p, exp.Select):
            depth += 1
        p = p.parent
    return depth


def nested_kind(sel: exp.Select) -> str:
    """Where does a SELECT sit relative to its enclosing query?"""
    child, p = sel, sel.parent
    while p is not None:
        if isinstance(p, exp.CTE):
            return "cte"
        if isinstance(p, exp.Exists):
            return "exists"
        if isinstance(p, exp.In):
            return "in_subquery"
        if isinstance(p, SETOP):
            if p.parent is None:
                return "setop_branch"
            child, p = p, p.parent
            continue
        if isinstance(p, exp.Select):
            key = child.arg_key
            if key in ("from", "from_", "joins"):
                return "from_derived"
            if key == "expressions":
                return "select_scalar"
            if key in ("where", "having"):
                return "where_scalar"
            return f"other_{key}"
        child, p = p, p.parent
    return "root"


def clause_of(node: exp.Expression) -> str:
    """Clause (select/where/having/order/group/from) of a node inside its nearest SELECT."""
    child, p = node, node.parent
    while p is not None and not isinstance(p, exp.Select):
        child, p = p, p.parent
    return CLAUSE.get(child.arg_key, child.arg_key or "?") if p is not None else "?"


def root_selects(tree: exp.Expression) -> list[exp.Select]:
    if isinstance(tree, exp.Select):
        return [tree]
    return [s for s in tree.find_all(exp.Select) if nested_kind(s) in ("root", "setop_branch")]


# ---------------------------------------------------------------- joins / foreign keys
def load_fks(path: str) -> dict[str, set[frozenset]]:
    fks: dict[str, set[frozenset]] = {}
    for db in load(path):
        tables = [t.lower() for t in db["table_names_original"]]
        cols = db["column_names_original"]
        pairs = set()
        for a, b in db.get("foreign_keys", []):
            (ta, ca), (tb, cb) = cols[a], cols[b]
            pairs.add(frozenset({(tables[ta], ca.lower()), (tables[tb], cb.lower())}))
        fks[db["db_id"]] = pairs
    return fks


def join_stats(sel: exp.Select, fk_pairs: set[frozenset]) -> dict:
    """Join kinds and whether each ON column-equality is a declared FK (one SELECT scope)."""
    tables = [t for t in scope_nodes(sel) if isinstance(t, exp.Table)]
    alias = {t.alias_or_name.lower(): t.name.lower() for t in tables}
    names = [t.name.lower() for t in tables]
    out = {"joins": 0, "kinds": Counter(), "eq": 0, "eq_fk": 0, "eq_unresolved": 0, "eq_samename": 0,
           "no_on": 0, "self_join": len(names) != len(set(names))}
    for j in sel.args.get("joins") or []:
        out["joins"] += 1
        out["kinds"][f"{j.side or ''} {j.kind or ''}".strip() or "plain"] += 1
        on = j.args.get("on")
        if on is None and not j.args.get("using"):
            out["no_on"] += 1
            continue
        for eq in (on.find_all(exp.EQ) if on is not None else []):
            l, r = eq.left, eq.right
            if not (isinstance(l, exp.Column) and isinstance(r, exp.Column)):
                continue
            out["eq"] += 1
            tl, tr = alias.get(l.table.lower()), alias.get(r.table.lower())
            if not tl or not tr:
                out["eq_unresolved"] += 1
            elif frozenset({(tl, l.name.lower()), (tr, r.name.lower())}) in fk_pairs:
                out["eq_fk"] += 1
            elif l.name.lower() == r.name.lower():
                out["eq_samename"] += 1
    return out


# ---------------------------------------------------------------- literals (value slots)
PASS_THROUGH = (exp.Paren, exp.Neg, exp.Tuple, exp.Cast)


def predicate_literals(tree: exp.Expression) -> list[exp.Literal]:
    """Literals that are direct operands of a predicate (WHERE/HAVING/ON/IIF conditions)."""
    out = []
    for lit in tree.find_all(exp.Literal):
        p = lit.parent
        while isinstance(p, PASS_THROUGH):
            p = p.parent
        if isinstance(p, PRED):
            out.append(lit)
    return out


def literal_value(lit: exp.Literal) -> str:
    v = lit.this.strip()
    if lit.is_string:
        v = v.strip("%").replace("%", " ").strip()
    elif re.fullmatch(r"-?\d+\.0+", v):
        v = v.split(".")[0]
    return v


def found(v: str, text: str, case: bool = False) -> bool:
    if not v:
        return False
    flags = 0 if case else re.IGNORECASE
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", text, flags) is not None


# ---------------------------------------------------------------- arithmetic templates
def arith_roots(tree: exp.Expression) -> list[exp.Expression]:
    roots = []
    for node in tree.find_all(*ARITH):
        p = node.parent
        while isinstance(p, (exp.Paren, exp.Cast)):
            p = p.parent
        if not isinstance(p, ARITH):
            roots.append(node)
    return roots


def cond_true_value(node: exp.Expression):
    if isinstance(node, exp.If):
        return node.args.get("true")
    ifs = node.args.get("ifs") or []
    return ifs[0].args.get("true") if ifs else None


def norm(node: exp.Expression) -> str:
    """Abstract an arithmetic expression: columns -> c, CAST/() dropped, IIF/CASE -> IND."""
    if isinstance(node, (exp.Paren, exp.Cast)):
        return norm(node.this)
    for cls, op in ((exp.Add, "+"), (exp.Sub, "-"), (exp.Mul, "*"), (exp.Div, "/")):
        if isinstance(node, cls):
            return f"({norm(node.left)} {op} {norm(node.right)})"
    if isinstance(node, exp.Literal):
        return "100" if re.fullmatch(r"100(\.0*)?", node.this) else "k"
    if isinstance(node, (exp.Column, exp.Star)):
        return "c"
    if isinstance(node, exp.Distinct):
        return "DISTINCT c"
    if isinstance(node, COND):
        tv = cond_true_value(node)
        return "IND" if isinstance(tv, exp.Literal) and tv.this in ("1", "1.0") else "IFVAL"
    if isinstance(node, exp.Count):
        inner = node.this
        if isinstance(inner, COND):
            return "COUNT(IND)"
        return "COUNT(DISTINCT c)" if isinstance(inner, exp.Distinct) else "COUNT(c)"
    if isinstance(node, (exp.Sum, exp.Avg, exp.Max, exp.Min)):
        inner = norm(node.this)
        name = type(node).__name__.upper()
        if inner == "IND" and name == "SUM":
            return "CNT_IF"
        return f"{name}({inner})"
    if isinstance(node, exp.Subquery) or isinstance(node, exp.Select):
        return "SUBQ"
    if is_fn(node):
        return "FN"
    return type(node).__name__.upper()


def arith_category(node: exp.Expression) -> str:
    ops = {type(n) for n in node.walk() if isinstance(n, ARITH)}
    pct100 = any(isinstance(n, exp.Literal) and re.fullmatch(r"100(\.0*)?", n.this) for n in node.walk())
    has_agg = any(isinstance(n, AGG) for n in node.walk())
    in_agg = any(isinstance(a, AGG) for a in iter_ancestors(node))
    if exp.Div in ops and pct100:
        cat = "percentage(ratio*100)"
    elif exp.Div in ops:
        cat = "ratio/average"
    elif ops == {exp.Sub}:
        cat = "difference"
    elif ops == {exp.Add}:
        cat = "sum"
    elif ops == {exp.Mul}:
        cat = "product"
    else:
        cat = "mixed"
    level = "agg-level" if has_agg else ("inside-agg" if in_agg else "row-level")
    return f"{cat} / {level} / in {clause_of(node)}"


def iter_ancestors(node: exp.Expression):
    p = node.parent
    while p is not None:
        yield p
        p = p.parent


# ---------------------------------------------------------------- per-query features
def in_join(node: exp.Expression) -> bool:
    return any(isinstance(a, exp.Join) for a in iter_ancestors(node))


def item_kind(node: exp.Expression) -> str:
    while isinstance(node, (exp.Alias, exp.Paren, exp.Cast)):
        node = node.this
    if isinstance(node, exp.Column):
        return "col"
    if isinstance(node, exp.Star):
        return "star"
    if isinstance(node, ARITH):
        return "arith"
    if isinstance(node, COND):
        return "iif/case"
    if isinstance(node, AGG):
        inner = node.this
        if isinstance(inner, exp.Distinct):
            return f"{type(node).__name__.upper()}(DISTINCT col)"
        if isinstance(inner, COND) or any(isinstance(n, COND + ARITH) for n in inner.walk()):
            return f"{type(node).__name__.upper()}(expr)"
        return f"{type(node).__name__.upper()}(col)"
    if isinstance(node, (exp.Subquery, exp.Select)):
        return "scalar-subquery"
    if is_fn(node) or isinstance(node, exp.Distinct):
        return "fn(col)" if not isinstance(node, exp.Distinct) else "distinct"
    if isinstance(node, exp.Window) or isinstance(node, exp.Literal):
        return type(node).__name__.lower()
    return type(node).__name__.lower()


def features(tree: exp.Expression, item: dict, fk_pairs: set[frozenset]) -> dict:
    roots = root_selects(tree)
    first = roots[0]
    nested = [s for s in tree.find_all(exp.Select) if nested_kind(s) not in ("root", "setop_branch")]
    f: dict = {"qid": item["question_id"], "db": item["db_id"], "diff": item["difficulty"]}
    f["setop"] = type(tree).__name__.upper() if isinstance(tree, SETOP) else ""
    f["cte"] = tree.find(exp.CTE) is not None
    f["window"] = tree.find(exp.Window) is not None
    f["nested_kinds"] = sorted(nested_kind(s) for s in nested if nested_kind(s) != "cte")
    f["depth"] = max(select_depth(s) for s in tree.find_all(exp.Select))
    f["items"] = [item_kind(e) for e in first.expressions]
    sel_nodes = [n for e in first.expressions for n in scope_walk(e)]
    f["sel_agg"] = any(isinstance(n, AGG) for n in sel_nodes)
    f["sel_expr"] = any(isinstance(n, ARITH + COND) or is_fn(n) for n in sel_nodes)
    f["distinct"] = any(r.args.get("distinct") is not None for r in roots)
    for key in ("where", "group", "having", "order", "limit"):
        f[key] = any(r.args.get(key) is not None for r in roots)
    lim = first.args.get("limit")
    lim_val = lim.expression if lim is not None and lim.expression is not None else None
    f["limit1"] = isinstance(lim_val, exp.Literal) and lim_val.this == "1"
    f["offset"] = tree.find(exp.Offset) is not None
    f["group_keys"] = len(first.args["group"].expressions) if first.args.get("group") else 0
    f["order_keys"] = len(first.args["order"].expressions) if first.args.get("order") else 0
    f["aggs"] = Counter(type(n).__name__.upper() for n in tree.find_all(*AGG))
    f["count_distinct"] = any(isinstance(n.this, exp.Distinct) for n in tree.find_all(exp.Count))
    f["cond"] = sum(1 for _ in tree.find_all(*COND))
    f["cond_agg"] = sum(1 for n in tree.find_all(*AGG) if isinstance(n.this, COND))
    f["cast"] = tree.find(exp.Cast) is not None
    f["date_fn"] = tree.find(*DATE_FN) is not None or any(
        n.name.upper() == "JULIANDAY" for n in tree.find_all(exp.Anonymous))
    f["str_fn"] = tree.find(*STR_FN) is not None
    f["num_fn"] = tree.find(*NUM_FN) is not None
    f["anon_fns"] = sorted({n.name.upper() for n in tree.find_all(exp.Anonymous)})
    preds = [p for p in tree.find_all(*PRED) if not in_join(p)]
    f["pred_atoms"] = len(preds)
    f["pred_ops"] = Counter(("NOT " if isinstance(p.parent, exp.Not) else "") + type(p).__name__.upper()
                            for p in preds)
    f["or"] = any(not in_join(o) for o in tree.find_all(exp.Or))
    f["not"] = tree.find(exp.Not) is not None
    roots_ar = arith_roots(tree)
    f["arith_n"] = len(roots_ar)
    f["arith_norm"] = [norm(a) for a in roots_ar]
    f["arith_cat"] = [arith_category(a) for a in roots_ar]
    f["pct100"] = any("100" in s for s in f["arith_norm"])
    f["tables"] = len({t.name.lower() for t in tree.find_all(exp.Table)})
    alias = {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)}
    f["columns"] = len({(alias.get(c.table.lower(), c.table.lower()), c.name.lower())
                        for c in tree.find_all(exp.Column)})
    js = [join_stats(s, fk_pairs) for s in tree.find_all(exp.Select)]
    f["joins"] = sum(j["joins"] for j in js)
    f["join_kinds"] = sum((j["kinds"] for j in js), Counter())
    for key in ("eq", "eq_fk", "eq_unresolved", "eq_samename", "no_on"):
        f["join_" + key] = sum(j[key] for j in js)
    f["self_join"] = any(j["self_join"] for j in js)
    f["literals"] = [(lit.is_string, literal_value(lit)) for lit in predicate_literals(tree)
                     if not in_join(lit)]
    f["ast_nodes"] = sum(1 for n in tree.walk() if not isinstance(
        n, (exp.Identifier, exp.TableAlias, exp.DataType, exp.Paren, exp.Alias)))
    f["ast_depth"] = max(node_depth(n) for n in tree.walk())
    return f


def node_depth(node: exp.Expression) -> int:
    return sum(1 for _ in iter_ancestors(node))


def scope_walk(node: exp.Expression):
    """Walk a sub-expression without entering nested SELECTs."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (exp.Select, exp.Subquery) + SETOP):
            continue
        yield n
        stack.extend(n.iter_expressions())


# ---------------------------------------------------------------- skeleton signatures
def sig_l0(f: dict) -> str:
    """Clause-level skeleton: which clauses exist and what the projection is (no expressions)."""
    parts = []
    if f["setop"]:
        parts.append(f["setop"])
    if f["cte"]:
        parts.append("WITH")
    parts.append("SELECT" + (" DISTINCT" if f["distinct"] else "") + (" AGG" if f["sel_agg"] else " COL"))
    for key, tag in (("where", "WHERE"), ("group", "GROUP"), ("having", "HAVING"), ("order", "ORDER")):
        if f[key]:
            parts.append(tag)
    if f["limit"]:
        parts.append("LIMIT1" if f["limit1"] else "LIMITk")
    parts.extend(f"SUB:{k}" for k in f["nested_kinds"])
    if f["window"]:
        parts.append("WINDOW")
    return " ".join(parts)


def sig_l1(f: dict) -> str:
    """L0 + expression markers (arithmetic / %·100 / conditional aggregation / scalar functions)."""
    marks = []
    if f["arith_n"]:
        marks.append("ARITH%" if f["pct100"] else "ARITH")
    if f["cond_agg"]:
        marks.append("CONDAGG")
    elif f["cond"]:
        marks.append("COND")
    if f["date_fn"]:
        marks.append("DATEFN")
    if f["str_fn"]:
        marks.append("STRFN")
    if f["num_fn"]:
        marks.append("NUMFN")
    return sig_l0(f) + (" | " + "+".join(marks) if marks else "")


ATOM = r"_ (?:=|<>|!=|>|<|>=|<=|LIKE|IS(?: NOT)?) (?:_|NULL)"


def sig_l2(tree: exp.Expression, collapse: bool) -> str:
    """Anonymized SQL (RESDSQL-style): schema items/values -> _, CAST/aliases/plain joins removed."""
    t = tree.copy()
    for a in list(t.find_all(exp.Alias)):
        a.replace(a.this)
    for c in list(t.find_all(exp.Cast)):
        c.replace(c.this)
    for j in list(t.find_all(exp.Join)):
        if isinstance(j.this, exp.Table):
            j.pop()
    for node in list(t.find_all(exp.Table, exp.Subquery)):
        node.set("alias", None)
    for node in list(t.find_all(exp.Column, exp.Literal, exp.Table)):
        node.replace(exp.var("_"))
    s = re.sub(r"\s+", " ", t.sql(dialect="sqlite"))
    if collapse:
        s = re.sub(r"_(?:, _)+", "_", s)
        s = re.sub(rf"(\(?{ATOM}\)?)(?: (?:AND|OR) \(?{ATOM}\)?)+", r"\1 ...", s)
    return s


def tier(f: dict) -> str:
    """Grammar tiers for a slot-filling compiler (cumulative coverage is reported)."""
    if f["setop"] or f["cte"] or f["window"] or f["depth"] >= 2 or len(f["nested_kinds"]) >= 2:
        return "D"
    if f["nested_kinds"]:
        return "C"
    if f["arith_n"] or f["cond"] or f["date_fn"] or f["str_fn"] or f["num_fn"] or f["anon_fns"]:
        return "B"
    return "A"


def family(f: dict) -> str:
    if f["setop"] or f["cte"] or f["window"]:
        return "F8 set-op / CTE / window"
    if f["nested_kinds"]:
        return "F7 nested subquery"
    if f["arith_n"] and f["sel_agg"]:
        return "F6 aggregate arithmetic (ratio/%/diff)"
    if f["arith_n"]:
        return "F5 row-level arithmetic"
    if f["group"] and f["order"] and f["limit"]:
        return "F4a group + argmax/top-k"
    if f["group"]:
        return "F4b group aggregate"
    if f["order"] and f["limit"]:
        return "F3 argmax/top-k (no group)"
    if f["sel_agg"]:
        return "F2 scalar aggregate"
    return "F1 lookup / projection"


def decisions(f: dict) -> int:
    """Content decisions a slot-filling decoder must get right (global clause flags excluded)."""
    n = 2 * len(f["items"])                      # column + aggregation/function per SELECT item
    n += 3 * f["pred_atoms"]                     # column + operator + value per predicate atom
    n += max(0, f["pred_atoms"] - 1)             # AND/OR connector between atoms
    n += f["group_keys"]
    n += 2 * f["order_keys"]                     # key + direction
    n += 1 if f["limit"] else 0
    n += f["arith_n"] + f["cond"]                # arithmetic template / IIF-CASE shape
    n += len(f["nested_kinds"])                  # subquery placement
    return n


GLOBAL_FLAGS = 9  # family + DISTINCT/WHERE/GROUP/HAVING/ORDER/LIMIT/nested/set-op flags


# ---------------------------------------------------------------- report helpers
FORMULA_FN = re.compile(r"\b(DIVIDE|SUBTRACT|MULTIPLY|ADD|SUM|COUNT|AVG|MAX|MIN)\s*\(", re.I)
FORMULA_OP = re.compile(r"\S\s+[/*+]\s+\S|\)\s*[/*-]\s*\(?")
FORMULA_WORD = re.compile(r"\b(percent\w*|ratio|rate|proportion|divided|difference|average)\b", re.I)
MAPPING = re.compile(r"refers? to|means|stands for|indicates|is (?:the )?\w+ =", re.I)


def coverage(counter: Counter, total: int, ks=(1, 5, 10, 20, 50)) -> str:
    vals = [c for _, c in counter.most_common()]
    out = [f"distinct={len(vals)}", f"singletons={sum(1 for v in vals if v == 1)}"]
    out += [f"top{k}={100.0 * sum(vals[:k]) / total:.1f}%" for k in ks]
    return ", ".join(out)


def by_diff(rows: list[dict], key) -> str:
    parts = []
    for d in DIFFS:
        sub = [r for r in rows if r["diff"] == d]
        parts.append(f"{d[:3]}={pct(sum(1 for r in sub if key(r)), len(sub))}")
    return f"all={pct(sum(1 for r in rows if key(r)), len(rows))}; " + "; ".join(parts)


def dist(vals) -> str:
    vals = sorted(vals)
    if not vals:
        return "n=0"
    p90 = vals[min(len(vals) - 1, int(0.9 * len(vals)))]
    return f"mean={statistics.mean(vals):.1f} median={statistics.median(vals):g} p90={p90} max={vals[-1]}"


def ground(v: str, q: str, e: str) -> str:
    if found(v, q, case=True):
        return "question(exact case)"
    if found(v, q):
        return "question(case differs)"
    if found(v, e, case=True):
        return "evidence only(exact case)"
    if found(v, e):
        return "evidence only(case differs)"
    return "not in question/evidence"


def top(counter: Counter, k: int, total: int, indent: str = "    ") -> None:
    for key, c in counter.most_common(k):
        print(f"{indent}{c:4d} ({100.0 * c / total:4.1f}%)  {key}")


# ---------------------------------------------------------------- main report
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--tables", default=TABLES)
    ap.add_argument("--train-json", default=None, help="optional BIRD train.json for skeleton overlap")
    ap.add_argument("--examples", action="store_true", help="print example question ids per tier")
    ap.add_argument("--keep-duplicates", action="store_true",
                    help="keep repeated question_ids (mini-dev repeats 137 and 138); default drops them")
    args = ap.parse_args()

    items, fks = load(args.data), load_fks(args.tables)
    if not args.keep_duplicates:
        seen_ids, uniq = set(), []
        for it in items:
            if it["question_id"] not in seen_ids:
                seen_ids.add(it["question_id"])
                uniq.append(it)
        print(f"dropped {len(items) - len(uniq)} rows with repeated question_id")
        items = uniq
    rows, trees, fails = [], {}, []
    for it in items:
        try:
            tree = sqlglot.parse_one(it["SQL"], read="sqlite")
        except Exception as err:  # noqa: BLE001 - report and continue
            fails.append((it["question_id"], str(err)[:80]))
            continue
        f = features(tree, it, fks.get(it["db_id"], set()))
        f.update(question=it["question"], evidence=it.get("evidence", ""))
        f["L0"], f["L1"] = sig_l0(f), sig_l1(f)
        f["L2"], f["L2c"] = sig_l2(tree, False), sig_l2(tree, True)
        f["tier"], f["family"], f["dec"] = tier(f), family(f), decisions(f)
        rows.append(f)
        trees[f["qid"]] = tree
    n = len(rows)
    print(f"# BIRD mini-dev gold SQL skeleton report  (sqlglot {sqlglot.__version__})")
    print(f"parsed {n}/{len(items)}; failures={fails}")
    print("difficulty:", dict(Counter(r["diff"] for r in rows)))

    print("\n## 1. Clause / construct frequency (all; simple; moderate; challenging)")
    checks = [
        ("WHERE", lambda r: r["where"]), ("JOIN (>=1)", lambda r: r["joins"] > 0),
        ("tables>=3", lambda r: r["tables"] >= 3), ("GROUP BY", lambda r: r["group"]),
        ("HAVING", lambda r: r["having"]), ("ORDER BY", lambda r: r["order"]),
        ("LIMIT", lambda r: r["limit"]), ("ORDER BY + LIMIT 1", lambda r: r["order"] and r["limit1"]),
        ("DISTINCT (select)", lambda r: r["distinct"]), ("COUNT(DISTINCT)", lambda r: r["count_distinct"]),
        ("aggregate anywhere", lambda r: sum(r["aggs"].values()) > 0),
        ("arithmetic (+-*/)", lambda r: r["arith_n"] > 0), ("ratio*100 (percentage)", lambda r: r["pct100"]),
        ("IIF/CASE", lambda r: r["cond"] > 0), ("conditional aggregation", lambda r: r["cond_agg"] > 0),
        ("CAST", lambda r: r["cast"]), ("date function", lambda r: r["date_fn"]),
        ("string function", lambda r: r["str_fn"]), ("OR in predicates", lambda r: r["or"]),
        ("NOT", lambda r: r["not"]), ("nested subquery", lambda r: bool(r["nested_kinds"])),
        ("set operation", lambda r: bool(r["setop"])), ("CTE", lambda r: r["cte"]),
        ("window function", lambda r: r["window"]), ("self-join", lambda r: r["self_join"]),
        ("LEFT JOIN", lambda r: any("LEFT" in k for k in r["join_kinds"])),
    ]
    for name, key in checks:
        print(f"  {name:26s} {by_diff(rows, key)}")
    print("  nested subquery kinds:", dict(Counter(k for r in rows for k in r["nested_kinds"])))
    print("  anonymous functions:", dict(Counter(a for r in rows for a in r["anon_fns"])))
    print("  join kinds:", dict(sum((r["join_kinds"] for r in rows), Counter())))

    print("\n## 2. Grammar tiers (A flat SPJA+GROUP/ORDER/LIMIT, B +expressions, C +one subquery, D rest)")
    cum = 0
    for t in "ABCD":
        cnt = sum(1 for r in rows if r["tier"] == t)
        cum += cnt
        print(f"  tier {t}: {pct(cnt, n)}  cumulative={100.0 * cum / n:.1f}%  "
              f"| {by_diff(rows, lambda r, t=t: r['tier'] <= t)}")
        if args.examples:
            print("     e.g.", [r["qid"] for r in rows if r["tier"] == t][:12])

    print("\n## 3. Query families (one label per query, precedence F8>F7>...>F1)")
    fam = Counter(r["family"] for r in rows)
    for key, c in sorted(fam.items()):
        sub = Counter(r["diff"] for r in rows if r["family"] == key)
        print(f"  {key:42s} {pct(c, n):>14s}  sim={sub['simple']} mod={sub['moderate']} cha={sub['challenging']}")

    print("\n## 4. Skeleton coverage (top-k = share of queries covered by the k most frequent skeletons)")
    for lvl, desc in (("L0", "clause-level"), ("L1", "clause + expression markers"),
                      ("L2c", "anonymized SQL, lists/AND-runs collapsed"), ("L2", "anonymized SQL, strict")):
        cnt = Counter(r[lvl] for r in rows)
        print(f"  {lvl:4s} ({desc}): {coverage(cnt, n)}")
    for lvl, k in (("L0", 15), ("L1", 12), ("L2c", 12)):
        print(f"  -- top {lvl}:")
        top(Counter(r[lvl] for r in rows), k, n)
    for d in DIFFS:
        sub = [r for r in rows if r["diff"] == d]
        print(f"  {d:12s} L0: {coverage(Counter(r['L0'] for r in sub), len(sub), (5, 10))} | "
              f"L1: {coverage(Counter(r['L1'] for r in sub), len(sub), (5, 10))}")

    print("\n## 5. Decision counts (content decisions; +%d global clause flags not included)" % GLOBAL_FLAGS)
    for d in ("all",) + DIFFS:
        sub = rows if d == "all" else [r for r in rows if r["diff"] == d]
        print(f"  {d:12s} decisions {dist([r['dec'] for r in sub])} | select items {dist([len(r['items']) for r in sub])}"
              f" | predicate atoms {dist([r['pred_atoms'] for r in sub])}")
        print(f"  {'':12s} AST nodes {dist([r['ast_nodes'] for r in sub])} | AST depth {dist([r['ast_depth'] for r in sub])}"
              f" | tables {dist([r['tables'] for r in sub])} | columns {dist([r['columns'] for r in sub])}")
    for p in (0.95, 0.98, 0.99):
        est = statistics.mean(p ** (r["dec"] + GLOBAL_FLAGS) for r in rows)
        print(f"  independent per-decision accuracy {p}: expected all-correct rate = {100 * est:.1f}%")
    print("  select item kinds:", dict(Counter(k for r in rows for k in r["items"]).most_common(14)))
    print("  predicate operators:", dict(sum((r["pred_ops"] for r in rows), Counter()).most_common(14)))

    print("\n## 6. Arithmetic templates (per arithmetic root expression)")
    norms = Counter(s for r in rows for s in r["arith_norm"])
    cats = Counter(s for r in rows for s in r["arith_cat"])
    total_roots = sum(norms.values())
    print(f"  queries with arithmetic: {pct(sum(1 for r in rows if r['arith_n']), n)}; roots={total_roots}; "
          f"{coverage(norms, total_roots, (1, 3, 5, 10, 20))}")
    top(norms, 14, total_roots)
    print("  -- categories:")
    top(cats, 14, total_roots)
    top10 = {s for s, _ in norms.most_common(10)}
    b_core = [r for r in rows if r["tier"] <= "B" and all(s in top10 for s in r["arith_norm"])]
    print(f"  tier A+B queries whose arithmetic uses only the top-10 templates: {by_diff(rows, lambda r: r in b_core)}")

    print("\n## 7. Evidence formulas vs gold arithmetic")
    for r in rows:
        ev = r["evidence"]
        r["ev_fn"] = bool(FORMULA_FN.search(ev))
        r["ev_formula"] = r["ev_fn"] or bool(FORMULA_OP.search(ev)) or bool(FORMULA_WORD.search(ev))
        r["ev_mapping"] = bool(MAPPING.search(ev))
    ar = [r for r in rows if r["arith_n"]]
    print(f"  evidence non-empty: {by_diff(rows, lambda r: bool(r['evidence'].strip()))}")
    print(f"  evidence has formula-like text: {by_diff(rows, lambda r: r['ev_formula'])}")
    print(f"  evidence has DIVIDE/SUM/COUNT(...)-style call: {by_diff(rows, lambda r: r['ev_fn'])}")
    print(f"  evidence has value mapping ('refers to' / '='): {by_diff(rows, lambda r: r['ev_mapping'])}")
    print(f"  P(evidence formula | gold arithmetic) = {pct(sum(1 for r in ar if r['ev_formula']), len(ar))}")
    na = [r for r in rows if r["ev_formula"]]
    print(f"  P(gold arithmetic | evidence formula) = {pct(sum(1 for r in na if r['arith_n']), len(na))}")

    print("\n## 8. Literal (value-slot) grounding, predicate literals outside JOIN ON")
    lit_rows = [(r, s, v) for r in rows for s, v in r["literals"]]
    for is_str in (True, False):
        sub = [(r, v) for r, s, v in lit_rows if s == is_str]
        c = Counter(ground(v, r["question"], r["evidence"]) for r, v in sub)
        print(f"  {'string' if is_str else 'number'} literals n={len(sub)}: "
              + ", ".join(f"{k}={pct(x, len(sub))}" for k, x in c.most_common()))
    qs = [r for r in rows if r["literals"]]
    allq = sum(1 for r in qs if all(ground(v, r["question"], r["evidence"]).startswith("question")
                                   for s, v in r["literals"]))
    print(f"  queries with literals: {pct(len(qs), n)}; all literals found verbatim in question: {pct(allq, len(qs))}")

    print("\n## 9. Joins vs declared foreign keys (dev_tables.json)")
    eq = sum(r["join_eq"] for r in rows)
    print(f"  queries with joins: {pct(sum(1 for r in rows if r['joins']), n)}; join ON column-equalities={eq}; "
          f"declared-FK={pct(sum(r['join_eq_fk'] for r in rows), eq)}; "
          f"unresolved alias={sum(r['join_eq_unresolved'] for r in rows)}; joins without ON={sum(r['join_no_on'] for r in rows)}")
    jq = [r for r in rows if r["join_eq"]]
    print(f"  queries whose every ON-equality is a declared FK: {pct(sum(1 for r in jq if r['join_eq_fk'] == r['join_eq']), len(jq))}")
    print(f"  non-FK equalities with identical column names (FK missing from metadata?): "
          f"{sum(r['join_eq_samename'] for r in rows)}; FK-or-same-name per query: "
          f"{pct(sum(1 for r in jq if r['join_eq_fk'] + r['join_eq_samename'] == r['join_eq']), len(jq))}")
    print(f"  queries with self-join: {pct(sum(1 for r in rows if r['self_join']), n)}")

    if args.train_json:
        train_overlap(args.train_json, rows)


def train_overlap(path: str, rows: list[dict]) -> None:
    """Share of mini-dev queries whose skeleton already occurs in BIRD train (template-bank upper bound)."""
    train = load(path)
    seen: dict[str, Counter] = defaultdict(Counter)
    ok = 0
    for it in train:
        sql = it.get("SQL") or it.get("sql") or it.get("query")
        try:
            tree = sqlglot.parse_one(sql, read="sqlite")
            f = features(tree, {"question_id": -1, "db_id": it.get("db_id", ""),
                                "difficulty": "train"}, set())
        except Exception:  # noqa: BLE001 - skip unparsable train SQL
            continue
        ok += 1
        seen["L0"][sig_l0(f)] += 1
        seen["L1"][sig_l1(f)] += 1
        seen["L2c"][sig_l2(tree, True)] += 1
        seen["L2"][sig_l2(tree, False)] += 1
    print(f"\n## 10. Skeleton overlap with train ({ok}/{len(train)} train SQL parsed)")
    for lvl in ("L0", "L1", "L2c", "L2"):
        print(f"  {lvl:4s}: train distinct={len(seen[lvl])}; mini-dev skeleton seen in train: "
              f"{by_diff(rows, lambda r, lvl=lvl: r[lvl] in seen[lvl])}; "
              f"seen >=5 times: {pct(sum(1 for r in rows if seen[lvl][r[lvl]] >= 5), len(rows))}")
        ranked = [s for s, _ in seen[lvl].most_common()]
        cov = []
        for k in (10, 20, 30, 50, 100, 255):
            top_k = set(ranked[:k])
            cov.append(f"K={k}:{100.0 * sum(1 for r in rows if r[lvl] in top_k) / len(rows):.1f}%")
        print(f"        mini-dev covered by train top-K {lvl} classes: " + " ".join(cov))


if __name__ == "__main__":
    main()
