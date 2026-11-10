"""Kùzu schema for the layered CIR graph and deterministic Cypher generation.

Every node table carries `programId`, so an import first detaches and deletes
exactly the nodes of the program being re-imported and then recreates them.
Re-running an import is therefore idempotent, and different programs can live
side by side in one database.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Program

Column = Tuple[str, str]

NODE_TABLES: Dict[str, List[Column]] = {
    "Program": [
        ("id", "STRING"), ("name", "STRING"), ("programId", "STRING"),
        ("schemaVersion", "STRING"),
    ],
    "TranslationUnit": [
        ("id", "STRING"), ("compilationId", "STRING"), ("sourceFile", "STRING"),
        ("cirFile", "STRING"), ("contentHash", "STRING"), ("programId", "STRING"),
        ("typeAliases", "STRING"), ("attrAliases", "STRING"),
    ],
    "FunctionSymbol": [
        ("id", "STRING"), ("name", "STRING"), ("demangledName", "STRING"),
        ("linkage", "STRING"), ("isStatic", "BOOLEAN"), ("tuId", "STRING"),
        ("programId", "STRING"),
    ],
    "Function": [
        ("id", "STRING"), ("name", "STRING"), ("demangledName", "STRING"),
        ("sourceFile", "STRING"), ("tuId", "STRING"), ("isDeclaration", "BOOLEAN"),
        ("isStatic", "BOOLEAN"), ("isEntryPoint", "BOOLEAN"), ("isRecursive", "BOOLEAN"),
        ("isDeadCode", "BOOLEAN"), ("isAddressTaken", "BOOLEAN"), ("startLine", "INT64"),
        ("endLine", "INT64"), ("location", "STRING"), ("linkage", "STRING"),
        ("signature", "STRING"), ("returnType", "STRING"), ("paramTypes", "STRING"),
        ("inDegree", "INT64"), ("outDegree", "INT64"), ("blockCount", "INT64"),
        ("programId", "STRING"),
    ],
    "GlobalVariable": [
        ("id", "STRING"), ("name", "STRING"), ("demangledName", "STRING"),
        ("linkage", "STRING"), ("isStatic", "BOOLEAN"), ("isDeclaration", "BOOLEAN"),
        ("isConstant", "BOOLEAN"), ("type", "STRING"), ("tuId", "STRING"),
        ("sourceFile", "STRING"), ("line", "INT64"), ("initializer", "STRING"),
        ("programId", "STRING"),
    ],
    "BasicBlock": [
        ("id", "STRING"), ("name", "STRING"), ("ordinal", "INT64"), ("functionId", "STRING"),
        ("isEntry", "BOOLEAN"), ("isExit", "BOOLEAN"), ("terminatorOp", "STRING"),
        ("operationCount", "INT64"), ("programId", "STRING"),
    ],
    "Operation": [
        ("id", "STRING"), ("name", "STRING"), ("sequenceIndex", "INT64"), ("blockId", "STRING"),
        ("functionId", "STRING"), ("location", "STRING"), ("line", "INT64"), ("col", "INT64"),
        ("isCall", "BOOLEAN"), ("isTerminator", "BOOLEAN"), ("calleeSymbol", "STRING"),
        ("calleeName", "STRING"), ("sideEffect", "STRING"), ("text", "STRING"),
        ("parentOpId", "STRING"), ("regionIndex", "INT64"), ("programId", "STRING"),
    ],
    "Value": [
        ("id", "STRING"), ("name", "STRING"), ("type", "STRING"), ("functionId", "STRING"),
        ("varName", "STRING"), ("isBlockArg", "BOOLEAN"), ("kind", "STRING"),
        ("definingOpId", "STRING"), ("constValue", "STRING"), ("programId", "STRING"),
    ],
}

# (name, FROM, TO, properties)
REL_TABLES: List[Tuple[str, str, str, List[Column]]] = [
    # Hierarchy (cross-layer containment)
    ("CONTAINS_TU", "Program", "TranslationUnit", []),
    ("CONTAINS_FUNCTION", "TranslationUnit", "Function", []),
    ("CONTAINS_GLOBAL", "TranslationUnit", "GlobalVariable", [("isDefinition", "BOOLEAN")]),
    ("REPRESENTS_SYMBOL", "Function", "FunctionSymbol", []),
    ("HAS_BLOCK", "Function", "BasicBlock", []),
    ("CONTAINS_OPERATION", "BasicBlock", "Operation", [("index", "INT64")]),
    ("DEFINES_VALUE", "Operation", "Value", [("resultIndex", "INT64")]),
    ("USES_VALUE", "Operation", "Value", [("operandIndex", "INT64")]),
    ("HAS_PARAMETER", "Function", "Value", [("paramIndex", "INT64")]),
    ("HAS_ARGUMENT", "BasicBlock", "Value", [("argIndex", "INT64")]),
    # Layer 1: call graph
    ("CALLS", "Function", "Function", [
        ("isCrossTU", "BOOLEAN"), ("isExternal", "BOOLEAN"), ("callSiteLine", "INT64"),
        ("callSiteCol", "INT64"), ("isDirect", "BOOLEAN"), ("callOpId", "STRING")]),
    # Layer 2: control flow
    ("CFG_NEXT", "BasicBlock", "BasicBlock", [
        ("kind", "STRING"), ("conditionValueId", "STRING"), ("isBackEdge", "BOOLEAN"),
        ("label", "STRING"), ("successorIndex", "INT64"), ("terminatorOpId", "STRING")]),
    # Layer 3: data flow
    ("FLOWS_TO", "Value", "Value", [
        ("kind", "STRING"), ("sourceOpId", "STRING"), ("targetOpId", "STRING"),
        ("argIndex", "INT64"), ("isInterprocedural", "BOOLEAN")]),
    # Cross-layer bridges
    ("INVOKES", "Operation", "Function", [("isDirect", "BOOLEAN"), ("isCrossTU", "BOOLEAN")]),
    ("ACCESSES_GLOBAL", "Operation", "GlobalVariable", []),
]

SCHEMA_VERSION = "1.0"
REL_BY_NAME = {name: (src, dst, props) for name, src, dst, props in REL_TABLES}


def ddl_statements() -> List[str]:
    statements = []
    for table, columns in NODE_TABLES.items():
        cols = ", ".join(f"{name} {ctype}" for name, ctype in columns)
        statements.append(f"CREATE NODE TABLE IF NOT EXISTS {table}({cols}, PRIMARY KEY(id))")
    for name, src, dst, props in REL_TABLES:
        extra = "".join(f", {p} {t}" for p, t in props)
        statements.append(f"CREATE REL TABLE IF NOT EXISTS {name}(FROM {src} TO {dst}{extra})")
    return statements


# -----------------------------------------------------------------------------
# Rows
# -----------------------------------------------------------------------------
def node_rows(program: Program) -> Dict[str, List[dict]]:
    pid = program.id
    rows: Dict[str, List[dict]] = {table: [] for table in NODE_TABLES}
    rows["Program"].append({"id": pid, "name": program.name, "programId": pid,
                            "schemaVersion": SCHEMA_VERSION})
    for tu in program.translationUnits:
        rows["TranslationUnit"].append({
            "id": tu.id, "compilationId": tu.compilationId, "sourceFile": tu.sourceFile,
            "cirFile": tu.cirFile, "contentHash": tu.contentHash, "programId": pid,
            "typeAliases": json.dumps(tu.typeAliases, sort_keys=True),
            "attrAliases": json.dumps(tu.attrAliases, sort_keys=True)})
    for sym in program.symbols.values():
        rows["FunctionSymbol"].append(dict(vars(sym)))
    for fn in program.all_functions():
        rows["Function"].append({
            "id": fn.id, "name": fn.name, "demangledName": fn.demangledName,
            "sourceFile": fn.sourceFile, "tuId": fn.tuId, "isDeclaration": fn.isDeclaration,
            "isStatic": fn.isStatic, "isEntryPoint": fn.isEntryPoint,
            "isRecursive": fn.isRecursive, "isDeadCode": fn.isDeadCode,
            "isAddressTaken": fn.isAddressTaken, "startLine": fn.startLine,
            "endLine": fn.endLine, "location": fn.location, "linkage": fn.linkage,
            "signature": fn.signature, "returnType": fn.returnType,
            "paramTypes": ", ".join(fn.paramTypes), "inDegree": fn.inDegree,
            "outDegree": fn.outDegree, "blockCount": len(fn.blocks), "programId": pid})
        for block in fn.blocks:
            rows["BasicBlock"].append({
                "id": block.id, "name": block.name, "ordinal": block.ordinal,
                "functionId": block.functionId, "isEntry": block.isEntry, "isExit": block.isExit,
                "terminatorOp": block.terminatorOp, "operationCount": len(block.operations),
                "programId": pid})
        for op in fn.operations:
            rows["Operation"].append({
                "id": op.id, "name": op.name, "sequenceIndex": op.sequenceIndex,
                "blockId": op.blockId, "functionId": op.functionId, "location": op.location,
                "line": op.line, "col": op.col, "isCall": op.isCall,
                "isTerminator": op.isTerminator, "calleeSymbol": op.calleeSymbol,
                "calleeName": op.calleeName, "sideEffect": op.sideEffect, "text": op.text,
                "parentOpId": op.parentOpId, "regionIndex": op.regionIndex, "programId": pid})
        for value in fn.values.values():
            rows["Value"].append({
                "id": value.id, "name": value.name, "type": value.type,
                "functionId": value.functionId, "varName": value.varName,
                "isBlockArg": value.isBlockArg, "kind": value.kind,
                "definingOpId": value.definingOpId, "constValue": value.constValue,
                "programId": pid})
    for gv in program.globals.values():
        rows["GlobalVariable"].append(dict(vars(gv)))
    return rows


def rel_rows(program: Program) -> Dict[str, List[dict]]:
    rows: Dict[str, List[dict]] = {name: [] for name, *_ in REL_TABLES}

    def add(rel: str, src: str, dst: str, **props) -> None:
        rows[rel].append({"src": src, "dst": dst, **props})

    for tu in program.translationUnits:
        add("CONTAINS_TU", program.id, tu.id)
        for fn in tu.functions:
            add("CONTAINS_FUNCTION", tu.id, fn.id)
        for gid, is_definition in tu.globalRefs:
            add("CONTAINS_GLOBAL", tu.id, gid, isDefinition=is_definition)
    for fn in program.all_functions():
        add("REPRESENTS_SYMBOL", fn.id, fn.symbolId)
        for index, vid in enumerate(fn.parameters):
            add("HAS_PARAMETER", fn.id, vid, paramIndex=index)
        for block in fn.blocks:
            add("HAS_BLOCK", fn.id, block.id)
            for index, op_id in enumerate(block.operations):
                add("CONTAINS_OPERATION", block.id, op_id, index=index)
            for index, vid in enumerate(block.arguments):
                add("HAS_ARGUMENT", block.id, vid, argIndex=index)
        for op in fn.operations:
            for index, vid in enumerate(op.results):
                add("DEFINES_VALUE", op.id, vid, resultIndex=index)
            for index, vid in enumerate(op.all_operands):
                add("USES_VALUE", op.id, vid, operandIndex=index)
        for edge in fn.cfgEdges:
            add("CFG_NEXT", edge.src, edge.dst, kind=edge.kind,
                conditionValueId=edge.conditionValueId, isBackEdge=edge.isBackEdge,
                label=edge.label, successorIndex=edge.successorIndex,
                terminatorOpId=edge.terminatorOpId)
        for flow in fn.flows:
            add("FLOWS_TO", flow.src, flow.dst, kind=flow.kind, sourceOpId=flow.sourceOpId,
                targetOpId=flow.targetOpId, argIndex=flow.argIndex, isInterprocedural=False)
    for flow in program.interFlows:
        add("FLOWS_TO", flow.src, flow.dst, kind=flow.kind, sourceOpId=flow.sourceOpId,
            targetOpId=flow.targetOpId, argIndex=flow.argIndex, isInterprocedural=True)
    for call in program.callEdges:
        add("CALLS", call.callerId, call.calleeId, isCrossTU=call.isCrossTU,
            isExternal=call.isExternal, callSiteLine=call.callSiteLine,
            callSiteCol=call.callSiteCol, isDirect=call.isDirect, callOpId=call.callOpId)
        add("INVOKES", call.callOpId, call.calleeId, isDirect=call.isDirect,
            isCrossTU=call.isCrossTU)
    for op_id, gid in program.globalAccesses:
        add("ACCESSES_GLOBAL", op_id, gid)
    return rows


# -----------------------------------------------------------------------------
# Cypher literals
# -----------------------------------------------------------------------------
def quote(value: Optional[str]) -> str:
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    text = text.replace("\n", "\\n").replace("\r", "\\r")
    return f"'{text}'"


def literal(value, ctype: str) -> str:
    if value is None:
        return f"cast(null, '{ctype}')"
    if ctype == "BOOLEAN":
        return "true" if value else "false"
    if ctype == "INT64":
        return str(int(value))
    return quote(value)


def _struct(row: dict, columns: Sequence[Column]) -> str:
    return "{" + ", ".join(f"{name}: {literal(row.get(name), ctype)}" for name, ctype in columns) + "}"


def _batches(rows: List[dict], size: int) -> Iterable[List[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def delete_statements(program_id: str) -> List[str]:
    """Remove a previous import of the same program (relationships go with DETACH)."""
    return [f"MATCH (n:{table}) WHERE n.programId = {quote(program_id)} DETACH DELETE n"
            for table in NODE_TABLES]


def data_statements(program: Program, batch_size: int = 250) -> List[str]:
    statements: List[str] = []
    nodes = node_rows(program)
    for table, columns in NODE_TABLES.items():
        assignments = ", ".join(f"n.{name} = row.{name}" for name, _ in columns if name != "id")
        for batch in _batches(nodes[table], batch_size):
            body = ",\n  ".join(_struct(row, columns) for row in batch)
            statements.append(
                f"UNWIND [\n  {body}\n] AS row\n"
                f"MERGE (n:{table} {{id: row.id}})\n"
                f"SET {assignments}")
    rels = rel_rows(program)
    for name, src, dst, props in REL_TABLES:
        columns = [("src", "STRING"), ("dst", "STRING")] + props
        prop_map = ", ".join(f"{p}: row.{p}" for p, _ in props)
        create = f"CREATE (a)-[:{name} {{{prop_map}}}]->(b)" if props else f"CREATE (a)-[:{name}]->(b)"
        for batch in _batches(rels[name], batch_size):
            body = ",\n  ".join(_struct(row, columns) for row in batch)
            statements.append(
                f"UNWIND [\n  {body}\n] AS row\n"
                f"MATCH (a:{src} {{id: row.src}}), (b:{dst} {{id: row.dst}})\n"
                f"{create}")
    return statements


def import_statements(program: Program, batch_size: int = 250) -> List[str]:
    return ddl_statements() + delete_statements(program.id) + data_statements(program, batch_size)
