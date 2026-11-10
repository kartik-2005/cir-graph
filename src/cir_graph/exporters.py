"""JSON (LLM-oriented) and Graphviz DOT exports of a linked Program."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List

from .models import Function, Program
from .memory import MAX_SLOT_PATH_DEPTH
from .schema import SCHEMA_VERSION


def _flow_dict(flow) -> dict:
    return {"from": flow.src, "to": flow.dst, "kind": flow.kind,
            "sourceOp": flow.sourceOpId, "targetOp": flow.targetOpId,
            **({"argIndex": flow.argIndex} if flow.argIndex is not None else {})}


def _compact(data: dict) -> dict:
    """Drop None / empty values so prompts stay small."""
    return {k: v for k, v in data.items() if v is not None and v != [] and v != {}}


def to_json(program: Program) -> dict:
    functions = program.all_functions()
    index = {fn.id: fn for fn in functions}
    callers: Dict[str, List[str]] = defaultdict(list)
    callees: Dict[str, List[str]] = defaultdict(list)
    for call in program.callEdges:
        if call.callerId not in callers[call.calleeId]:
            callers[call.calleeId].append(call.callerId)
        if call.calleeId not in callees[call.callerId]:
            callees[call.callerId].append(call.calleeId)

    defined = [fn for fn in functions if not fn.isDeclaration]
    flow_kinds = Counter(f.kind for fn in defined for f in fn.flows)
    flow_kinds.update(f.kind for f in program.interFlows)

    def names(ids):
        return [index[i].demangledName for i in ids]

    summary = {
        "translationUnits": len(program.translationUnits),
        "definedFunctions": len(defined),
        "externalFunctions": len(program.externFunctions),
        "globals": len(program.globals),
        "basicBlocks": sum(len(fn.blocks) for fn in defined),
        "cfgEdges": sum(len(fn.cfgEdges) for fn in defined),
        "operations": sum(len(fn.operations) for fn in defined),
        "values": sum(len(fn.values) for fn in defined),
        "callEdges": len(program.callEdges),
        "crossTUCalls": sum(1 for c in program.callEdges if c.isCrossTU),
        "flowsByKind": dict(sorted(flow_kinds.items())),
        "entryPoints": names(fn.id for fn in defined if fn.isEntryPoint),
        "deadCode": names(fn.id for fn in defined if fn.isDeadCode),
        "recursionCycles": [names(cycle) for cycle in program.recursionCycles],
    }

    macro = {
        "translationUnits": [_compact({
            "id": tu.id, "compilationId": tu.compilationId, "sourceFile": tu.sourceFile,
            "cirFile": tu.cirFile, "functions": [fn.id for fn in tu.functions],
            "typeAliases": tu.typeAliases, "attrAliases": tu.attrAliases,
            "globals": [{"id": gid, "isDefinition": d} for gid, d in tu.globalRefs],
        }) for tu in program.translationUnits],
        "functions": [_compact({
            "id": fn.id, "name": fn.name, "demangledName": fn.demangledName,
            "tuId": fn.tuId, "linkage": fn.linkage, "signature": fn.signature,
            "returnType": fn.returnType, "paramTypes": fn.paramTypes,
            "isDeclaration": fn.isDeclaration, "isStatic": fn.isStatic,
            "isEntryPoint": fn.isEntryPoint, "isRecursive": fn.isRecursive,
            "isDeadCode": fn.isDeadCode, "isAddressTaken": fn.isAddressTaken,
            "startLine": fn.startLine, "endLine": fn.endLine,
            "callers": callers.get(fn.id), "callees": callees.get(fn.id),
        }) for fn in functions],
        "globals": [_compact(dict(vars(gv), programId=None)) for gv in program.globals.values()],
        "calls": [_compact({
            "caller": c.callerId, "callee": c.calleeId, "callOp": c.callOpId,
            "line": c.callSiteLine, "col": c.callSiteCol, "isDirect": c.isDirect,
            "isCrossTU": c.isCrossTU, "isExternal": c.isExternal,
        }) for c in program.callEdges],
        "recursionCycles": program.recursionCycles,
    }

    meso: Dict[str, dict] = {}
    micro: Dict[str, dict] = {}
    for fn in defined:
        succs: Dict[str, List[dict]] = defaultdict(list)
        for edge in fn.cfgEdges:
            succs[edge.src].append(_compact({
                "to": edge.dst, "kind": edge.kind, "label": edge.label,
                "condition": edge.conditionValueId,
                "isBackEdge": edge.isBackEdge or None}))
        meso[fn.id] = {
            "entryBlock": next((b.id for b in fn.blocks if b.isEntry), None),
            "blocks": [_compact({
                "id": b.id, "name": b.name, "ordinal": b.ordinal, "isEntry": b.isEntry or None,
                "isExit": b.isExit or None, "terminatorOp": b.terminatorOp,
                "arguments": b.arguments, "operations": b.operations,
                "successors": succs.get(b.id),
            }) for b in fn.blocks],
        }
        micro[fn.id] = {
            "parameters": fn.parameters,
            "operations": [_compact({
                "id": op.id, "seq": op.sequenceIndex, "name": op.name, "text": op.text,
                "block": op.blockId, "line": op.line, "col": op.col,
                "results": op.results, "operands": op.all_operands,
                "successorOperands": op.successorOperands,
                "callee": op.calleeName, "calleeSymbol": op.calleeSymbol,
                "sideEffect": op.sideEffect if op.sideEffect != "none" else None,
                "parentOp": op.parentOpId,
            }) for op in fn.operations],
            "values": [_compact({
                "id": v.id, "name": v.name, "type": v.type, "kind": v.kind,
                "varName": v.varName, "const": v.constValue, "definedBy": v.definingOpId,
            }) for v in fn.values.values()],
            "flows": [_flow_dict(f) for f in fn.flows],
        }

    flows_by_op: Dict[str, List[dict]] = defaultdict(list)
    for flow in program.interFlows:
        if flow.kind in ("call_argument", "call_return"):
            flows_by_op[flow.targetOpId].append(flow)
    call_sites = []
    for call in program.callEdges:
        callee = index[call.calleeId]
        params = set(callee.parameters)
        bindings = [f for f in flows_by_op.get(call.callOpId, [])]
        call_sites.append(_compact({
            "callOp": call.callOpId, "caller": call.callerId, "callee": call.calleeId,
            "isDirect": call.isDirect, "isCrossTU": call.isCrossTU,
            "argumentBindings": [{"argument": f.src, "parameter": f.dst, "index": f.argIndex}
                                 for f in bindings if f.kind == "call_argument" and f.dst in params],
            "returnBindings": [{"returnValue": f.src, "result": f.dst, "returnOp": f.sourceOpId}
                               for f in bindings if f.kind == "call_return"
                               and f.src.startswith(callee.id + "#")],
        }))
    bridges = {
        "callSites": call_sites,
        "interproceduralFlows": [_flow_dict(f) for f in program.interFlows],
        "globalAccesses": [{"op": op, "global": gid} for op, gid in program.globalAccesses],
    }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "analysisLimits": {"memorySlotPathDepth": MAX_SLOT_PATH_DEPTH,
                           "deeperMemoryPaths": "may-alias wildcard; weak updates"},
        "program": {"id": program.id, "name": program.name},
        "summary": summary,
        "layers": {"macro": macro, "meso": meso, "micro": micro, "bridges": bridges},
    }


# -----------------------------------------------------------------------------
# Graphviz
# -----------------------------------------------------------------------------
def _esc(text) -> str:
    return (str(text).replace("\\", "\\\\").replace('"', '\\"')
            .replace("{", "\\{").replace("}", "\\}").replace("<", "\\<").replace(">", "\\>"))


def _nid(raw: str) -> str:
    return '"' + raw.replace('"', "'") + '"'


EDGE_COLORS = {"true": "#2E7D32", "false": "#C62828", "unconditional": "#455A64",
               "switch": "#6A1B9A", "exception": "#EF6C00"}


def _block_label(fn: Function, block, ops_by_id, max_ops: int) -> str:
    lines = [f"{block.name}  (bb{block.ordinal})"]
    shown = 0
    for op_id in block.operations:
        op = ops_by_id[op_id]
        if op.name in ("cir.alloca",):
            continue
        text = op.name.replace("cir.", "")
        if op.isCall:
            text += f" {op.calleeName or '<indirect>'}"
        elif op.name == "cir.const":
            const = fn.values[op.results[0]].constValue if op.results else ""
            text += f" {const}"
        elif op.name in ("cir.load", "cir.store") and op.operands:
            slot_vid = op.operands[0] if op.name == "cir.load" else op.operands[-1]
            var = fn.values[slot_vid].varName
            text += f" {var}" if var else ""
        if op.line:
            text += f"  :{op.line}"
        lines.append(text)
        shown += 1
        if shown >= max_ops:
            lines.append("…")
            break
    return "\\l".join(_esc(line) for line in lines) + "\\l"


def to_dot(program: Program, max_ops_per_block: int = 14) -> str:
    out = [
        "digraph cir_graph {",
        '  graph [compound=true, rankdir=TB, fontname="Helvetica", fontsize=12, '
        f'label="{_esc(program.name)} — unified CG / CFG / DFG", labelloc=t];',
        '  node [shape=box, style="rounded,filled", fontname="Courier", fontsize=9, fillcolor="#FFFFFF"];',
        '  edge [fontname="Helvetica", fontsize=8];',
    ]
    entry_of: Dict[str, str] = {}
    block_of_op: Dict[str, str] = {}
    for tu_index, tu in enumerate(program.translationUnits):
        out.append(f'  subgraph "cluster_tu_{tu_index}" {{')
        out.append(f'    label="TU {_esc(tu.compilationId)}"; style="dashed,rounded"; color="#90A4AE";')
        for fn_index, fn in enumerate(tu.functions):
            ops_by_id = {op.id: op for op in fn.operations}
            fill = ("#C8E6C9" if fn.isEntryPoint else "#FFCDD2" if fn.isRecursive
                    else "#EEEEEE" if fn.isDeadCode else "#E3F2FD")
            tags = [t for t, on in (("entry", fn.isEntryPoint), ("recursive", fn.isRecursive),
                                    ("dead", fn.isDeadCode), ("static", fn.isStatic)) if on]
            out.append(f'    subgraph "cluster_fn_{tu_index}_{fn_index}" {{')
            out.append(f'      label="{_esc(fn.demangledName)}'
                       f'{" [" + ", ".join(tags) + "]" if tags else ""}"; style="rounded,filled"; '
                       f'fillcolor="{fill}"; color="#546E7A";')
            for block in fn.blocks:
                style = ', penwidth=2' if block.isEntry else ''
                shape = ', peripheries=2' if block.isExit else ''
                out.append(f'      {_nid(block.id)} [label="{_block_label(fn, block, ops_by_id, max_ops_per_block)}"'
                           f'{style}{shape}];')
                if block.isEntry:
                    entry_of[fn.id] = block.id
                for op_id in block.operations:
                    block_of_op[op_id] = block.id
            out.append("    }")
        out.append("  }")
    for fn in program.externFunctions.values():
        out.append(f'  {_nid(fn.id)} [label="{_esc(fn.demangledName)}\\nextern", shape=hexagon, '
                   'style="dashed,filled", fillcolor="#FFF8E1"];')
        entry_of[fn.id] = fn.id

    for tu in program.translationUnits:
        for fn in tu.functions:
            for edge in fn.cfgEdges:
                label = edge.kind if edge.kind != "unconditional" else ""
                if edge.label:
                    label = f"{label} {edge.label}".strip()
                attrs = [f'color="{EDGE_COLORS.get(edge.kind, "#455A64")}"']
                if label:
                    attrs.append(f'label="{_esc(label)}"')
                if edge.isBackEdge:
                    attrs.append('style=bold, constraint=false')
                out.append(f"  {_nid(edge.src)} -> {_nid(edge.dst)} [{', '.join(attrs)}];")
    for call in program.callEdges:
        src = block_of_op.get(call.callOpId)
        dst = entry_of.get(call.calleeId)
        if not src or not dst:
            continue
        color = "#6A1B9A" if call.isCrossTU else "#1565C0"
        style = "dashed" if call.isDirect else "dotted"
        label = ("call ×TU" if call.isCrossTU else "call") + ("" if call.isDirect else " (indirect)")
        out.append(f'  {_nid(src)} -> {_nid(dst)} [color="{color}", style={style}, penwidth=1.6, '
                   f'label="{label}", fontcolor="{color}", constraint=false];')
    out.append("}")
    return "\n".join(out) + "\n"
