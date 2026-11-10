# Known issues and limitations

cir-graph builds a **sequential may-dependence graph**. An edge means one thing
*may* influence another. The graph does not evaluate the program, prove path
feasibility, or give a soundness guarantee for all of C/C++. The items below are
open even when graph construction and the test suite succeed.

When you add a fix, add a small source/CIR regression that checks the actual
connection or counterexample (see `testbench/` and `tests/test_regressions.py`).
Construction success and graph size alone do not establish correctness.

Most of the evidence comes from an exploration of **cJSON 1.7.19** (commit
`6d9f2443`): the full `cJSON.c` plus a small caller, compared against the
source. Its report and artifacts are in the original `cir-analysis` research
repository.

## Quick reference

| Area | What to expect |
|---|---|
| Call graph | Direct calls, cross-TU binding, static scoping and recursion are reliable. Calls through function-pointer **parameters**, virtual calls and callbacks have no `CALLS` edge. |
| CFG | Faithful for `if`/`?:`/loops/`switch`/`goto`/`break`/`continue`. Exception control flow is approximate. |
| Data flow | `def_use`, local `store_to_load`, argument/return bindings, and writes through pointer/reference parameters work. Heap memory, globals shared between functions, and general points-to do not. |
| Dead code | Relative to `main` plus `--entry` names; a library API with no caller in the analyzed snapshot shows up as dead. |

## Analysis precision

| ID | Priority | Problem and evidence | What a fix must show |
|---|---|---|---|
| C01 | High | **No control dependence.** In cJSON, `cJSON_GetArraySize`'s list controls the iteration count and therefore the returned counter, but value-flow edges alone miss that influence. Null/type guards that select constant returns have no data edge from the guard. | A separate control-dependence relation backed by branch/loop evidence. A backward slice of the array-size return includes the loop condition and array input, without inventing scalar value forwarding. |
| C02 | High | **Pointers loaded from fields, linked structures, general points-to.** cJSON: `array.child = &first` is connected, but `first.next = &second` does not reach the callee's `child->next` load. | Model call-time pointer contents and multiple possible targets; recover the `next` connection, keep unknown alternatives, test loops/cycles and mixed known/unknown pointers. |
| C03 | High | **Heap allocation, deallocation and library contracts.** There are no allocation identities or lifetimes. `realloc`, `memcpy`/`memset`, string and I/O functions have no contracts. A `cJSON_free` hook call does not say which allocation dies. | Bounded allocation/lifetime and mod/ref contracts. Specialize callbacks only once they are proven; keep barriers for unknown hooks. |
| C04 | High | **Context-insensitive interprocedural flows.** Function contexts are merged, and memory flows do not record the producing or consuming call site. Two calls with different arguments can be confused. | Call/return-balanced traversal or context-sensitive slices; record call-site IDs on memory flows; include recursive examples. |
| C05 | Medium | **No must-write summaries; aggregate/field mixing.** Writes made inside a call are weak updates. cJSON's getter can still see the original aggregate-zero initialization although the setter overwrote `valuedouble`. Type-changing forwarding (aggregate store → scalar field load) is forwarded directly, not marked uncertain. | Track definitely-written regions on every returning path, including partial aggregate updates. Kill stale inputs only with that proof. Represent aggregate→field projection explicitly. |
| C06 | High | **Casts, offsets, arrays, unions and aliasing.** Pointer copies are flow-insensitive. Array elements use weak slots. Byte offsets and reinterpretation are not precise, and formal/formal or formal/global aliasing is not modeled. | Layout/offset-aware may-target sets and flow-sensitive pointer copies. Keep unknown alternatives; test union overlap, variable indices, casts and unknown writes. |
| C07 | High | **Exception and lifetime control flow.** `cir.try` handlers get `exception` edges from the try entry, and `cir.try_call` unwind edges are kept only in flat CIR. This does not model every throw, destructor, cleanup and unwind effect. | Check exceptional control/effects against compiler facts, including unwinding destructors and memory changed before a throw. |
| C08 | Medium | **Sequential semantics only.** Atomics are ordinary operations, and there is no synchronization or happens-before. | Define and test a concurrency model before answering ordering/race questions. `volatile` alone is not synchronization. |
| G01 | High | **Globals do not carry flow between functions.** In `void set(){ g = 5; } int get(){ return g; }`, the store in `set` has no `store_to_load` edge to the load in `get` (checked with this engine). `ACCESSES_GLOBAL` still records both accesses. | Global effect summaries propagated through calls, with tests for write-in-callee/read-in-caller and the reverse. |
| G02 | High | **Unknown memory is implicit.** Field reads of external memory (parameters, globals) have no input edge. Unknown calls such as `cJSON_free`'s hook keep `sideEffect = all` on the operation, but no "unknown memory" value or clobber edge exists. A missing edge therefore does not mean independence. | Explicit typed external/uninitialized/unknown memory values. Unknown calls and unresolved writes reach escaped storage, and private spills are protected. |
| G03 | Medium | **No per-function effect summaries.** There are no may-read/may-write sets on functions or operations to answer "what can this call modify?" | Compact recursive may-read/may-write summaries that propagate parameter and global effects through calls. |

## Front end and linking

| ID | Priority | Boundary | Follow-up |
|---|---|---|---|
| V401 | High | **Text front end, not MLIR verification.** The parser follows the canonical CIR printer layout. Hand-formatted CIR that splits one operation header across lines (some `clang/test/CIR` lit files) can lose that operation's regions. Unmodeled module-level operations are skipped silently, and printer changes in newer clang versions can break parsing. Unknown `goto`/block targets do raise an error. | Report unmodeled top-level text; differentially check structured lowering against verified flattened CIR; re-run `testbench/regenerate.sh` and the suite when upgrading clang. |
| V402 | Medium | **Duplicate external definitions bind to the first one.** If two TUs both define `int f(void)`, every call binds silently to the first TU's body (checked with this engine). Repeated inline/ODR definitions are fine, but genuine conflicts are not reported. Arity/ABI/variadic compatibility is not checked. | Represent compatible candidates compactly and fail on (or flag) conflicting strong definitions; never silently pick the first body. `DiskGraph` already marks such symbols ambiguous. |
| V403 | Medium | **Summary depth and convergence.** Recursive field paths widen to a wildcard after four member/element steps (`analysisLimits` in JSON). If the interprocedural worklist reaches its iteration limit, generation fails explicitly. A completed run does not prove alias-analysis completeness. | Exercise recursive object shapes and replace the worklist guard with a measured convergence policy. |
| V408 | Low | **Dead code is relative.** Reachability depends on the chosen entry points and modeled targets. An API unused by a small caller is not unused by every application. | Use `--entry` for library entry points; keep snapshot reachability separate from whole-library dead-code claims. |
| L01 | Medium | **Indirect calls.** A function pointer resolves only when it traces back to `cir.get_global @f` within the same function (directly or through a local slot). Pointers passed as parameters (testbench case 16), virtual calls and callbacks give a call operation with `isCall` but no `CALLS`/`INVOKES` edge. Their targets are still `isAddressTaken`, so they are not reported as dead. | Points-to for function pointers (C02/C06), plus vtable resolution for C++. |
| L03 | Medium | <a id="l03"></a>**Language coverage.** With the pinned clang, these all compile to CIR and build a graph: virtual dispatch, exceptions, lambdas, `std::map`/`std::vector`/`std::function`, iostream and coroutines (C++); varargs, computed `goto`, `_Atomic`, `_Thread_local`, inline asm, `__int128`, `_Complex`, VLAs, bitfields, unions and `setjmp`/`longjmp` (C). That shows they are *accepted*; it does not show their semantics are complete (exceptions are approximate, see C07; atomics are sequential, see C08). Upstream ClangIR can still reject rarer constructs with a "not yet implemented" (NYI) error, and `analyze.sh` stops at the first TU that fails. C++ graphs also contain the standard-library template instances a TU uses, so expect many more functions than the source shows. | Analyze a failing TU separately or exclude it; add a testbench case for any construct whose graph matters to you; upgrade the pinned clang when upstream support lands. |
| L02 | Low | **Demangling needs `c++filt` or `llvm-cxxfilt`.** Without either tool, C++ names stay mangled. | Install binutils (included in `environment.yml`). |

## Storage, database and tooling

| ID | Priority | Boundary | Follow-up |
|---|---|---|---|
| V407 | Medium | <a id="v407"></a>**Kùzu 0.11.3 aggregation discrepancy.** On the cJSON database, `sum(CASE WHEN f.isDeclaration THEN 1 ELSE 0 END)` reported 8 external functions, while a filtered `count` returned 12, matching the node list and JSON. `cir-graph-query summary` now uses separate filtered counts, but the runtime behavior itself is unexplained. | Prefer simple filtered counts; check aggregates against the JSON before trusting them; re-test on newer Kùzu releases. |
| D01 | Medium | **Disk-store route boundaries.** `DiskGraph` (per-TU JSON in SQLite) does not propagate pointer-write memory summaries between TUs, and does not recompute project-wide recursion/reachability. This differs from the in-memory linker. | Document per-query which builder produced a graph; add cross-TU summary propagation if needed. |
| D02 | Low | **Schema changes need `--reset`.** Importing into a database built with an older schema fails with a raw Kùzu error (the import transaction is rolled back) rather than a clear version message; `--reset` deletes *every* program in that database. | Check `Program.schemaVersion` before importing and refuse mismatches up front. |
| D03 | Low | **Large-graph exports.** `graph.json` for a ~10k-operation TU (cJSON) is about 12 MB, and Graphviz rendering of `graph.dot` becomes impractical. | Use `--no-svg`; give LLMs selected layers (`summary` + `macro`, then `meso`/`micro` per function) rather than the whole JSON. |

## What the public-API metadata does not do

`cir_graph.public_api.*` exports public declarations from Clang's JSON AST. It
does not evaluate macro expressions, expand templates or inherited C++ members,
or prove that a declared function has a body in the compiled graph.
Function-like macros are listed by name and parameters only.
