"""Memory data flow: store-to-load forwarding over the CFG.

Addresses are resolved to abstract *slots*:

    (("L", alloca_value_id),)      local variable (cir.alloca)
    (("G", symbol),)               global variable (cir.get_global)
    (("P", parameter_index),)      memory reached through a pointer parameter
    slot + (("f", index, name),)   struct member (cir.get_member)
    slot + (("e",),)               array element / pointer arithmetic (weak)

A may-reaching-definitions analysis runs on the lowered CFG, so a store in one
branch never reaches a load in the other branch, and loop-carried stores reach
loads in the next iteration through the back edge. Stores to a slot kill
earlier stores to that slot and its sub-slots; element stores and call effects
never kill.

Inter-procedural memory flow is summarized per pointer parameter:

* callee writes through parameter i are replayed at each call site as weak
  definitions of the caller's argument slot (e.g. `bump(int &r)` mutating `a`);
* definitions reaching a call's pointer argument become entry definitions of
  the callee's parameter slot (caller store -> callee load).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from .models import FlowEdge, Function, Operation

Slot = Tuple[tuple, ...]

# Keep the abstract domain finite when recursion forwards p + 1 or &p->field.
# A wildcard denotes every descendant below a retained prefix.
MAX_SLOT_PATH_DEPTH = 4
ANY_DESCENDANT = ("any",)


def normalize_slot(slot: Slot) -> Slot:
    for index, part in enumerate(slot):
        if part == ANY_DESCENDANT:
            slot = slot[:index + 1]
            break
    if len(slot) > MAX_SLOT_PATH_DEPTH + 1:
        return slot[:MAX_SLOT_PATH_DEPTH + 1] + (ANY_DESCENDANT,)
    return slot


@dataclass(frozen=True)
class Def:
    slot: Slot
    value: Optional[str]     # Value ID written (None: unknown contents)
    op: str                  # Operation ID of the writing store
    strong: bool


def _is_prefix(prefix: Slot, slot: Slot) -> bool:
    for index, part in enumerate(prefix):
        if part == ANY_DESCENDANT:
            return True
        if index >= len(slot) or part != slot[index]:
            return False
    return True


def _overlaps(a: Slot, b: Slot) -> bool:
    return _is_prefix(a, b) or _is_prefix(b, a)


def is_pointer(vtype: Optional[str]) -> bool:
    return bool(vtype) and vtype.startswith("!cir.ptr")


class _PendingSlot(Exception):
    def __init__(self, vid):
        self.vid = vid


class MemoryModel:
    """Slot resolution and reaching definitions for one function."""

    def __init__(self, fn: Function, demangle: Callable[[str], str]):
        self.fn = fn
        self.demangle = demangle
        self.def_op: Dict[str, Operation] = {
            vid: op for op in fn.operations for vid in op.results}
        self.param_index = {vid: i for i, vid in enumerate(fn.parameters)}
        self.stores = [op for op in fn.operations if op.name == "cir.store" and len(op.operands) >= 2]
        self._memo: Dict[str, Optional[Slot]] = {}
        self._resolving = False
        ops_by_id = {o.id: o for o in fn.operations}
        self.block_ops = {b.id: [ops_by_id[o] for o in b.operations] for b in fn.blocks}
        self.preds: Dict[str, List[str]] = {b.id: [] for b in fn.blocks}
        for edge in fn.cfgEdges:
            self.preds[edge.dst].append(edge.src)

    # -- slots ---------------------------------------------------------------------
    def slot_of(self, vid: str) -> Optional[Slot]:
        if vid in self._memo:
            return self._memo[vid]
        # Resolve dependencies with an explicit DFS stack. Large functions can
        # have more pointer dependencies than Python's call-stack limit.
        if self._resolving:
            raise _PendingSlot(vid)
        pending = [vid]
        self._memo[vid] = None  # preserve the existing pointer-cycle guard
        self._resolving = True
        try:
            while pending:
                current = pending[-1]
                try:
                    self._memo[current] = self._resolve(current)
                except _PendingSlot as dependency:
                    self._memo[dependency.vid] = None
                    pending.append(dependency.vid)
                else:
                    pending.pop()
        except BaseException:
            for current in pending:
                self._memo.pop(current, None)
            raise
        finally:
            self._resolving = False
        return self._memo[vid]

    def _resolve(self, vid: str) -> Optional[Slot]:
        value = self.fn.values.get(vid)
        if value is None:
            return None
        if vid in self.param_index:
            return (("P", self.param_index[vid]),) if is_pointer(value.type) else None
        op = self.def_op.get(vid)
        if op is None:
            return None
        name = op.name
        if name == "cir.alloca":
            return (("L", vid),)
        if name == "cir.get_global" and op.globalSymbol:
            return (("G", op.globalSymbol),)
        if name == "cir.get_member" and op.operands:
            base = self.slot_of(op.operands[0])
            index = re.search(r"\[(\d+)\]", op.text)
            member = re.search(r'name\s*=\s*"([^"]*)"', op.text)
            if base is None:
                return None
            return normalize_slot(base + (("f", int(index.group(1)) if index else -1,
                                           member.group(1) if member else None),))
        if name == "cir.cast" and op.operands:
            kind = re.match(r"^(?:%\S+ = )?cir\.cast\s*\(?\s*(\w+)", op.text)
            kind_name = kind.group(1) if kind else ""
            if kind_name in ("bitcast", "address_space"):
                return self.slot_of(op.operands[0])
            if kind_name == "array_to_ptrdecay":
                return self._element(self.slot_of(op.operands[0]))
            return None
        if name == "cir.ptr_stride" and op.operands:
            return self._element(self.slot_of(op.operands[0]))
        if name == "cir.load" and op.operands and is_pointer(value.type):
            # A pointer loaded from a slot aliases whatever address was stored
            # there, provided every store agrees (flow-insensitive points-to).
            container = self.slot_of(op.operands[0])
            if container is None:
                return None
            targets = set()
            for store in self.stores:
                if self.slot_of(store.operands[1]) == container:
                    targets.add(self.slot_of(store.operands[0]))
            # An unknown store is evidence against a single known target.
            # Dropping it would incorrectly turn a may-alias into a must-alias.
            if None not in targets and len(targets) == 1:
                return targets.pop()
        return None

    @staticmethod
    def _element(slot: Optional[Slot]) -> Optional[Slot]:
        if slot is None:
            return None
        return slot if slot[-1] in (("e",), ANY_DESCENDANT) else normalize_slot(slot + (("e",),))

    def slot_name(self, slot: Slot) -> str:
        head = slot[0]
        if head[0] == "L":
            value = self.fn.values[head[1]]
            text = value.varName or value.name
        elif head[0] == "G":
            text = self.demangle(head[1])
        else:
            pid = self.fn.parameters[head[1]]
            text = "*" + (self.fn.values[pid].varName or f"arg{head[1]}")
        for part in slot[1:]:
            if part[0] == "f":
                text += "." + (part[2] if part[2] else str(part[1]))
            elif part == ANY_DESCENDANT:
                text += ".*"
            else:
                text += "[]"
        return text

    @staticmethod
    def is_strong(slot: Slot) -> bool:
        return ("e",) not in slot and ANY_DESCENDANT not in slot

    # -- naming --------------------------------------------------------------------
    def annotate_names(self) -> None:
        """Propagate source variable names to parameters, loads and members."""
        for store in self.stores:
            data, addr = store.operands[0], store.operands[1]
            slot = self.slot_of(addr)
            if data in self.param_index and slot and slot[0][0] == "L" and len(slot) == 1:
                value = self.fn.values[data]
                if not value.varName:
                    value.varName = self.slot_name(slot)
        for op in self.fn.operations:
            if op.name in ("cir.load", "cir.get_member") and op.operands and op.results:
                slot = self.slot_of(op.operands[0] if op.name == "cir.load" else op.results[0])
                if slot is not None:
                    value = self.fn.values[op.results[0]]
                    value.varName = value.varName or self.slot_name(slot)

    # -- summaries -------------------------------------------------------------------
    def direct_param_writes(self) -> List[Tuple[int, Slot, Optional[str], str]]:
        """(parameter index, sub-slot path, written Value ID, store op) for writes via params."""
        writes = []
        for store in self.stores:
            slot = self.slot_of(store.operands[1])
            if slot and slot[0][0] == "P":
                writes.append((slot[0][1], slot[1:], store.operands[0], store.id))
        return writes

    def call_arg_slots(self, op: Operation, first_arg: int) -> List[Tuple[int, Slot]]:
        slots = []
        for index, vid in enumerate(op.operands[first_arg:]):
            slot = self.slot_of(vid)
            if slot is not None:
                slots.append((index, slot))
        return slots

    # -- reaching definitions ----------------------------------------------------------
    def run(self, call_defs: Dict[str, List[Def]], entry_defs: List[Def],
            call_args: Dict[str, int]):
        """Return (flows, call_in) where call_in maps (call op, arg index) to
        the definitions reaching that pointer argument."""
        defs: List[Def] = list(entry_defs)
        op_defs: Dict[str, List[int]] = {}
        for op in self.fn.operations:
            produced: List[Def] = []
            if op.name == "cir.store" and len(op.operands) >= 2:
                slot = self.slot_of(op.operands[1])
                if slot is not None:
                    produced.append(Def(slot, op.operands[0], op.id, self.is_strong(slot)))
            elif op.name == "cir.copy" and len(op.operands) >= 2:
                slot = self.slot_of(op.operands[1])
                if slot is not None:
                    produced.append(Def(slot, None, op.id, self.is_strong(slot)))
            produced.extend(call_defs.get(op.id, []))
            if produced:
                op_defs[op.id] = list(range(len(defs), len(defs) + len(produced)))
                defs.extend(produced)

        entry_ids = set(range(len(entry_defs)))

        def transfer(state: Set[int], op: Operation) -> Set[int]:
            for index in op_defs.get(op.id, ()):
                d = defs[index]
                if d.strong:
                    state = {i for i in state if not _is_prefix(d.slot, defs[i].slot)}
                state = state | {index}
            return state

        entry_block = next((b.id for b in self.fn.blocks if b.isEntry), None)
        order = [b.id for b in self.fn.blocks]
        out: Dict[str, Set[int]] = {b: set() for b in order}
        changed = True
        while changed:
            changed = False
            for bid in order:
                state: Set[int] = set(entry_ids) if bid == entry_block else set()
                for pred in self.preds[bid]:
                    state |= out[pred]
                for op in self.block_ops[bid]:
                    state = transfer(state, op)
                if state != out[bid]:
                    out[bid] = state
                    changed = True

        flows: List[FlowEdge] = []
        call_in: Dict[Tuple[str, int], List[Def]] = {}
        for bid in order:
            state = set(entry_ids) if bid == entry_block else set()
            for pred in self.preds[bid]:
                state |= out[pred]
            for op in self.block_ops[bid]:
                if op.name == "cir.load" and op.operands and op.results:
                    slot = self.slot_of(op.operands[0])
                    if slot is not None:
                        for index in sorted(state):
                            d = defs[index]
                            if d.value and _overlaps(d.slot, slot):
                                flows.append(FlowEdge(d.value, op.results[0], "store_to_load",
                                                      d.op, op.id))
                if op.id in call_args:
                    for arg_index, slot in self.call_arg_slots(op, call_args[op.id]):
                        reaching = [defs[i] for i in sorted(state) if _overlaps(defs[i].slot, slot)]
                        if reaching:
                            call_in[(op.id, arg_index)] = [
                                Def(d.slot[len(slot):] if _is_prefix(slot, d.slot) else (),
                                    d.value, d.op, False) for d in reaching]
                state = transfer(state, op)
        return flows, call_in
