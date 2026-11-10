#!/usr/bin/env python3
"""Layered queries over a cir-graph Kùzu database.

Examples:
  cir-graph-query summary
  cir-graph-query functions                      # macro: functions with flags
  cir-graph-query calls [--cross-tu]             # macro: call edges
  cir-graph-query cycles | dead | entry          # macro: recursion / dead code / entries
  cir-graph-query cfg choose                     # meso: blocks and CFG edges of a function
  cir-graph-query ops choose                     # micro: operations in block order
  cir-graph-query flows choose [--kind store_to_load]
  cir-graph-query callsites choose               # bridges: argument/return bindings
  cir-graph-query trace main --var value         # values carrying a source variable
  cir-graph-query cypher "MATCH (f:Function) RETURN f.demangledName"

Function arguments match the demangled name, mangled symbol, or ID (substring).
Use --program to select one program when the database holds several.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_DB = Path("artifacts") / "graph.kuzu"

FN_MATCH = "f.id IN $fids"
PROGRAM = "($program = '' OR f.programId = $program)"


def _import_kuzu():
    try:
        import kuzu
        return kuzu
    except ImportError:
        sys.exit("error: the 'kuzu' Python package is required (pip install kuzu)")


QUERIES = {
    "summary": [
        ("Programs", "MATCH (p:Program) WHERE ($program = '' OR p.id = $program) "
                     "OPTIONAL MATCH (p)-[:CONTAINS_TU]->(tu:TranslationUnit) "
                     "RETURN p.id AS program, count(tu) AS tus"),
        ("Functions", "MATCH (f:Function) WHERE " + PROGRAM + " RETURN count(f) AS functions"),
        ("External functions", "MATCH (f:Function) WHERE " + PROGRAM +
         " AND f.isDeclaration RETURN count(f) AS external"),
        ("Blocks", "MATCH (b:BasicBlock) WHERE ($program = '' OR b.programId = $program) "
                   "RETURN count(b) AS blocks"),
        ("Call edges", "MATCH (f:Function)-[c:CALLS]->(:Function) WHERE " + PROGRAM +
         " RETURN count(c) AS calls"),
        ("Flow kinds",
         "MATCH (a:Value)-[r:FLOWS_TO]->(:Value) WHERE ($program = '' OR a.programId = $program) "
         "RETURN r.kind AS kind, count(*) AS edges ORDER BY kind"),
    ],
    "functions": [
        ("Functions",
         "MATCH (f:Function) WHERE " + PROGRAM + " "
         "RETURN f.demangledName AS function, f.tuId AS tu, f.linkage AS linkage, "
         "f.isEntryPoint AS entry, f.isRecursive AS recursive, f.isDeadCode AS dead, "
         "f.inDegree AS callers, f.outDegree AS callees, f.blockCount AS blocks "
         "ORDER BY tu, function"),
    ],
    "calls": [
        ("Call edges",
         "MATCH (f:Function)-[c:CALLS]->(g:Function) WHERE " + PROGRAM + " "
         "AND ($crosstu = false OR c.isCrossTU) "
         "RETURN f.demangledName AS caller, f.tuId AS callerTU, g.demangledName AS callee, "
         "g.tuId AS calleeTU, c.isCrossTU AS crossTU, c.isDirect AS direct, "
         "c.callSiteLine AS line ORDER BY callerTU, caller, line"),
    ],
    "cycles": [
        ("Recursive functions",
         "MATCH (f:Function) WHERE " + PROGRAM + " AND f.isRecursive "
         "OPTIONAL MATCH (f)-[:CALLS]->(g:Function) WHERE g.isRecursive "
         "RETURN f.demangledName AS function, collect(g.demangledName) AS recursiveCallees"),
    ],
    "dead": [
        ("Dead code",
         "MATCH (f:Function) WHERE " + PROGRAM + " AND f.isDeadCode "
         "RETURN f.demangledName AS function, f.tuId AS tu, f.startLine AS line"),
    ],
    "entry": [
        ("Entry points",
         "MATCH (f:Function) WHERE " + PROGRAM + " AND f.isEntryPoint "
         "RETURN f.demangledName AS function, f.tuId AS tu, f.outDegree AS callees"),
    ],
    "cfg": [
        ("Blocks",
         "MATCH (f:Function)-[:HAS_BLOCK]->(b:BasicBlock) WHERE " + FN_MATCH + " AND " + PROGRAM + " "
         "RETURN f.demangledName + ' [' + f.tuId + ']' AS function, b.ordinal AS ordinal, b.name AS block, "
         "b.isEntry AS entry, b.isExit AS exit, b.terminatorOp AS terminator, "
         "b.operationCount AS ops ORDER BY function, ordinal"),
        ("CFG edges",
         "MATCH (f:Function)-[:HAS_BLOCK]->(a:BasicBlock)-[e:CFG_NEXT]->(b:BasicBlock) "
         "WHERE " + FN_MATCH + " AND " + PROGRAM + " "
         "RETURN f.demangledName + ' [' + f.tuId + ']' AS function, a.ordinal AS src, a.name AS fromBlock, e.kind AS kind, "
         "e.label AS label, b.ordinal AS dst, b.name AS toBlock, e.isBackEdge AS backEdge "
         "ORDER BY function, src, e.successorIndex"),
    ],
    "ops": [
        ("Operations",
         "MATCH (f:Function)-[:HAS_BLOCK]->(b:BasicBlock)-[:CONTAINS_OPERATION]->(o:Operation) "
         "WHERE " + FN_MATCH + " AND " + PROGRAM + " "
         "RETURN f.demangledName + ' [' + f.tuId + ']' AS function, b.ordinal AS bb, o.sequenceIndex AS seq, "
         "o.line AS line, o.text AS text ORDER BY function, seq"),
    ],
    "flows": [
        ("Data flow",
         "MATCH (a:Value)-[r:FLOWS_TO]->(b:Value), (f:Function) "
         "WHERE (a.functionId = f.id OR b.functionId = f.id) AND " + FN_MATCH + " AND " + PROGRAM + " "
         "AND ($kind = '' OR r.kind = $kind) "
         "RETURN r.kind AS kind, a.id AS source, coalesce(a.varName, a.constValue) AS srcVar, "
         "b.id AS target, b.varName AS dstVar, r.isInterprocedural AS inter "
         "ORDER BY kind, source"),
    ],
    "callsites": [
        ("Call sites",
         "MATCH (f:Function)-[:HAS_BLOCK]->(:BasicBlock)-[:CONTAINS_OPERATION]->(o:Operation)"
         "-[i:INVOKES]->(g:Function) WHERE (" + FN_MATCH + " OR g.id IN $fids) "
         "AND " + PROGRAM + " "
         "RETURN f.demangledName AS caller, o.line AS line, g.demangledName AS callee, "
         "i.isCrossTU AS crossTU, o.id AS op ORDER BY caller, line"),
        ("Argument -> parameter / return -> result bindings",
         "MATCH (a:Value)-[r:FLOWS_TO]->(b:Value), (f:Function) "
         "WHERE r.kind IN ['call_argument', 'call_return'] "
         "AND (a.functionId = f.id OR b.functionId = f.id) AND " + FN_MATCH + " AND " + PROGRAM + " "
         "RETURN r.kind AS kind, r.argIndex AS idx, a.id AS source, b.id AS target, "
         "r.targetOpId AS callOp ORDER BY callOp, kind, idx"),
    ],
    "trace": [
        ("Values carrying a variable",
         "MATCH (f:Function)-[:HAS_BLOCK]->(:BasicBlock)-[:CONTAINS_OPERATION]->(o:Operation)"
         "-[:DEFINES_VALUE]->(v:Value) WHERE " + FN_MATCH + " AND " + PROGRAM + " "
         "AND v.varName = $var AND o.name <> 'cir.alloca' "
         "OPTIONAL MATCH (src:Value)-[r:FLOWS_TO]->(v) "
         "RETURN o.sequenceIndex AS seq, o.name AS op, v.id AS value, "
         "collect(r.kind + ' <- ' + coalesce(src.varName, src.constValue, src.id)) AS incoming "
         "ORDER BY seq"),
    ],
}

NEEDS_FUNCTION = {"cfg", "ops", "flows", "callsites", "trace"}


def print_table(title: str, columns, rows) -> None:
    print(f"\n== {title} ({len(rows)} row{'s' if len(rows) != 1 else ''})")
    if not rows:
        return
    cells = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [min(60, max(len(c), *(len(r[i]) for r in cells))) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join((v if len(v) <= w else v[:w - 1] + "…").ljust(w) for v, w in zip(row, widths)))


def resolve_functions(conn, target: str, program: str) -> list:
    """Function IDs for `target`: exact name/ID, then `name(`, then substring."""
    scope = "($program = '' OR f.programId = $program)"
    attempts = [
        ("f.demangledName = $t OR f.name = $t OR f.id = $t", target),
        ("f.demangledName STARTS WITH $t", target + "("),
        ("f.demangledName CONTAINS $t OR f.id ENDS WITH $t", target),
    ]
    for condition, value in attempts:
        result = conn.execute(f"MATCH (f:Function) WHERE ({condition}) AND {scope} RETURN f.id",
                              {"t": value, "program": program})
        ids = []
        while result.has_next():
            ids.append(result.get_next()[0])
        if ids:
            return ids
    return []


def run(conn, title: str, query: str, params: dict) -> None:
    used = {k: v for k, v in params.items() if f"${k}" in query}
    result = conn.execute(query, used)
    columns = result.get_column_names()
    rows = []
    while result.has_next():
        rows.append(result.get_next())
    print_table(title, columns, rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cir-graph-query", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted([*QUERIES, "cypher"]))
    parser.add_argument("target", nargs="?", default="",
                        help="function name for meso/micro commands, or Cypher text")
    parser.add_argument("--database", "-d", type=Path, default=DEFAULT_DB,
                        help=f"Kùzu database (default: {DEFAULT_DB})")
    parser.add_argument("--program", default="", help="restrict to one program ID")
    parser.add_argument("--kind", default="", help="FLOWS_TO kind filter for 'flows'")
    parser.add_argument("--var", default="", help="source variable name for 'trace'")
    parser.add_argument("--cross-tu", action="store_true", help="only cross-TU calls for 'calls'")
    args = parser.parse_args(argv)

    if args.command in NEEDS_FUNCTION and not args.target:
        parser.error(f"'{args.command}' needs a function name")
    if args.command == "trace" and not args.var:
        parser.error("'trace' needs --var NAME")
    if not args.database.exists():
        parser.error(f"database not found: {args.database}")

    kuzu = _import_kuzu()
    db = kuzu.Database(str(args.database), read_only=True)
    conn = kuzu.Connection(db)
    if args.command == "cypher":
        run(conn, "Cypher", args.target, {})
        return 0
    fids = resolve_functions(conn, args.target, args.program) if args.command in NEEDS_FUNCTION else []
    if args.command in NEEDS_FUNCTION and not fids:
        print(f"No function matches {args.target!r}.", file=sys.stderr)
        return 1
    params = {"fids": fids, "program": args.program, "kind": args.kind,
              "var": args.var, "crosstu": args.cross_tu}
    for title, query in QUERIES[args.command]:
        run(conn, title, query, params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
