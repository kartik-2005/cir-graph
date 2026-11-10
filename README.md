# cir-graph

**Whole-program knowledge graphs from Clang IR.** cir-graph compiles C and C++ to
[ClangIR (CIR)](https://llvm.github.io/clangir/) and builds **one layered graph**
from it: call graph, control flow and data flow. The graph is stored in an
embedded [Kùzu](https://kuzudb.com) database, and is also written as Cypher,
JSON and Graphviz.

```
 MACRO    Program → TranslationUnit → Function ─CALLS→ Function        (call graph, cross-TU)
            │ HAS_BLOCK
 MESO     BasicBlock ─CFG_NEXT{true|false|switch|…, isBackEdge}→ BasicBlock   (control flow)
            │ CONTAINS_OPERATION
 MICRO    Operation ─DEFINES/USES_VALUE→ Value ─FLOWS_TO{def_use|store_to_load|…}→ Value
 BRIDGES  call site ─INVOKES→ callee · argument → parameter · return → call result · globals
```

Every layer refers to the same operations and values. A query (or an LLM) can go
from "who calls `parse`?" down to "which store does this load observe?" in one
graph.

* **Pure Python engine.** It reads CIR as text, so building a graph from `.cir`
  files needs only Python and `kuzu`. No LLVM build is required.
* **Multi-TU linking.** Static symbols stay scoped to their TU, `extern`
  declarations bind across TUs, and recursion cycles and dead code are detected.
* **Data flow across calls.** It tracks def-use chains, store-to-load forwarding,
  argument/return bindings, and writes through pointer and reference parameters.
* **Outputs:** a Kùzu database, an idempotent Cypher script, layered JSON for LLM
  prompts, and DOT/SVG.

## 60-second quickstart (no LLVM needed)

```sh
git clone <this-repo> cir-graph && cd cir-graph
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                       # installs kuzu==0.11.3 and the cir-graph commands

# Build a graph from pre-compiled CIR (a three-TU program from the testbench)
./analyze.sh testbench/cases/10_three_tu_chain

# Query it
cir-graph-query summary
cir-graph-query calls --cross-tu
cir-graph-query callsites first
```

`analyze.sh` writes `artifacts/graph.kuzu`, `graph.cypher`, `graph.json`,
`graph.dot`, and `graph.svg` (if Graphviz is installed).

## Analyzing your own C/C++ code

Compiling C/C++ to CIR needs a clang built with CIR enabled; see the section
[below](#building-a-cir-enabled-clang). Once you have one:

```sh
export CLANG=/path/to/llvm-project/build/bin/clang
./analyze.sh examples/demo/main.cpp examples/demo/math.cpp --program-id demo
./analyze.sh examples/demo_c/main.c examples/demo_c/stack.c --program-id stack
./analyze.sh -I include src/*.c --program-id mylib --no-svg
```

* Each source file is compiled separately, one translation unit each, and then
  all TUs are linked.
* `analyze.sh` checks that the compiler really supports `-emit-cir`. A stock
  system clang is rejected with a clear message.
* If you already have `.cir` files (for example, made on another machine that
  has the compiler), pass them directly; no clang is needed.

```text
[+] Program 'demo': 2 TU(s), 8 defined + 0 external function(s)
[+] Macro: 7 call edge(s) (3 cross-TU), 1 recursion cycle(s)
[+] Meso:  29 basic block(s), 28 CFG edge(s)
[+] Micro: 157 operation(s), 113 value(s), 97 data-flow edge(s)
[+] Entry points: ['main']
[!] Dead code: ['never_called(int)']
```

Then explore it:

```sh
cir-graph-query cfg checked_sum                  # blocks and CFG edges
cir-graph-query trace main --var total           # where does `total` come from?
cir-graph-query cypher "MATCH (f:Function) WHERE f.isRecursive RETURN f.demangledName"
```

More queries are in [`examples/queries.cypher`](examples/queries.cypher). For
browsing the graph visually in Kùzu Explorer, see
[docs/USAGE.md](docs/USAGE.md#kùzu-explorer).

## Command reference

| Command | Purpose |
|---|---|
| `./analyze.sh [opts] <sources/.cir/dirs>` | one shot: compile (if needed) → graph → DB, Cypher, JSON, DOT, SVG |
| `cir-graph <.cir…> [--database] [--output] [--json] [--dot]` | build a graph from CIR files |
| `cir-graph-query <command> [target]` | `summary`, `functions`, `calls`, `cycles`, `dead`, `entry`, `cfg`, `ops`, `flows`, `callsites`, `trace`, `cypher` |
| `cir-graph-load <script.cypher> --database DB` | load a written Cypher script into a database |

Without `pip install`, use `PYTHONPATH=src python3 -m cir_graph.cli` (or
`.query`, `.load`). The full option lists are in
[docs/USAGE.md](docs/USAGE.md).

## Requirements

| Needed for | Requirement |
|---|---|
| everything | Python ≥ 3.9 (tested with 3.12) |
| database and queries | `kuzu==0.11.3` (`pip install -r requirements.txt`) |
| readable C++ names | `c++filt` or `llvm-cxxfilt` (binutils) |
| `graph.svg` (optional) | Graphviz `dot` |
| compiling C/C++ (optional) | a CIR-enabled clang (`CLANG=…`) |

A conda environment with all of the above except clang:
`conda env create -f environment.yml`.

## Building a CIR-enabled clang

CIR is part of upstream LLVM but is **off by default**, and prebuilt clang
packages are generally built without it. A one-time build of clang alone takes roughly
30–90 minutes, depending on cores and RAM:

```sh
git clone --depth 1 https://github.com/llvm/llvm-project.git
cmake -S llvm-project/llvm -B llvm-project/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_ENABLE_PROJECTS="clang;mlir" -DCLANG_ENABLE_CIR=ON \
  -DLLVM_TARGETS_TO_BUILD=host
ninja -C llvm-project/build clang
export CLANG=$PWD/llvm-project/build/bin/clang
```

* The parser is tested against the compiler recorded in
  [`testbench/CLANG_VERSION`](testbench/CLANG_VERSION). CIR's text format still
  changes upstream, so check out that commit if a newer one fails to parse.
* To share the cost, build clang once (on a workstation, a CI job or a container
  image) and hand `.cir` files or the `clang` binary to everyone else. cir-graph
  itself never needs LLVM.

## Tests

```sh
python3 -m unittest discover -s tests -v                 # 78 tests, no LLVM needed
CLANG=/path/to/clang python3 -m unittest discover -s tests   # also runs the source→graph tests
```

* The testbench has 16 self-checking C/C++ programs with their CIR committed
  alongside; see [testbench/README.md](testbench/README.md).
* Without `CLANG`, the two source-compilation tests are skipped. Without `kuzu`,
  the database tests are skipped.

## Repository layout

```
analyze.sh            one-shot runner
src/cir_graph/        engine: parser, CFG/DFG builder, memory model, linker, Kùzu schema,
                      exporters, CLI, query tool; public_api/ for optional declaration metadata
tests/                unit and regression tests (+ fixtures/stress)
testbench/            16 self-checking C/C++ cases with committed CIR, regenerate.sh
examples/             C++ demo (2 TUs), C demo (2 TUs) and example Cypher queries
docs/                 ARCHITECTURE.md · USAGE.md · KNOWN_ISSUES.md
```

## Limitations

cir-graph is a **may-dependence** graph for program understanding. It is not a
verifier.

* It has no control-dependence edges.
* It does not model heap memory or general points-to, and globals do not carry
  data flow between functions.
* Calls through function-pointer parameters and virtual calls have no call edge.
* Interprocedural flow is context-insensitive.

See [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) before relying on a missing edge
as proof of independence.
