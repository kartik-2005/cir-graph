# Usage

* [Docker: cir-graph-docker](#docker-cir-graph-docker)
* [analyze.sh: one-shot runner](#analyzesh-one-shot-runner)
* [cir-graph: build a graph from .cir files](#cir-graph-build-a-graph-from-cir-files)
* [Producing CIR yourself](#producing-cir-yourself)
* [cir-graph-query: layered queries](#cir-graph-query-layered-queries)
* [Cypher by layer](#cypher-by-layer)
* [Kùzu Explorer](#kùzu-explorer)
* [Python API](#python-api)
* [Optional public-API metadata](#optional-public-api-metadata)

All commands below run from the repository root. Without `pip install -e .`,
replace `cir-graph …` with `PYTHONPATH=src python3 -m cir_graph.cli …` (and
likewise `cir_graph.query`, `cir_graph.load`).

## Docker: cir-graph-docker

`cir-graph-docker` runs everything inside an image that contains cir-graph, a
CIR-enabled clang, C/C++ system headers, `c++filt` and Graphviz.

```sh
cir-graph-docker build                                 # once; compiles clang from source (1-3 h)
cir-graph-docker build --prebuilt ~/llvm-project/build # or reuse an existing CIR build (minutes)

cd ~/my-project
cir-graph-docker src/*.c -I include --program-id mylib # same options as analyze.sh
cir-graph-docker query summary                         # cir-graph-query on ./artifacts/graph.kuzu
cir-graph-docker query cfg main
cir-graph-docker cir build/*.cir --json out.json       # cir-graph on existing .cir files
cir-graph-docker load out.cypher --database other.kuzu # cir-graph-load
cir-graph-docker shell                                 # bash in the image
```

* The current directory is mounted (read-write) at the **same path** inside the
  container, and outputs must be written under it.
* Inputs elsewhere, such as `../lib/util.c` or `-I ../shared/include`, are
  mounted **read-only** automatically. Symlinks are resolved first.
* The container runs as your user, so outputs are owned by you.
* Graphs are byte-identical to a native run with the same clang.
* `CIR_GRAPH_IMAGE` selects another image. If the name includes a registry host
  (it contains a `/`), the image is pulled when missing.
* The image build runs the whole test suite against its own clang, so a broken
  image fails to build instead of producing wrong graphs.

Publishing the image once saves everyone else the build:

```sh
IMAGE=registry.example.com/cir-graph:latest     # any registry your users can pull from
docker tag cir-graph:latest "$IMAGE" && docker push "$IMAGE"
# users then run:  CIR_GRAPH_IMAGE=$IMAGE cir-graph-docker src/*.c
```

## analyze.sh: one-shot runner

```sh
./analyze.sh [options] <file.c|.cc|.cpp|.cxx|.cir|DIR> [more ...]
```

* Each C/C++ source is compiled **separately** (one translation unit each) to raw
  structured CIR. `.cir` inputs are used as they are, and you can mix both.
* A directory argument expands to the `.cir` files directly inside it. If it has
  none, it expands to its C/C++ sources. To recompile a directory that also holds
  `.cir` files, pass the sources explicitly (`dir/*.cpp`).
* All TUs are linked into one program graph.

| Option | Meaning |
|---|---|
| `--program-id ID` | program identity (default: the first input's directory name) |
| `--output-dir DIR` | artifact directory (default: `./artifacts`) |
| `--database PATH` | Kùzu database (default: `<output-dir>/graph.kuzu`) |
| `--no-database` | skip the Kùzu import; `kuzu` is then not required |
| `--no-svg` | do not render `graph.svg` (useful for large programs) |
| `--absolute-paths` | keep clang's absolute source paths in the graph (see below) |
| `--reset` | delete the database first (needed after a schema change) |
| `--entry NAME` | extra entry point for dead-code analysis (repeatable) |
| `--std STD` | language standard (default `c11` for `.c`, `c++20` otherwise) |
| `-I DIR`, `-D MACRO` | forwarded to clang |

| Environment | Meaning |
|---|---|
| `CLANG` | CIR-enabled clang. If it is unset, the script tries `$LLVM_BUILD/bin/clang`, `~/llvm-project/build/bin/clang`, then `clang` on `PATH`, and uses the first one that accepts `-emit-cir`. |
| `PYTHON` | Python with `kuzu`. If it is unset: the active virtualenv, then `./.venv`, then `./.conda-env`, then `python3`. |

Outputs:

| File | Content |
|---|---|
| `graph.kuzu` | Kùzu database, loaded in one transaction. Several programs can share it. |
| `graph.cypher` | The exact import statements: schema DDL, a program-scoped delete, batched `UNWIND … MERGE/CREATE`. Re-importing is idempotent. |
| `graph.json` | Layered JSON for LLM prompts (see [ARCHITECTURE.md](ARCHITECTURE.md#json-export)). |
| `graph.dot` / `graph.svg` | TU and function clusters, blocks listing their instructions, colored CFG edges, call edges. SVG only if Graphviz `dot` is installed. |
| `cir/<program>/*.cir` | The raw CIR compiled from your sources. |

Examples:

```sh
./analyze.sh examples/demo/main.cpp examples/demo/math.cpp --program-id demo
./analyze.sh examples/demo_c/main.c examples/demo_c/stack.c --program-id stack
./analyze.sh testbench/cases/10_three_tu_chain            # committed .cir, no clang needed
./analyze.sh -I ../cJSON ../cJSON/cJSON.c my_caller.c --program-id cjson --no-svg
```

C and C++ can be mixed in one program; each file's language follows its extension.

**Paths in the graph.** clang records every source file's absolute path. Before
building the graph, `analyze.sh` rewrites those paths:

* files under the current directory become relative (`src/main.c`, and locations
  like `src/main.c:12:3`);
* other files under your home directory become `~/…`;
* system headers keep their standard paths (`/usr/include/…`).

Only the module name and source locations are rewritten. String literals in your
code are program data and stay exactly as written. The graph therefore never
carries your user name or checkout location, and the same project gives the same
graph on any machine. Pass `--absolute-paths` to keep clang's original paths.
`.cir` files you pass in yourself are used unchanged.

## cir-graph: build a graph from .cir files

```sh
cir-graph a.cir b.cir --program-id demo \
    --database out/graph.kuzu --output out/graph.cypher --json out/graph.json --dot out/graph.dot
```

| Option | Meaning |
|---|---|
| `inputs…` | `.cir` files, one per translation unit (structured *or* flattened CIR) |
| `--database PATH` | create/update a Kùzu database |
| `--output PATH` | write the Cypher import script |
| `--json PATH` / `--dot PATH` | write the JSON / DOT exports |
| `--program-id ID` | program identity (default: directory of the first input) |
| `--name NAME` | human-readable program name |
| `--entry NAME` | extra entry point for dead-code analysis (repeatable) |
| `--reset` | delete the database first (use after schema changes) |
| `--batch-size N` | rows per `UNWIND` statement (default 250) |
| `--dry-run` | parse, link and print the summary only |

A Cypher script written by `--output` can be loaded later, or into another
database:

```sh
cir-graph-load out/graph.cypher --database other.kuzu [--reset]
```

## Producing CIR yourself

Use the same flags `analyze.sh` uses, with one invocation per translation unit:

```sh
$CLANG -c -std=c++20 -Xclang -emit-cir -Xclang -clangir-disable-passes foo.cpp -o foo.cir
```

`-clangir-disable-passes` keeps CIR *structured* (`cir.if`, `cir.for`, …), which
preserves source-level shape; cir-graph lowers it to blocks itself. Flattened CIR
(after `cir-flatten-cfg`) is accepted as well.

CIR's textual format changes with the compiler version. Generate all TUs of a
program with the same clang. The parser is tested against the version recorded in
[`testbench/CLANG_VERSION`](../testbench/CLANG_VERSION).

## cir-graph-query: layered queries

```sh
cir-graph-query summary                        # programs, functions, blocks, calls, flow kinds
cir-graph-query functions                      # macro: functions with flags
cir-graph-query calls [--cross-tu]             # macro: call edges
cir-graph-query cycles | dead | entry          # macro: recursion / dead code / entry points
cir-graph-query cfg checked_sum                # meso: blocks and CFG edges of a function
cir-graph-query ops fact                       # micro: operations in block order
cir-graph-query flows main --kind store_to_load
cir-graph-query callsites fact                 # bridges: argument/return bindings
cir-graph-query trace main --var total         # values carrying a source variable
cir-graph-query cypher "MATCH (f:Function) RETURN f.demangledName"
```

* The default database is `artifacts/graph.kuzu`; use `--database PATH` for
  another one.
* Function arguments match the demangled name, the mangled symbol, or an ID
  (exact match first, then `name(`, then substring).
* Use `--program ID` when one database holds several programs.

## Cypher by layer

[`examples/queries.cypher`](../examples/queries.cypher) has 15 runnable queries
for the demo program, grouped by layer. A few highlights:

```cypher
// MACRO: cross-TU calls
MATCH (a:Function)-[c:CALLS]->(b:Function)
WHERE c.isCrossTU AND a.programId = 'demo'
RETURN a.demangledName AS caller, b.demangledName AS callee, c.callSiteLine;

// MESO: loops (back edges, latch -> header)
MATCH (f:Function)-[:HAS_BLOCK]->(latch:BasicBlock)-[e:CFG_NEXT {isBackEdge: true}]->(header:BasicBlock)
WHERE f.programId = 'demo'
RETURN f.demangledName, header.name, latch.name;

// MICRO: which stores can each load of `total` observe (including scale()'s write)?
MATCH (src:Value)-[r:FLOWS_TO {kind: 'store_to_load'}]->(ld:Value)
WHERE ld.programId = 'demo' AND ld.varName = 'total'
RETURN ld.id, src.id, r.isInterprocedural;

// BRIDGES: call site -> callee, with argument -> parameter bindings
MATCH (o:Operation)-[:INVOKES]->(g:Function)-[:HAS_PARAMETER]->(p:Value),
      (arg:Value)-[f:FLOWS_TO {kind: 'call_argument'}]->(p)
WHERE f.targetOpId = o.id AND o.programId = 'demo'
RETURN o.functionId, o.line, g.demangledName, arg.id, p.varName;
```

Tips:

* Always filter by `programId` when a database holds several programs.
* Bound variable-length paths (`[:FLOWS_TO*1..8]`, `[:CALLS*1..10]`) on large
  programs.
* Check aggregate results against a plain filtered count or against the JSON
  before trusting them. Kùzu 0.11.3 miscounted one conditional aggregation
  (`sum(CASE …)`) in our tests; see [KNOWN_ISSUES.md](KNOWN_ISSUES.md#v407).

## Kùzu Explorer

[Kùzu Explorer](https://docs.kuzudb.com/visualization/) is a browser UI for
running Cypher and viewing results as a graph. Mount the artifact directory read-only:

```sh
docker run --rm -d -p 127.0.0.1:8000:8000 \
  -v "$(pwd)/artifacts:/database:ro" \
  -e KUZU_FILE=graph.kuzu -e MODE=READ_ONLY \
  kuzudb/explorer:0.11.3
```

Then open <http://localhost:8000>. Use the Explorer version that matches the
`kuzu` package (0.11.3), because the on-disk format changes between releases.

Graph-view tips:

* Return nodes and relationships (`RETURN a, e, b`), not only properties, to get
  the graph view.
* Start from one function: `MATCH (f:Function {demangledName: 'fact(int)'})-[r:HAS_BLOCK|CFG_NEXT*1..3]-(n) RETURN f, r, n`.
* `Operation` and `Value` nodes are numerous. Add the micro layer for one
  function at a time.

## Python API

```python
from cir_graph.linker import build_program
from cir_graph.exporters import to_json, to_dot
from cir_graph.schema import import_statements
from cir_graph.cli import load_kuzu
from pathlib import Path

program = build_program(["main.cir", "math.cir"], "demo", entry_points=[])
print(program.recursionCycles)
data = to_json(program)                          # dict, same as graph.json
load_kuzu(import_statements(program), Path("demo.kuzu"), reset=False)
```

For project-sized inputs, see `cir_graph.project_builder.build_program` and
`cir_graph.disk_store.DiskGraph` in [ARCHITECTURE.md](ARCHITECTURE.md#large-projects).

## Optional public-API metadata

CIR lowers away enum names, macros and some typedef spellings. These helpers use
Clang's JSON AST (any recent clang works; CIR support is not needed) to export
**public declarations only**. Function bodies, comments and source excerpts are
never exported. The output is metadata to attach next to a graph. It does not
prove that a definition exists in the compiled graph.

```sh
# C library headers, selected by name prefix (defaults: git_ / GIT_ for libgit2)
python3 -m cir_graph.public_api.base include/git2.h --include include \
    --define GIT_STATIC_BUILD=1 --output api.json

# Other C libraries: repeatable prefixes plus exact type names
python3 -m cir_graph.public_api.base libpq-fe.h --function-prefix PQ \
    --constant-prefix PG --type-prefix PG --type-name Oid --output pq-api.json

# C projects, with a names-only catalog of function-like macros
python3 -m cir_graph.public_api.c_project bfd.h --function-prefix bfd_ \
    --constant-prefix BFD_ --output bfd-api.json

# Selected C++ types: public members, overloads, defaults, member enums
python3 -m cir_graph.public_api.cpp umbrella.h --std c++17 \
    --type-name QVersionNumber --include qt/include --output qt-api.json
```

Empty prefixes are rejected, so unrelated transitive system declarations are not
exported. C++ templates and inherited members are not expanded.
