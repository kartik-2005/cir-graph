# Testbench

Sixteen small C/C++ programs, each aimed at one graph feature. Every program
checks its own result and exits 0 when run natively. Each translation unit's raw
CIR is **committed next to its source**, so the checks run without an LLVM build:

```sh
python3 -m unittest tests.test_testbench -v      # from the repository root
```

`tests/test_testbench.py` asserts the expected graph for every case:

* translation units and defined functions
* every call edge (one per call site, with its cross-TU flag)
* recursion cycles, globals, entry points and dead code
* targeted CFG and data-flow facts (listed in the table below)

It also imports all 16 programs into one Kùzu database.

| Case | Feature checked |
|---|---|
| `01_constant` | minimal program, one block |
| `02_local_assignment` | local `store_to_load` and `def_use` |
| `03_if_else` | `true`/`false` CFG edges; calls inside a short-circuit `&&` |
| `04_while_loop` | loop back edge |
| `05_same_tu_call` | same-TU call, straight-line CFG |
| `06_header_function` | header declaration binds to the definition in another TU |
| `07_extern_global` | `extern` global resolves to its definition (`ACCESSES_GLOBAL`) |
| `08_static_function` | same-named `static` functions stay per TU |
| `09_static_global` | same-named `static` globals stay per TU |
| `10_three_tu_chain` | cross-TU argument and return bindings |
| `11_discount_if` | branches plus three-level calls |
| `12_reference_side_effect` | a write through `int&` in another TU flows back to the caller |
| `13_recursion` | self recursion and mutual recursion (SCCs) |
| `14_switch_loop` | `switch` edges; `for` with `break`/`continue` |
| `15_struct_field` | a struct field written in one call reaches a read in another |
| `16_c_function_pointer` | C TUs; address-taken targets; unresolved indirect call through a parameter (documented limitation) |

## Adding or changing a case

1. Create `cases/NN_name/` with `main.c` or `main.cpp` (plus other TUs and
   headers). Have `main` return 0 only when the program computes the expected
   result.
2. Regenerate the CIR with a CIR-enabled clang:

   ```sh
   CLANG=~/llvm-project/build/bin/clang testbench/regenerate.sh NN_name
   ```

   The script compiles each TU with `-emit-cir -clangir-disable-passes`, strips
   absolute paths, builds and runs the program natively, and records the
   compiler in `CLANG_VERSION`.
3. Add the case's expectations to `EXPECTED` in `tests/test_testbench.py`, plus a
   targeted assertion for the feature it exercises.

CIR's text format follows the compiler version. When you upgrade clang,
regenerate every case (`testbench/regenerate.sh` with no arguments) and re-run the
full suite.
