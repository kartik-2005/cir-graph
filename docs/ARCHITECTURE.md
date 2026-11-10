# Architecture

cir-graph turns Clang IR (CIR) translation units into **one layered knowledge
graph**. An LLM, or a person writing Cypher, can move from whole-program structure
down to a single SSA value without switching tools. Every layer refers to the same
operations and values.

```
 MACRO  (inter-procedural)   Program ─CONTAINS_TU→ TranslationUnit ─CONTAINS_FUNCTION→ Function
                             Function ─CALLS{isCrossTU,isDirect,callOpId}→ Function
                             Function ─REPRESENTS_SYMBOL→ FunctionSymbol
                             TranslationUnit ─CONTAINS_GLOBAL→ GlobalVariable
                                   │ HAS_BLOCK
 MESO   (intra-procedural)   BasicBlock ─CFG_NEXT{kind: true|false|unconditional|switch|exception,
                                                  isBackEdge, conditionValueId}→ BasicBlock
                                   │ CONTAINS_OPERATION
 MICRO  (instructions/data)  Operation ─DEFINES_VALUE / USES_VALUE→ Value
                             Value ─FLOWS_TO{kind: def_use|store_to_load|block_arg|region_yield|
                                             call_argument|call_return}→ Value
 BRIDGES                     Operation ─INVOKES→ Function        (call site → callee)
                             caller arg ─FLOWS_TO{call_argument}→ callee parameter (HAS_PARAMETER)
                             callee return ─FLOWS_TO{call_return}→ caller call result
                             Operation ─ACCESSES_GLOBAL→ GlobalVariable
```

The engine is pure Python and reads CIR as **text**. It does not use the MLIR C++
API or Python bindings, so building a graph from `.cir` files needs only Python. A
CIR-enabled clang is needed only to produce `.cir` from C/C++ sources.

## Pipeline

```
 .c / .cpp ──clang -emit-cir -clangir-disable-passes──▶ .cir (one per TU)
                                                          │
             cir_parser ── op tree per TU ────────────────┘
                 │
             graph_builder ── per function: blocks, SSA values, def-use
                 │
             memory ───────── slots, reaching definitions, pointer summaries
                 │
             linker ───────── symbols across TUs, calls, bridges, SCCs, dead code
                 │
     ┌───────────┼────────────┬─────────────┐
   schema      exporters    exporters     cli / load
  (Kùzu DDL,   (JSON)       (DOT)         (Kùzu import)
   Cypher)
```

## Source layout

```
src/cir_graph/
  cli.py                    `cir-graph`: .cir files -> Kùzu / Cypher / JSON / DOT
  query.py                  `cir-graph-query`: layered queries over a database
  load.py                   `cir-graph-load`: import a written Cypher script
  cir_parser.py             CIR/MLIR text -> op tree (module/func/region/block/op), #loc table
  graph_builder.py          per function: structured regions -> basic blocks, SSA values, def-use
  memory.py                 address slots, CFG reaching definitions, pointer-parameter summaries
  linker.py                 multi-TU symbols, call resolution, bridges, Tarjan SCC, dead code
  models.py                 dataclasses: Program, TranslationUnit, FunctionSymbol, Function,
                            BasicBlock, Operation, Value, CFGEdge, FlowEdge, CallEdge, GlobalVariable
  schema.py                 Kùzu DDL, rows, idempotent batched Cypher
  exporters.py              layered JSON and Graphviz DOT
  project_builder.py        large-project entry point (per-TU building, switch normalization)
  disk_store.py             SQLite index with compressed per-function records (bounded memory)
  external_type_aliases.py  exact compressed sidecars for oversized CIR type aliases
  public_api/               optional public-declaration metadata from Clang's JSON AST
    base.py                 C headers, selected by name prefix
    cpp.py                  selected C++ types (public members, overloads, enums)
    c_project.py            configured C projects (function-like macros, prefixes)
```

## 1. Parsing (`cir_parser.py`)

The parser follows the MLIR printer's layout. An operation starts a line, and a
region opens with a trailing `{`. A closing `}` is followed either by the next
region (`} else {`, `}, false {`, `} do {`, `} catch all (%eh : …) {`) or by the
rest of the owning operation (`}) : (!cir.bool) -> !s32i loc(#loc21)`).
Region-carrying operations therefore keep their nested operations; nothing is
flattened into one "logical line". This keeps calls inside `cir.ternary` regions
(short-circuit `&&`/`||`, `?:`) visible. Locations are resolved through `#locN`
aliases, `fused[...]` and inline `loc("file":l:c)`.

## 2. Meso layer: structured CIR to a CFG (`graph_builder.py`)

`clang -Xclang -emit-cir -Xclang -clangir-disable-passes` emits *structured* CIR
with no `^bb` blocks. The builder lowers the region tree the way CIR's
`FlattenCFG` pass does, and names blocks after Clang's conventions:

| CIR | Blocks and edges |
|---|---|
| `cir.scope`, unknown region ops | inlined into the current block |
| `cir.if` | head `-true→ if.then`, `-false→ if.else` (or `if.end`), branches → `if.end` |
| `cir.ternary` | head `-true→ ternary.true`, `-false→ ternary.false` → `ternary.end`; yields flow into the result (`region_yield`) |
| `cir.while` | head → `while.cond -true→ while.body →(back edge) while.cond`, `-false→ while.end` |
| `cir.do` | head → `do.body → do.cond -true→(back edge) do.body`, `-false→ do.end` |
| `cir.for` | head → `for.cond -true→ for.body → for.inc →(back edge) for.cond`, `-false→ for.end` |
| `cir.switch` / `cir.case` | head `-switch {label}→ switch.case / switch.default`; `cir.yield` falls through to the next case, `cir.break` → `switch.end` |
| `cir.break` / `cir.continue` | innermost loop/switch exit, or the loop's `cond`/`inc` block |
| `cir.goto` / `cir.label` | edge to the named label; unknown targets are an error |
| `cir.return`, `cir.trap`, `cir.unreachable` | `isExit` block |

Already-flattened CIR (`^bb` blocks with `cir.br`, `cir.brcond`, `cir.switch.flat`)
is used as is, and branch operands flow into block arguments (`block_arg`).
Every operation belongs to exactly one block. Ordinals follow source order. Back
edges are found by DFS (`isBackEdge`). Loop headers are created before their
bodies, so a back edge always targets a lower ordinal. Unreachable code (for
example, the `cir.trap` after an `if` that returns on both paths) keeps its own
block with no predecessor.

## 3. Micro layer: values and data flow (`graph_builder.py`, `memory.py`)

* **Values** are operation results and block arguments. Function parameters are
  the entry block's arguments (`kind: parameter`). IDs are scoped per function.
  MLIR reuses SSA names in sibling regions (`%5` in both arms of an `if`), so
  repeated names get a suffix (`#%5`, `#%5.1`). Each value records `type`,
  `varName` (from `cir.alloca`, parameter spills, loads and members) and, for
  constants, `constValue`.
* **`def_use`**: an operand flows into the result of a computing op (`cir.cmp`,
  `cir.add`, `cir.cast`, `cir.select`, `cir.get_member`, …). Memory, control and
  call ops use the kinds below instead.
* **`store_to_load`**: a may-reaching-definitions analysis on the lowered CFG.
  Addresses resolve to *slots*: locals, globals, struct members, array elements
  (weak updates), and memory reached through pointer parameters. A store in one
  branch never reaches a load in the other branch. A store in a loop body reaches
  the condition through the back edge.
* **Inter-procedural memory**: writes through a pointer or reference parameter
  (`void scale(int &v)`) are replayed at the call site, so the caller's later loads
  see the callee's store. Stores that reach a pointer argument become entry
  definitions of the callee's parameter. Both appear as `store_to_load` with
  `isInterprocedural = true`.
* **Bounds**: member/element paths keep up to four steps after the root
  (`MAX_SLOT_PATH_DEPTH`). Deeper paths use a wildcard with weak updates, and
  this is reported in JSON `analysisLimits`. If the inter-procedural work queue
  hits its iteration limit, generation fails rather than exporting partial flows.

## 4. Macro layer and linking (`linker.py`)

* **Static symbols**: `internal`/`private` functions and globals get TU-scoped IDs
  (`prog:sym:left.cpp:_ZL6offseti` vs `prog:sym:right.cpp:_ZL6offseti`), so they
  never merge. A call binds first to a definition in its own TU.
* **Declarations**: a header `extern` declaration binds to the externally visible
  definition in another TU (`isCrossTU = true`). No separate declaration node is
  created. Calls to symbols that are never defined share one `isDeclaration`
  Function (`prog:extern:<symbol>`), and the call is marked `isExternal`.
* **Indirect calls**: a function pointer whose value comes from
  `cir.get_global @f` in the same function (directly or through a local slot)
  resolves to `f`, giving a `CALLS {isDirect: false}` edge. Such functions are
  `isAddressTaken`. Pointers passed in as parameters stay unresolved.
* **Analyses**: Tarjan SCCs give `isRecursive` and `recursionCycles` (self and
  mutual recursion). Entry points are `main` plus `--entry` names. *Dead code*
  is a defined function that is unreachable from any entry point and not
  address-taken.
* **Demangling** runs one `c++filt` (or `llvm-cxxfilt`) process per program; without either tool, names stay mangled.

## Identity scheme

All IDs are deterministic and prefixed with the program ID, so several programs
can share one database and re-imports are idempotent.

| Node | ID |
|---|---|
| Program | `demo` |
| TranslationUnit | `demo:main.cpp` (`compilationId` = source basename; duplicates get `~2`) |
| FunctionSymbol | `demo:sym:<mangled>`; static: `demo:sym:<tu>:<mangled>` |
| Function | `demo:<tu>:<mangled>`; unresolved external: `demo:extern:<mangled>` |
| BasicBlock / Operation / Value | `<functionId>#bb<ordinal>` / `#op<seq>` / `#%<ssa>[.<k>]` |
| GlobalVariable | `demo:gv:<mangled>`; static: `demo:gv:<tu>:<mangled>` |

## Kùzu schema (schema version `1.0`)

Every node has `programId`.

| Node table | Key properties |
|---|---|
| `Program` | `id, name, schemaVersion` |
| `TranslationUnit` | `id, compilationId, sourceFile, cirFile, contentHash`, `typeAliases`/`attrAliases` (JSON strings) |
| `FunctionSymbol` | `id, name, demangledName, linkage, isStatic, tuId` |
| `Function` | `id, name, demangledName, sourceFile, tuId, isDeclaration, isStatic, isEntryPoint, isRecursive, isDeadCode, isAddressTaken, linkage, signature, returnType, paramTypes, inDegree, outDegree, blockCount, startLine, endLine, location` |
| `BasicBlock` | `id, name, ordinal, functionId, isEntry, isExit, terminatorOp, operationCount` |
| `Operation` | `id, name, sequenceIndex, blockId, functionId, location, line, col, isCall, calleeSymbol, calleeName, sideEffect, text, isTerminator, parentOpId, regionIndex` |
| `Value` | `id, name, type, functionId, varName, isBlockArg, kind, definingOpId, constValue` |
| `GlobalVariable` | `id, name, demangledName, linkage, isStatic, isDeclaration, isConstant, type, tuId, sourceFile, line, initializer` |

| Rel table | From → To | Properties |
|---|---|---|
| `CONTAINS_TU` | Program → TranslationUnit | |
| `CONTAINS_FUNCTION` | TranslationUnit → Function | (definitions only) |
| `REPRESENTS_SYMBOL` | Function → FunctionSymbol | |
| `HAS_BLOCK` | Function → BasicBlock | |
| `CONTAINS_OPERATION` | BasicBlock → Operation | `index` |
| `DEFINES_VALUE` / `USES_VALUE` | Operation → Value | `resultIndex` / `operandIndex` |
| `CALLS` | Function → Function | `isCrossTU, callSiteLine, callSiteCol, isDirect, callOpId, isExternal` |
| `CFG_NEXT` | BasicBlock → BasicBlock | `kind, conditionValueId, isBackEdge, label, successorIndex, terminatorOpId` |
| `FLOWS_TO` | Value → Value | `kind, sourceOpId, targetOpId, argIndex, isInterprocedural` |
| `INVOKES` | Operation → Function | `isDirect, isCrossTU` (call-site bridge) |
| `HAS_PARAMETER` | Function → Value | `paramIndex` |
| `HAS_ARGUMENT` | BasicBlock → Value | `argIndex` |
| `CONTAINS_GLOBAL` | TranslationUnit → GlobalVariable | `isDefinition` |
| `ACCESSES_GLOBAL` | Operation → GlobalVariable | |

`FLOWS_TO.kind = region_yield` connects a structured `cir.yield` to the result of
`cir.ternary`/`cir.scope`; it is the structured counterpart of `block_arg`.

The Cypher import script contains the schema DDL, a program-scoped delete, and
batched `UNWIND … MERGE/CREATE` statements. Re-importing a program replaces only
that program's nodes. After a schema change, rebuild with `--reset` (this deletes
every program in that database).

## JSON export

```jsonc
{
  "schemaVersion": "1.0",
  "program": {"id": "demo", "name": "demo"},
  "summary": {"translationUnits": 2, "crossTUCalls": 3, "entryPoints": ["main"],
              "deadCode": ["never_called(int)"], "recursionCycles": [["fact(int)"]],
              "flowsByKind": {...}, ...},
  "layers": {
    "macro":   {"translationUnits": [...], "functions": [...], "globals": [...],
                "calls": [...], "recursionCycles": [...]},
    "meso":    {"<functionId>": {"entryBlock": "...", "blocks": [{"name": "for.cond",
                "operations": [...], "successors": [{"to": "...", "kind": "true"}]}]}},
    "micro":   {"<functionId>": {"parameters": [...], "operations": [...],
                "values": [...], "flows": [...]}},
    "bridges": {"callSites": [{"callOp": "...", "callee": "...", "argumentBindings": [...],
                "returnBindings": [...]}], "interproceduralFlows": [...], "globalAccesses": [...]}
  }
}
```

Meso and micro data are keyed by function ID, so a prompt can be built one layer
at a time. Start with `summary` and `macro`. Add `meso[f]` and `micro[f]` only for
the functions the question is about. Follow a value across a call with
`bridges.callSites`. Empty and null fields are omitted to save tokens.

JSON operation `operands` and Kùzu `USES_VALUE` include values passed to successor
blocks; `successorOperands` groups those by successor. In the Python model,
`Operation.operands` holds ordinary operands, and `Operation.all_operands` also
includes successor arguments.

## Large projects

The monolithic builder (`linker.build_program`) holds the whole program in
memory. For project-sized inputs:

* `project_builder.build_program` is the production entry point. It keeps the
  same analyses and normalizes CIR switches that contain pure, argument-free
  forwarding blocks before their case containers. Unsupported control flow fails
  explicitly.
* `disk_store.DiskGraph` is a SQLite index of compressed per-function CFG/DFG
  records built from per-TU JSON exports. Store each TU under its own program
  namespace, then call `link()`. The symbol pass resolves same-TU calls first,
  then unique external definitions. Internal-linkage functions stay TU-scoped,
  and multiple strong external definitions are marked ambiguous rather than bound
  arbitrarily. `find`, `body`, `relations` and `bindings` load only the selected
  function bodies.
  **Boundary:** pointer-write memory summaries are *not* propagated between TUs in
  this route, and project-wide recursion/reachability is not recomputed.
* `external_type_aliases.prepare` moves oversized `!alias = ...` type definitions
  into exact compressed sidecars, so huge C++ TUs can be parsed without expanding
  the full file in memory.
