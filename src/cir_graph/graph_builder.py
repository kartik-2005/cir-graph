"""Per-function construction of the meso (CFG) and micro (SSA) layers.

Raw structured CIR keeps control flow inside region-carrying operations
(cir.if, cir.ternary, cir.while, cir.for, cir.do, cir.switch, cir.scope).
This module lowers that region tree to basic blocks the same way CIR's
FlattenCFG pass would, so CFG edges, operations and values all refer to one
representation. Already-flattened CIR (explicit ^bb blocks) is consumed as is.

Lowering rules (block names follow Clang's conventions):

* cir.scope / unknown region ops: regions are inlined into the current block.
* cir.if:        head -true-> if.then, -false-> if.else | if.end
* cir.ternary:   head -true-> ternary.true, -false-> ternary.false; yields
                 join in ternary.end and flow into the ternary result.
* cir.while:     head -> while.cond -true-> while.body -> while.cond (back edge)
                                    -false-> while.end
* cir.do:        head -> do.body -> do.cond -true-> do.body (back edge)
* cir.for:       head -> for.cond -true-> for.body -> for.step -> for.cond
* cir.switch:    head -switch-> each cir.case block; case fallthrough via
                 cir.yield, cir.break exits to switch.end.
* cir.break / cir.continue jump to the innermost loop/switch targets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple

from .cir_parser import (
    RawBlock, RawFunction, RawModule, RawOp, RawRegion, mask_strings,
    rfind_top_level, split_top_level, strip_locs,
)
from .models import BasicBlock, CFGEdge, FlowEdge, Function, Operation, Value

EXIT_OPS = {"cir.return", "cir.unreachable", "cir.trap", "cir.resume", "cir.resume.flat"}
BRANCH_OPS = {"cir.br", "cir.brcond", "cir.switch.flat", "cir.indirect_br", "cir.try_call"}
CALL_OPS = {"cir.call", "cir.try_call"}
# Operations whose operands do not flow into their results as data.
NO_DEF_USE = {
    "cir.alloca", "cir.store", "cir.load", "cir.call", "cir.try_call", "cir.return",
    "cir.br", "cir.brcond", "cir.yield", "cir.condition", "cir.if", "cir.ternary",
    "cir.switch", "cir.switch.flat", "cir.case", "cir.const", "cir.get_global",
    "cir.copy", "cir.scope", "cir.while", "cir.for", "cir.do", "cir.break", "cir.continue",
    "cir.goto", "cir.label",
}
LOOP_LIKE = {"cir.while", "cir.for", "cir.do"}


@dataclass
class _Blk:
    hint: str
    serial: int
    ops: List[Operation] = field(default_factory=list)
    args: List[str] = field(default_factory=list)
    is_exit: bool = False
    terminator: Optional[str] = None


@dataclass
class _Edge:
    src: _Blk
    dst: _Blk
    kind: str
    cond: Optional[str] = None
    label: Optional[str] = None
    term_op: Optional[str] = None


@dataclass
class _Ctx:
    break_target: Optional[_Blk] = None
    continue_target: Optional[_Blk] = None
    yield_target: Optional[_Blk] = None   # None: region is inlined
    yield_results: List[str] = field(default_factory=list)
    yield_op: Optional[str] = None
    cond_true: Optional[_Blk] = None
    cond_false: Optional[_Blk] = None
    parent_op: Optional[str] = None
    region_index: Optional[int] = None


def const_literal(attr: str, attr_aliases: Dict[str, str]) -> str:
    """Render a CIR constant attribute compactly: #cir.int<1> -> '1'."""
    text = attr.strip()
    if text in attr_aliases:
        text = attr_aliases[text].strip()
        colon = rfind_top_level(text, " : ")
        if colon >= 0:
            text = text[:colon].strip()
    match = re.match(r"^#cir\.(?:int|fp|bool)<(.*)>$", text)
    if match:
        return match.group(1).strip()
    if text.startswith("#cir.ptr<null"):
        return "null"
    return text


def infer_result_type(raw: RawOp, attr_aliases: Dict[str, str]) -> Optional[str]:
    """Best-effort type of the first result from the printed operation."""
    text = strip_locs(raw.header if not raw.trailers else raw.trailers[-1])
    if raw.name == "cir.cmp":
        return "!cir.bool"
    if raw.name == "cir.const":
        attr = text[len("cir.const"):].strip()
        colon = rfind_top_level(attr, " : ")
        if colon >= 0:
            return attr[colon + 3:].strip()
        alias = attr_aliases.get(attr)
        if alias:
            colon = rfind_top_level(alias, " : ")
            return alias[colon + 3:].strip() if colon >= 0 else None
        return None
    if raw.name == "cir.call" and not raw.results:
        return None
    arrow = rfind_top_level(text, "->")
    if arrow >= 0:
        result = text[arrow + 2:].strip()
        if result.startswith("("):
            parts = split_top_level(result[1:-1])
            result = parts[0] if parts else ""
        return _drop_attr_dict(result) or None
    colon = rfind_top_level(text, " : ")
    if colon >= 0:
        parts = split_top_level(text[colon + 3:])
        return _drop_attr_dict(parts[-1]) if parts else None
    return None


def _drop_attr_dict(text: str) -> str:
    masked = mask_strings(text)
    depth = 0
    for i, ch in enumerate(masked):
        if ch == "{" and depth == 0:
            return text[:i].strip()
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth -= 1
    return text.strip()


def _case_label(raw: RawOp) -> str:
    text = strip_locs(raw.header)
    match = re.match(r"^cir\.case\s*\(\s*(\w+)\s*,\s*\[(.*)\]\s*\)", text)
    if not match:
        return "case"
    kind, values = match.group(1), match.group(2)
    literals = [const_literal(split_top_level(v, " : ")[0], {}) for v in split_top_level(values)]
    if kind == "default":
        return "default"
    if kind == "range" and len(literals) == 2:
        return f"{literals[0]}...{literals[1]}"
    return ",".join(literals)


class FunctionBuilder:
    """Builds blocks, operations, values and intra-procedural flows."""

    def __init__(self, raw: RawFunction, module: RawModule, fn: Function,
                 demangle: Callable[[str], str]):
        self.raw = raw
        self.module = module
        self.fn = fn
        self.demangle = demangle
        self.blocks: List[_Blk] = []
        self.edges: List[_Edge] = []
        self.cur: Optional[_Blk] = None
        self.seq = 0
        self.scopes: Dict[int, Dict[str, str]] = {}
        self.parents: Dict[int, Optional[RawRegion]] = {}
        self.result_vids: Dict[int, List[str]] = {}
        self.block_arg_vids: Dict[Tuple[int, int], List[str]] = {}
        self.label_maps: Dict[int, Dict[str, _Blk]] = {}
        self.op_block: Dict[str, _Blk] = {}
        self.flows: List[FlowEdge] = []
        self._taken: Dict[str, int] = {}
        self.named_labels: Dict[str, _Blk] = {}
        self.pending_gotos: List[Tuple[_Blk, str, Operation]] = []

    # -- values ---------------------------------------------------------------
    def _new_value(self, name: str, vtype: Optional[str], kind: str, block_arg: bool) -> str:
        count = self._taken.get(name, 0)
        self._taken[name] = count + 1
        vid = f"{self.fn.id}#{name}" if count == 0 else f"{self.fn.id}#{name}.{count}"
        self.fn.values[vid] = Value(id=vid, name=name, type=vtype, functionId=self.fn.id,
                                    kind=kind, isBlockArg=block_arg, programId=self.fn.programId)
        return vid

    def _declare(self, region: RawRegion, parent: Optional[RawRegion], is_body: bool) -> None:
        scope: Dict[str, str] = {}
        self.scopes[id(region)] = scope
        self.parents[id(region)] = parent
        for b_index, block in enumerate(region.blocks):
            vids = []
            for name, vtype in block.args:
                kind = "parameter" if is_body and b_index == 0 else "block_arg"
                vid = self._new_value(name, vtype, kind, True)
                scope[name] = vid
                vids.append(vid)
            self.block_arg_vids[(id(region), b_index)] = vids
            for op in block.ops:
                rtype = infer_result_type(op, self.module.attrAliases) if op.results else None
                kind = "constant" if op.name == "cir.const" else "result"
                vids_r = []
                for name in op.results:
                    vid = self._new_value(name, rtype, kind, False)
                    scope[name] = vid
                    vids_r.append(vid)
                self.result_vids[id(op)] = vids_r
                for sub in op.regions:
                    self._declare(sub, region, False)

    def lookup(self, name: str, region: Optional[RawRegion]) -> str:
        while region is not None:
            scope = self.scopes.get(id(region), {})
            if name in scope:
                return scope[name]
            region = self.parents.get(id(region))
        vid = f"{self.fn.id}#{name}?"
        if vid not in self.fn.values:
            self.fn.values[vid] = Value(id=vid, name=name, type=None, functionId=self.fn.id,
                                        kind="unresolved", programId=self.fn.programId)
        return vid

    # -- blocks and edges -------------------------------------------------------
    def new_block(self, hint: str) -> _Blk:
        blk = _Blk(hint=hint, serial=len(self.blocks))
        self.blocks.append(blk)
        return blk

    def ensure_cur(self) -> _Blk:
        if self.cur is None:
            self.cur = self.new_block("unreachable")
        return self.cur

    def edge(self, src: Optional[_Blk], dst: Optional[_Blk], kind: str = "unconditional",
             cond: Optional[str] = None, label: Optional[str] = None,
             op: Optional[Operation] = None) -> None:
        if src is None or dst is None:
            return
        self.edges.append(_Edge(src, dst, kind, cond, label, op.id if op else None))
        if op is not None:
            src.terminator = op.name

    def flow(self, src: str, dst: str, kind: str, source_op: Optional[str],
             target_op: Optional[str], index: Optional[int] = None) -> None:
        self.flows.append(FlowEdge(src, dst, kind, source_op, target_op, index))

    # -- operations ---------------------------------------------------------------
    def emit(self, raw: RawOp, region: RawRegion, ctx: _Ctx) -> Operation:
        blk = self.ensure_cur()
        seq = self.seq
        self.seq += 1
        op_id = f"{self.fn.id}#op{seq}"
        points = self.module.locations.resolve(raw.loc)
        point = points[0] if points else None
        op = Operation(
            id=op_id, name=raw.name, sequenceIndex=seq, blockId="", functionId=self.fn.id,
            text=raw.display_text, location=f"{point.file}:{point.line}:{point.col}" if point else None,
            line=point.line if point else None, col=point.col if point else None,
            parentOpId=ctx.parent_op, regionIndex=ctx.region_index, programId=self.fn.programId,
        )
        op.operands = [self.lookup(n, region) for n in raw.operands]
        op.successorOperands = [[self.lookup(n, region) for n in successor.operands]
                                for successor in raw.successors]
        op.results = list(self.result_vids.get(id(raw), []))
        for vid in op.results:
            self.fn.values[vid].definingOpId = op_id
        self._annotate(raw, op)
        blk.ops.append(op)
        self.op_block[op_id] = blk
        self.fn.operations.append(op)
        return op

    def _annotate(self, raw: RawOp, op: Operation) -> None:
        name = raw.name
        if name in CALL_OPS:
            op.isCall = True
            effect = re.search(r"side_effect\((\w+)\)", raw.header)
            op.sideEffect = effect.group(1) if effect else "all"
            if re.match(r"^cir\.(?:try_)?call\s+@", raw.header) and raw.symbol:
                op.calleeSymbol = raw.symbol
                op.calleeName = self.demangle(raw.symbol)
        elif name == "cir.load":
            op.sideEffect = "read"
        elif name in ("cir.store", "cir.copy"):
            op.sideEffect = "write"
        elif name == "cir.alloca":
            op.sideEffect = "alloc"
            var = re.match(r'^cir\.alloca\s+"((?:[^"\\]|\\.)*)"', raw.header)
            if var and op.results:
                self.fn.values[op.results[0]].varName = var.group(1)
        if name == "cir.const" and op.results:
            attr = strip_locs(raw.header)[len("cir.const"):].strip()
            colon = rfind_top_level(attr, " : ")
            literal = attr[:colon].strip() if colon >= 0 else attr
            self.fn.values[op.results[0]].constValue = const_literal(literal, self.module.attrAliases)
        if name == "cir.get_global" and raw.symbol:
            op.globalSymbol = raw.symbol
            if op.results:
                self.fn.values[op.results[0]].varName = self.demangle(raw.symbol)
        op.isTerminator = name in EXIT_OPS or name in BRANCH_OPS or name in (
            "cir.yield", "cir.condition", "cir.break", "cir.continue", "cir.goto")

    def _def_use(self, op: Operation) -> None:
        if op.name in NO_DEF_USE or not op.results:
            return
        for index, src in enumerate(op.operands):
            src_def = self.fn.values[src].definingOpId
            for dst in op.results:
                self.flow(src, dst, "def_use", src_def, op.id, index)

    # -- lowering -----------------------------------------------------------------
    def build(self) -> None:
        body = self.raw.body
        self._declare(body, None, True)
        entry = self.new_block("entry")
        self.cur = entry
        self.fn.parameters = list(self.block_arg_vids.get((id(body), 0), []))
        entry.args.extend(self.fn.parameters)
        self.lower_region(body, _Ctx(), function_body=True)
        for head, label, op in self.pending_gotos:
            if label not in self.named_labels:
                raise ValueError(f"{self.fn.id}: unknown goto label {label!r}")
            self.edge(head, self.named_labels[label], op=op)
        self._finalize()

    def lower_region(self, region: RawRegion, ctx: _Ctx, function_body: bool = False) -> None:
        blocks = region.blocks
        if len(blocks) == 1 and blocks[0].label is None:
            if not function_body:
                self.ensure_cur().args.extend(self.block_arg_vids.get((id(region), 0), []))
            self.lower_ops(blocks[0], region, ctx)
            return
        label_map: Dict[str, _Blk] = {}
        mapped: List[_Blk] = []
        for index, block in enumerate(blocks):
            blk = self.ensure_cur() if index == 0 else self.new_block(block.label or "block")
            if block.label:
                label_map[block.label] = blk
            if index > 0 or not function_body:
                blk.args.extend(self.block_arg_vids.get((id(region), index), []))
            mapped.append(blk)
        self.label_maps[id(region)] = label_map
        for index, block in enumerate(blocks):
            if index > 0:
                if self.cur is not None:
                    self.edge(self.cur, mapped[index])
                self.cur = mapped[index]
            self.lower_ops(block, region, ctx)

    def lower_ops(self, block: RawBlock, region: RawRegion, ctx: _Ctx) -> None:
        for raw in block.ops:
            self.lower_op(raw, region, ctx)

    def _label_target(self, region: RawRegion, label: str) -> Optional[_Blk]:
        while region is not None:
            target = self.label_maps.get(id(region), {}).get(label)
            if target is not None:
                return target
            region = self.parents.get(id(region))
        return None

    def _sub_ctx(self, ctx: _Ctx, op: Operation, index: int, **changes) -> _Ctx:
        return replace(ctx, parent_op=op.id, region_index=index, **changes)

    def lower_op(self, raw: RawOp, region: RawRegion, ctx: _Ctx) -> None:
        name = raw.name
        if name == "cir.label":
            label = self._named_label(raw)
            if label in self.named_labels:
                raise ValueError(f"{self.fn.id}: duplicate label {label!r}")
            previous = self.cur
            if previous is None or previous.ops:
                self.cur = self.new_block(f"label.{label}")
                self.edge(previous, self.cur)
            else:
                self.cur.hint = f"label.{label}"
            self.named_labels[label] = self.cur
        op = self.emit(raw, region, ctx)
        self._def_use(op)
        head = self.op_block[op.id]

        if name == "cir.goto":
            self.pending_gotos.append((head, self._named_label(raw), op))
            self.cur = None
        elif name in BRANCH_OPS:
            self._lower_branch(raw, op, region)
            self.cur = None
        elif name in EXIT_OPS:
            head.is_exit = True
            head.terminator = name
            self.cur = None
        elif name == "cir.yield":
            for index, (src, dst) in enumerate(zip(op.operands, ctx.yield_results)):
                self.flow(src, dst, "region_yield", self.fn.values[src].definingOpId,
                          ctx.yield_op, index)
            if ctx.yield_target is not None:
                self.edge(head, ctx.yield_target, op=op)
                self.cur = None
        elif name == "cir.condition":
            cond = op.operands[0] if op.operands else None
            self.edge(head, ctx.cond_true, "true", cond, op=op)
            self.edge(head, ctx.cond_false, "false", cond, op=op)
            self.cur = None
        elif name == "cir.break":
            self.edge(head, ctx.break_target, op=op)
            self.cur = None
        elif name == "cir.continue":
            self.edge(head, ctx.continue_target, op=op)
            self.cur = None
        elif name in ("cir.if", "cir.ternary"):
            self._lower_if(raw, op, ctx)
        elif name in LOOP_LIKE:
            self._lower_loop(raw, op, ctx)
        elif name == "cir.switch":
            self._lower_switch(raw, op, ctx)
        elif name == "cir.try":
            self._lower_try(raw, op, ctx)
        else:
            # cir.scope and any other region-carrying op: inline its regions so
            # nested calls and data flow are never dropped.
            for index, sub in enumerate(raw.regions):
                self.lower_region(sub, self._sub_ctx(
                    ctx, op, index, yield_target=None, yield_results=op.results, yield_op=op.id))

    @staticmethod
    def _named_label(raw: RawOp) -> str:
        match = re.match(r'^cir\.(?:goto|label)\s+"((?:[^"\\]|\\.)*)"', raw.header)
        if not match:
            raise ValueError(f"Unsupported named label: {raw.header}")
        return match.group(1)

    def _lower_branch(self, raw: RawOp, op: Operation, region: RawRegion) -> None:
        head = self.op_block[op.id]
        name = raw.name
        succ_count = len(raw.successors)
        for index, succ in enumerate(raw.successors):
            target = self._label_target(region, succ.label)
            if target is None:
                raise ValueError(f"{self.fn.id}: unknown successor block {succ.label!r}")
            cond = None
            label = None
            if name == "cir.brcond":
                kind = "true" if index == 0 else "false"
                cond = op.operands[0] if op.operands else None
            elif name == "cir.switch.flat":
                kind = "switch"
                label = "default" if index == 0 else succ.caseValue
                cond = op.operands[0] if op.operands else None
            elif name == "cir.try_call":
                kind = "unconditional" if index == 0 else "exception"
            elif name == "cir.indirect_br":
                kind = "indirect"
            else:
                kind = "unconditional"
            self.edge(head, target, kind, cond, label, op)
            for arg_index, operand in enumerate(succ.operands):
                if arg_index < len(target.args):
                    src = self.lookup(operand, region)
                    self.flow(src, target.args[arg_index], "block_arg",
                              self.fn.values[src].definingOpId, op.id, arg_index)
        if succ_count == 0:
            head.terminator = name

    def _lower_if(self, raw: RawOp, op: Operation, ctx: _Ctx) -> None:
        head = self.op_block[op.id]
        prefix = "if" if raw.name == "cir.if" else "ternary"
        cond = op.operands[0] if op.operands else None
        has_else = len(raw.regions) > 1 and any(b.ops for b in raw.regions[1].blocks)
        then_blk = self.new_block(f"{prefix}.then" if prefix == "if" else "ternary.true")
        else_blk = self.new_block(f"{prefix}.else" if prefix == "if" else "ternary.false") if has_else else None
        join = self.new_block(f"{prefix}.end")
        self.edge(head, then_blk, "true", cond, op=op)
        self.edge(head, else_blk or join, "false", cond, op=op)
        for index, (blk, sub) in enumerate(zip((then_blk, else_blk), raw.regions)):
            if blk is None:
                continue
            self.cur = blk
            self.lower_region(sub, self._sub_ctx(
                ctx, op, index, yield_target=join, yield_results=op.results, yield_op=op.id))
            if self.cur is not None:
                self.edge(self.cur, join)
        self.cur = join

    def _lower_loop(self, raw: RawOp, op: Operation, ctx: _Ctx) -> None:
        head = self.op_block[op.id]
        regions = raw.regions
        if raw.name == "cir.while" and len(regions) >= 2:
            cond_blk, body_blk = self.new_block("while.cond"), self.new_block("while.body")
            exit_blk = self.new_block("while.end")
            self.edge(head, cond_blk, op=op)
            plan = [(cond_blk, regions[0], dict(cond_true=body_blk, cond_false=exit_blk)),
                    (body_blk, regions[1], dict(break_target=exit_blk, continue_target=cond_blk,
                                                yield_target=cond_blk))]
            after = {1: cond_blk}
        elif raw.name == "cir.do" and len(regions) >= 2:
            body_blk, cond_blk = self.new_block("do.body"), self.new_block("do.cond")
            exit_blk = self.new_block("do.end")
            self.edge(head, body_blk, op=op)
            plan = [(body_blk, regions[0], dict(break_target=exit_blk, continue_target=cond_blk,
                                                yield_target=cond_blk)),
                    (cond_blk, regions[1], dict(cond_true=body_blk, cond_false=exit_blk))]
            after = {0: cond_blk}
        elif raw.name == "cir.for" and len(regions) >= 3:
            cond_blk, body_blk = self.new_block("for.cond"), self.new_block("for.body")
            step_blk, exit_blk = self.new_block("for.inc"), self.new_block("for.end")
            self.edge(head, cond_blk, op=op)
            plan = [(cond_blk, regions[0], dict(cond_true=body_blk, cond_false=exit_blk)),
                    (body_blk, regions[1], dict(break_target=exit_blk, continue_target=step_blk,
                                                yield_target=step_blk)),
                    (step_blk, regions[2], dict(yield_target=cond_blk))]
            after = {1: step_blk, 2: cond_blk}
        else:
            for index, sub in enumerate(regions):
                self.lower_region(sub, self._sub_ctx(ctx, op, index, yield_target=None))
            return
        index_of = {id(sub): i for i, sub in enumerate(regions)}
        for blk, sub, changes in plan:
            self.cur = blk
            changes.setdefault("yield_target", None)
            changes.setdefault("yield_results", [])
            changes["yield_op"] = op.id
            index = index_of[id(sub)]
            self.lower_region(sub, self._sub_ctx(ctx, op, index, **changes))
            if self.cur is not None:
                self.edge(self.cur, after.get(index, exit_blk))
        self.cur = exit_blk

    def _lower_switch(self, raw: RawOp, op: Operation, ctx: _Ctx) -> None:
        head = self.op_block[op.id]
        cond = op.operands[0] if op.operands else None
        exit_blk = self.new_block("switch.end")
        if not raw.regions:
            self.cur = exit_blk
            return
        region = raw.regions[0]
        cases = [c for b in region.blocks for c in b.ops if c.name == "cir.case"]
        case_blocks = []
        has_default = False
        for case in cases:
            label = _case_label(case)
            has_default = has_default or label == "default"
            blk = self.new_block("switch.default" if label == "default" else "switch.case")
            case_blocks.append(blk)
            self.edge(head, blk, "switch", cond, label, op)
        if not has_default:
            self.edge(head, exit_blk, "switch", cond, "default", op)
        case_index = 0
        for block in region.blocks:
            for raw_child in block.ops:
                if raw_child.name != "cir.case":
                    if raw_child.name == "cir.yield":
                        continue
                    self.cur = head if self.cur is None else self.cur
                    self.lower_op(raw_child, region, self._sub_ctx(ctx, op, 0))
                    continue
                blk = case_blocks[case_index]
                fallthrough = case_blocks[case_index + 1] if case_index + 1 < len(case_blocks) else exit_blk
                case_index += 1
                self.cur = blk
                case_op = self.emit(raw_child, region, self._sub_ctx(ctx, op, 0))
                for sub_index, sub in enumerate(raw_child.regions):
                    self.lower_region(sub, self._sub_ctx(
                        ctx, case_op, sub_index, break_target=exit_blk,
                        yield_target=fallthrough, yield_results=[], yield_op=case_op.id))
                if self.cur is not None:
                    self.edge(self.cur, fallthrough)
        self.cur = exit_blk

    def _lower_try(self, raw: RawOp, op: Operation, ctx: _Ctx) -> None:
        head = self.op_block[op.id]
        join = self.new_block("try.cont")
        if raw.regions:
            body = self.new_block("try.body")
            self.edge(head, body, op=op)
            self.cur = body
            self.lower_region(raw.regions[0], self._sub_ctx(
                ctx, op, 0, yield_target=join, yield_results=op.results, yield_op=op.id))
            if self.cur is not None:
                self.edge(self.cur, join)
        for index, sub in enumerate(raw.regions[1:], 1):
            handler = self.new_block("catch")
            self.edge(head, handler, "exception", op=op)
            self.cur = handler
            self.lower_region(sub, self._sub_ctx(
                ctx, op, index, yield_target=join, yield_results=op.results, yield_op=op.id))
            if self.cur is not None:
                self.edge(self.cur, join)
        self.cur = join

    # -- finalization ---------------------------------------------------------------
    def _finalize(self) -> None:
        connected = {id(e.src) for e in self.edges} | {id(e.dst) for e in self.edges}
        live = [b for b in self.blocks
                if b.ops or id(b) in connected or b.serial == 0]
        succs: Dict[int, List[_Edge]] = {id(b): [] for b in live}
        for edge in self.edges:
            succs[id(edge.src)].append(edge)

        # Ordinals follow creation (source) order; loop headers are created
        # before their bodies, so back edges always target a lower ordinal.
        # Back edges are edges whose target is still on the DFS stack.
        entry = self.blocks[0]
        state: Dict[int, int] = {}
        back: set = set()
        stack = [(entry, iter(succs[id(entry)]))]
        state[id(entry)] = 1
        while stack:
            blk, it = stack[-1]
            edge = next(it, None)
            if edge is None:
                stack.pop()
                state[id(blk)] = 2
                continue
            target_state = state.get(id(edge.dst), 0)
            if target_state == 1:
                back.add(id(edge))
            elif target_state == 0:
                state[id(edge.dst)] = 1
                stack.append((edge.dst, iter(succs[id(edge.dst)])))
        final = live

        ids: Dict[int, str] = {}
        for ordinal, blk in enumerate(final):
            block_id = f"{self.fn.id}#bb{ordinal}"
            ids[id(blk)] = block_id
            model = BasicBlock(
                id=block_id, name=blk.hint, ordinal=ordinal, functionId=self.fn.id,
                isEntry=blk is entry, isExit=blk.is_exit, terminatorOp=blk.terminator,
                operations=[o.id for o in blk.ops], arguments=list(blk.args),
                programId=self.fn.programId)
            for o in blk.ops:
                o.blockId = block_id
            self.fn.blocks.append(model)

        per_src: Dict[int, int] = {}
        for edge in self.edges:
            if id(edge.src) not in ids or id(edge.dst) not in ids:
                continue
            index = per_src.get(id(edge.src), 0)
            per_src[id(edge.src)] = index + 1
            self.fn.cfgEdges.append(CFGEdge(
                src=ids[id(edge.src)], dst=ids[id(edge.dst)], kind=edge.kind,
                successorIndex=index, conditionValueId=edge.cond, label=edge.label,
                isBackEdge=id(edge) in back, terminatorOpId=edge.term_op))
        self.fn.operations.sort(key=lambda o: o.sequenceIndex)
        self.fn.flows.extend(self.flows)


def build_function(raw: RawFunction, module: RawModule, fn: Function,
                   demangle: Callable[[str], str]) -> Function:
    builder = FunctionBuilder(raw, module, fn, demangle)
    builder.build()
    return fn
