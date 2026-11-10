"""Multi-TU linking: symbols, call graph, cross-layer bridges and analyses.

Resolution rules (mirroring the static linker):

1. A call first binds to a definition in the caller's own translation unit.
   This is the only way to reach a `static` (internal linkage) function, so
   two TUs that each define `static int offset(int)` never collide.
2. Otherwise it binds to the externally visible definition of the symbol in
   any loaded TU (`extern` declaration in a header -> definition elsewhere).
3. Otherwise the callee is an unresolved external declaration, represented
   once per symbol by an `isDeclaration` Function node.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections import defaultdict, deque
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .cir_parser import RawFunction, RawModule, STATIC_LINKAGES, parse_cir_file
from .graph_builder import build_function
from .memory import Def, MemoryModel, normalize_slot
from .models import (
    CallEdge, FlowEdge, Function, FunctionSymbol, GlobalVariable, Program,
    TranslationUnit,
)


# -----------------------------------------------------------------------------
# Demangling
# -----------------------------------------------------------------------------
def demangle_all(symbols: Iterable[str]) -> Dict[str, str]:
    """Demangle Itanium symbols with one c++filt (or llvm-cxxfilt) process."""
    names = sorted({s for s in symbols if s})
    result = {s: s for s in names}
    mangled = [s for s in names if s.startswith("_Z")]
    tool = shutil.which("c++filt") or shutil.which("llvm-cxxfilt")
    if not mangled or not tool:
        return result
    try:
        proc = subprocess.run([tool], input="\n".join(mangled) + "\n", capture_output=True,
                              text=True, check=True, timeout=30)
        lines = proc.stdout.splitlines()
        if len(lines) == len(mangled):
            result.update(dict(zip(mangled, (line.strip() for line in lines))))
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def _collect_symbols(module: RawModule) -> Set[str]:
    symbols = {f.symbol for f in module.functions} | {g.symbol for g in module.globals}

    def walk(region):
        for block in region.blocks:
            for op in block.ops:
                if op.symbol:
                    symbols.add(op.symbol)
                for sub in op.regions:
                    walk(sub)

    for fn in module.functions:
        if fn.body:
            walk(fn.body)
    return symbols


def _signature(raw: RawFunction) -> str:
    params = ", ".join(t for _, t in raw.params)
    results = ", ".join(raw.resultTypes) or "()"
    return f"({params}) -> {results}"


def _is_main(fn: Function) -> bool:
    return fn.name == "main" or fn.demangledName in ("main", "main()")


# -----------------------------------------------------------------------------
# Program construction
# -----------------------------------------------------------------------------
class ProgramLinker:
    def __init__(self, program_id: str, name: Optional[str] = None,
                 entry_points: Optional[Iterable[str]] = None):
        self.program = Program(id=program_id, name=name or program_id)
        self.modules: Dict[str, RawModule] = {}
        self.entry_names = set(entry_points or ())
        self.names: Dict[str, str] = {}
        self.local_defs: Dict[str, Dict[str, Function]] = defaultdict(dict)
        self.external_defs: Dict[str, Function] = {}
        self.decls: Dict[str, RawFunction] = {}
        self.function_symbols: Dict[str, Set[str]] = defaultdict(set)  # tu -> symbols that are functions
        self.models: Dict[str, MemoryModel] = {}
        self.callees: Dict[str, List[Function]] = defaultdict(list)    # call op -> callees
        self.call_first_arg: Dict[str, Dict[str, int]] = defaultdict(dict)

    def demangle(self, symbol: str) -> str:
        return self.names.get(symbol, symbol)

    # -- step 1: translation units --------------------------------------------------
    def load(self, cir_paths: List[str]) -> Program:
        modules = [parse_cir_file(path) for path in cir_paths]
        symbols: Set[str] = set()
        for module in modules:
            symbols |= _collect_symbols(module)
        self.names = demangle_all(symbols)

        used_ids: Dict[str, int] = {}
        for module in modules:
            base = os.path.basename(module.sourcePath) or os.path.basename(module.path)
            count = used_ids.get(base, 0)
            used_ids[base] = count + 1
            cu = base if count == 0 else f"{base}~{count + 1}"
            tu = TranslationUnit(
                id=f"{self.program.id}:{cu}", compilationId=cu, sourceFile=module.sourcePath,
                cirFile=os.path.basename(module.path), programId=self.program.id,
                contentHash=module.contentHash, typeAliases=dict(module.typeAliases),
                attrAliases=dict(module.attrAliases))
            self.program.translationUnits.append(tu)
            self.modules[tu.id] = module

        for tu in self.program.translationUnits:
            self._load_tu(tu, self.modules[tu.id])
        self._load_globals()
        self._link_calls()
        self._memory_flows()
        self._analyze_call_graph()
        return self.program

    def _symbol(self, symbol: str, linkage: str, tu: Optional[TranslationUnit]) -> FunctionSymbol:
        is_static = linkage in STATIC_LINKAGES
        sid = (f"{self.program.id}:sym:{tu.compilationId}:{symbol}" if is_static and tu
               else f"{self.program.id}:sym:{symbol}")
        existing = self.program.symbols.get(sid)
        if existing is None:
            existing = FunctionSymbol(id=sid, name=symbol, demangledName=self.demangle(symbol),
                                      linkage=linkage, isStatic=is_static,
                                      tuId=tu.id if is_static and tu else None,
                                      programId=self.program.id)
            self.program.symbols[sid] = existing
        elif existing.linkage == "external" and linkage != "external" and not is_static:
            existing.linkage = linkage
        return existing

    def _load_tu(self, tu: TranslationUnit, module: RawModule) -> None:
        for raw in module.functions:
            self.function_symbols[tu.id].add(raw.symbol)
            if raw.is_declaration:
                self.decls.setdefault(raw.symbol, raw)
                continue
            linkage = raw.linkage
            symbol = self._symbol(raw.symbol, linkage, tu)
            points = module.locations.resolve(raw.loc)
            lines = [p.line for p in points]
            fn = Function(
                id=f"{tu.id}:{raw.symbol}", name=raw.symbol, demangledName=self.demangle(raw.symbol),
                symbolId=symbol.id, tuId=tu.id, sourceFile=tu.sourceFile, linkage=linkage,
                isDeclaration=False, isStatic=symbol.isStatic, signature=_signature(raw),
                returnType=raw.resultTypes[0] if raw.resultTypes else None,
                paramTypes=[t for _, t in raw.params],
                location=f"{points[0].file}:{points[0].line}:{points[0].col}" if points else None,
                startLine=min(lines) if lines else None, endLine=max(lines) if lines else None,
                programId=self.program.id)
            build_function(raw, module, fn, self.demangle)
            tu.functions.append(fn)
            self.local_defs[tu.id][raw.symbol] = fn
            if not fn.isStatic and raw.symbol not in self.external_defs:
                self.external_defs[raw.symbol] = fn

    def _extern_function(self, symbol: str) -> Function:
        fid = f"{self.program.id}:extern:{symbol}"
        fn = self.program.externFunctions.get(fid)
        if fn is None:
            raw = self.decls.get(symbol)
            linkage = raw.linkage if raw else "external"
            sym = self._symbol(symbol, linkage, None)
            fn = Function(
                id=fid, name=symbol, demangledName=self.demangle(symbol), symbolId=sym.id,
                tuId=None, sourceFile=None, linkage=linkage, isDeclaration=True, isStatic=False,
                signature=_signature(raw) if raw else "",
                returnType=raw.resultTypes[0] if raw and raw.resultTypes else None,
                paramTypes=[t for _, t in raw.params] if raw else [], programId=self.program.id)
            self.program.externFunctions[fid] = fn
        return fn

    def resolve_function(self, symbol: str, tu_id: str) -> Function:
        local = self.local_defs[tu_id].get(symbol)
        if local is not None:
            return local
        if symbol in self.external_defs:
            return self.external_defs[symbol]
        return self._extern_function(symbol)

    # -- step 2: globals --------------------------------------------------------------
    def _load_globals(self) -> None:
        program = self.program
        for tu in program.translationUnits:
            module = self.modules[tu.id]
            for raw in module.globals:
                is_static = raw.linkage in STATIC_LINKAGES
                gid = (f"{program.id}:gv:{tu.compilationId}:{raw.symbol}" if is_static
                       else f"{program.id}:gv:{raw.symbol}")
                points = module.locations.resolve(raw.loc)
                existing = program.globals.get(gid)
                if existing is None or (existing.isDeclaration and not raw.is_declaration):
                    program.globals[gid] = GlobalVariable(
                        id=gid, name=raw.symbol, demangledName=self.demangle(raw.symbol),
                        linkage=raw.linkage, isStatic=is_static,
                        isDeclaration=raw.is_declaration, isConstant=raw.is_constant,
                        type=raw.type, tuId=tu.id, sourceFile=tu.sourceFile,
                        line=points[0].line if points else None,
                        initializer=raw.initializer, programId=program.id)
                tu.globalRefs.append((gid, not raw.is_declaration))

    def resolve_global(self, symbol: str, tu: TranslationUnit) -> Optional[str]:
        local = f"{self.program.id}:gv:{tu.compilationId}:{symbol}"
        if local in self.program.globals:
            return local
        shared = f"{self.program.id}:gv:{symbol}"
        return shared if shared in self.program.globals else None

    # -- step 3: calls, bridges -----------------------------------------------------------
    def _function_targets(self, fn: Function, model: MemoryModel, vid: str,
                          seen: Optional[Set[str]] = None) -> List[str]:
        """Function symbols a function-pointer value may hold."""
        seen = seen if seen is not None else set()
        if vid in seen:
            return []
        seen.add(vid)
        op = model.def_op.get(vid)
        if op is None:
            return []
        if op.name == "cir.get_global" and op.globalSymbol in self.function_symbols[fn.tuId]:
            return [op.globalSymbol]
        if op.name == "cir.cast" and op.operands:
            return self._function_targets(fn, model, op.operands[0], seen)
        if op.name == "cir.load" and op.operands:
            container = model.slot_of(op.operands[0])
            targets: List[str] = []
            if container is not None:
                for store in model.stores:
                    if model.slot_of(store.operands[1]) == container:
                        for sym in self._function_targets(fn, model, store.operands[0], seen):
                            if sym not in targets:
                                targets.append(sym)
            return targets
        return []

    def _link_calls(self) -> None:
        program = self.program
        returns: Dict[str, List[Tuple[str, str]]] = {}
        for tu in program.translationUnits:
            for fn in tu.functions:
                self.models[fn.id] = MemoryModel(fn, self.demangle)
                returns[fn.id] = [(op.operands[0], op.id) for op in fn.operations
                                  if op.name == "cir.return" and op.operands]

        for tu in program.translationUnits:
            for fn in tu.functions:
                model = self.models[fn.id]
                model.annotate_names()
                for op in fn.operations:
                    if op.name == "cir.get_global" and op.globalSymbol:
                        if op.globalSymbol in self.function_symbols[tu.id]:
                            self.resolve_function(op.globalSymbol, tu.id).isAddressTaken = True
                        else:
                            gid = self.resolve_global(op.globalSymbol, tu)
                            if gid:
                                program.globalAccesses.append((op.id, gid))
                    if not op.isCall:
                        continue
                    if op.calleeSymbol:
                        targets = [(self.resolve_function(op.calleeSymbol, tu.id), True)]
                        first_arg = 0
                    else:
                        first_arg = 1
                        symbols = self._function_targets(fn, model, op.operands[0]) if op.operands else []
                        targets = [(self.resolve_function(s, tu.id), False) for s in symbols]
                        if len(targets) == 1:
                            op.calleeName = targets[0][0].demangledName
                    self.call_first_arg[fn.id][op.id] = first_arg
                    for callee, direct in targets:
                        self.callees[op.id].append(callee)
                        program.callEdges.append(CallEdge(
                            callerId=fn.id, calleeId=callee.id, callOpId=op.id, isDirect=direct,
                            isCrossTU=bool(callee.tuId) and callee.tuId != fn.tuId,
                            isExternal=callee.isDeclaration,
                            callSiteLine=op.line, callSiteCol=op.col))
                        if callee.isDeclaration:
                            continue
                        args = op.operands[first_arg:]
                        for index, (arg, param) in enumerate(zip(args, callee.parameters)):
                            program.interFlows.append(FlowEdge(
                                arg, param, "call_argument",
                                fn.values[arg].definingOpId if arg in fn.values else None,
                                op.id, index))
                        if op.results:
                            for ret_value, ret_op in returns.get(callee.id, []):
                                program.interFlows.append(FlowEdge(
                                    ret_value, op.results[0], "call_return", ret_op, op.id))

    # -- step 4: memory -----------------------------------------------------------------------
    def _memory_flows(self) -> None:
        functions = [fn for tu in self.program.translationUnits for fn in tu.functions]
        by_id = {fn.id: fn for fn in functions}

        def defined_callees(op_id: str) -> List[Function]:
            return [c for c in self.callees.get(op_id, []) if not c.isDeclaration]

        # Writes through pointer parameters, closed over calls that forward them.
        writes: Dict[str, Set[tuple]] = {
            fn.id: set(self.models[fn.id].direct_param_writes()) for fn in functions}
        ops_by_fn = {fn.id: {o.id: o for o in fn.operations} for fn in functions}
        changed = True
        while changed:
            changed = False
            for fn in functions:
                model = self.models[fn.id]
                for op_id, first in self.call_first_arg[fn.id].items():
                    for arg_index, slot in model.call_arg_slots(ops_by_fn[fn.id][op_id], first):
                        if slot[0][0] != "P":
                            continue
                        for callee in defined_callees(op_id):
                            for (pidx, suffix, value, store) in list(writes[callee.id]):
                                if pidx == arg_index:
                                    item = (slot[0][1], normalize_slot(slot + suffix)[1:], value, store)
                                    if item not in writes[fn.id]:
                                        writes[fn.id].add(item)
                                        changed = True

        call_defs: Dict[str, Dict[str, List[Def]]] = defaultdict(lambda: defaultdict(list))
        for fn in functions:
            model = self.models[fn.id]
            for op_id, first in self.call_first_arg[fn.id].items():
                for arg_index, slot in model.call_arg_slots(ops_by_fn[fn.id][op_id], first):
                    for callee in defined_callees(op_id):
                        for (pidx, suffix, value, store) in sorted(writes[callee.id], key=repr):
                            if pidx == arg_index:
                                call_defs[fn.id][op_id].append(
                                    Def(normalize_slot(slot + suffix), value, store, False))

        entry_defs: Dict[str, List[Def]] = defaultdict(list)
        results: Dict[str, List[FlowEdge]] = {}
        pending = deque(fn.id for fn in functions)
        queued = set(pending)
        rounds = 0
        while pending and rounds < 50 * max(1, len(functions)):
            rounds += 1
            fid = pending.popleft()
            queued.discard(fid)
            fn = by_id[fid]
            flows, call_in = self.models[fid].run(
                call_defs[fid], entry_defs[fid], self.call_first_arg[fid])
            results[fid] = flows
            for (op_id, arg_index), defs in call_in.items():
                for callee in defined_callees(op_id):
                    known = set(entry_defs[callee.id])
                    added = False
                    for d in defs:
                        new = Def(normalize_slot((("P", arg_index),) + d.slot),
                                  d.value, d.op, False)
                        if new not in known:
                            entry_defs[callee.id].append(new)
                            known.add(new)
                            added = True
                    if added and callee.id not in queued:
                        pending.append(callee.id)
                        queued.add(callee.id)

        if pending:
            raise RuntimeError("Inter-procedural memory analysis did not converge; "
                               "refusing to export incomplete flows")

        seen: Set[tuple] = set()
        for fn in functions:
            for flow in results.get(fn.id, []):
                key = (flow.src, flow.dst, flow.kind, flow.sourceOpId, flow.targetOpId)
                if key in seen:
                    continue
                seen.add(key)
                if flow.src in fn.values:
                    fn.flows.append(flow)
                else:
                    self.program.interFlows.append(flow)

    # -- step 5: call graph analyses -------------------------------------------------------------
    def _analyze_call_graph(self) -> None:
        program = self.program
        functions = program.all_functions()
        adjacency: Dict[str, List[str]] = defaultdict(list)
        callers: Dict[str, Set[str]] = defaultdict(set)
        for edge in program.callEdges:
            if edge.calleeId not in adjacency[edge.callerId]:
                adjacency[edge.callerId].append(edge.calleeId)
            callers[edge.calleeId].add(edge.callerId)
        for fn in functions:
            fn.outDegree = len(adjacency[fn.id])
            fn.inDegree = len(callers[fn.id])

        # Tarjan's strongly connected components (iterative).
        index: Dict[str, int] = {}
        low: Dict[str, int] = {}
        on_stack: Set[str] = set()
        stack: List[str] = []
        counter = 0
        sccs: List[List[str]] = []
        for root in (fn.id for fn in functions):
            if root in index:
                continue
            work = [(root, 0)]
            while work:
                node, child = work.pop()
                if child == 0:
                    index[node] = low[node] = counter
                    counter += 1
                    stack.append(node)
                    on_stack.add(node)
                recurse = False
                successors = adjacency[node]
                while child < len(successors):
                    succ = successors[child]
                    child += 1
                    if succ not in index:
                        work.append((node, child))
                        work.append((succ, 0))
                        recurse = True
                        break
                    if succ in on_stack:
                        low[node] = min(low[node], index[succ])
                if recurse:
                    continue
                if low[node] == index[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    sccs.append(component)
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])

        by_id = {fn.id: fn for fn in functions}
        for component in sccs:
            recursive = len(component) > 1 or component[0] in adjacency[component[0]]
            if recursive:
                order = {fn.id: i for i, fn in enumerate(functions)}
                component.sort(key=lambda f: order[f])
                program.recursionCycles.append(component)
                for member in component:
                    by_id[member].isRecursive = True
        program.recursionCycles.sort()

        defined = [fn for fn in functions if not fn.isDeclaration]
        for fn in defined:
            fn.isEntryPoint = _is_main(fn) or fn.name in self.entry_names or \
                fn.demangledName in self.entry_names
        entries = [fn.id for fn in defined if fn.isEntryPoint]
        if entries:
            reachable = set(entries)
            frontier = list(entries)
            while frontier:
                node = frontier.pop()
                for succ in adjacency[node]:
                    if succ not in reachable:
                        reachable.add(succ)
                        frontier.append(succ)
            for fn in defined:
                fn.isDeadCode = fn.id not in reachable and not fn.isAddressTaken
        else:
            for fn in defined:
                fn.isDeadCode = fn.isStatic and fn.inDegree == 0 and not fn.isAddressTaken


def build_program(cir_paths: List[str], program_id: str, name: Optional[str] = None,
                  entry_points: Optional[Iterable[str]] = None) -> Program:
    return ProgramLinker(program_id, name, entry_points).load(cir_paths)
