#!/usr/bin/env python3
"""Tests for the unified CIR knowledge graph.

Run:  python3 -m unittest discover -s tests -v      (or: pytest tests)

Inputs are raw CIR emitted by
`clang -Xclang -emit-cir -Xclang -clangir-disable-passes`: the testbench cases
(testbench/cases/*/*.cir) plus a stress program (tests/fixtures/stress/stress.cpp)
and its flattened form (stress.flat.cir, captured after cir-flatten-cfg). Database tests are
skipped when the `kuzu` package is missing; the end-to-end pipeline test is
skipped when no CIR-enabled clang is found.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FIXTURES = HERE / "fixtures"            # stress program only
CASES = REPO / "testbench" / "cases"   # testbench CIR (committed .cir next to sources)
sys.path.insert(0, str(REPO / "src"))

from cir_graph.cir_parser import parse_cir_file  # noqa: E402
from cir_graph.exporters import to_dot, to_json  # noqa: E402
from cir_graph.linker import build_program  # noqa: E402
from cir_graph.schema import NODE_TABLES, REL_TABLES, import_statements  # noqa: E402
from cir_graph.load import split_cypher  # noqa: E402

try:
    import kuzu  # noqa: F401
    HAVE_KUZU = True
except ImportError:
    HAVE_KUZU = False

_CACHE = {}


def case(name: str, program_id: str = None):
    """Build (and memoize) the program for a fixture directory."""
    key = (name, program_id)
    if key not in _CACHE:
        files = sorted((CASES / name).glob("*.cir"),
                       key=lambda p: (p.name != "main.cir", p.name))
        if name == "stress":
            files = [FIXTURES / "stress" / "stress.cir"]
        _CACHE[key] = build_program([str(f) for f in files], program_id or name)
    return _CACHE[key]


def fn_named(program, demangled: str, tu: str = None):
    matches = [f for f in program.all_functions()
               if f.demangledName == demangled and (tu is None or (f.tuId or "").endswith(tu))]
    assert len(matches) == 1, f"{demangled!r} in {tu}: {[f.id for f in matches]}"
    return matches[0]


def block_named(fn, name: str):
    return [b for b in fn.blocks if b.name == name]


def all_flows(program):
    return [f for fn in program.all_functions() for f in fn.flows] + program.interFlows


# -----------------------------------------------------------------------------
# Parser
# -----------------------------------------------------------------------------
class ParserTests(unittest.TestCase):
    def test_nested_ternary_regions_keep_their_calls(self):
        module = parse_cir_file(str(CASES / "03_if_else" / "main.cir"))
        main = module.function("main")
        ternary = next(op for op in main.body.blocks[0].ops if op.name == "cir.ternary")
        self.assertEqual([r.label for r in ternary.regions], ["true", "false"])
        nested_calls = [op for op in ternary.regions[0].blocks[0].ops if op.name == "cir.call"]
        self.assertEqual(len(nested_calls), 1)
        self.assertEqual(nested_calls[0].symbol, "_Z6choosei")
        self.assertIn("{…}, false {…})", ternary.display_text)

    def test_declarations_linkage_and_locations(self):
        module = parse_cir_file(str(CASES / "08_static_function" / "left.cir"))
        offset = module.function("_ZL6offseti")
        self.assertEqual(offset.linkage, "internal")
        self.assertFalse(offset.is_declaration)
        points = module.locations.resolve(offset.loc)
        self.assertEqual((points[0].line, points[-1].line), (3, 3))
        decl = parse_cir_file(str(CASES / "06_header_function" / "main.cir")).function("_Z7add_onei")
        self.assertTrue(decl.is_declaration)
        self.assertEqual(decl.linkage, "external")
        self.assertEqual(decl.params, [(None, "!s32i")])

    def test_flat_successors_and_switch_cases(self):
        module = parse_cir_file(str(FIXTURES / "stress" / "stress.flat.cir"))
        self.assertTrue(module.isFlat)
        loops = module.function("_Z5loopsi")
        switch = next(op for b in loops.body.blocks for op in b.ops if op.name == "cir.switch.flat")
        self.assertEqual([s.caseValue for s in switch.successors], [None, "1", "2", "3"])
        merge = next(b for b in module.function("main").body.blocks if b.args)
        self.assertEqual(merge.args[0][1], "!cir.bool")

    def test_globals(self):
        decl = parse_cir_file(str(CASES / "07_extern_global" / "main.cir")).globals[0]
        defn = parse_cir_file(str(CASES / "07_extern_global" / "state.cir")).globals[0]
        self.assertTrue(decl.is_declaration)
        self.assertFalse(defn.is_declaration)
        self.assertEqual(defn.initializer, "#cir.int<3>")


# -----------------------------------------------------------------------------
# Macro layer: call graph
# -----------------------------------------------------------------------------
class MacroLayerTests(unittest.TestCase):
    def test_static_functions_are_scoped_per_tu(self):  # testbench case 08
        program = case("08_static_function")
        left = fn_named(program, "offset(int)", "left.cpp")
        right = fn_named(program, "offset(int)", "right.cpp")
        self.assertNotEqual(left.id, right.id)
        self.assertNotEqual(left.symbolId, right.symbolId)
        self.assertTrue(left.isStatic and right.isStatic)
        for wrapper, target in (("from_left(int)", left), ("from_right(int)", right)):
            caller = fn_named(program, wrapper)
            edges = [c for c in program.callEdges if c.callerId == caller.id]
            self.assertEqual([c.calleeId for c in edges], [target.id])
            self.assertFalse(edges[0].isCrossTU)
        self.assertEqual(len([s for s in program.symbols.values() if s.name == "_ZL6offseti"]), 2)

    def test_extern_declaration_links_to_definition(self):  # case 06
        program = case("06_header_function")
        self.assertEqual(program.externFunctions, {})
        (call,) = program.callEdges
        self.assertTrue(call.isCrossTU)
        self.assertFalse(call.isExternal)
        callee = program.function_index()[call.calleeId]
        self.assertTrue(callee.tuId.endswith(":helper.cpp"))
        self.assertFalse(callee.isDeclaration)

    def test_three_tu_chain(self):  # case 10
        program = case("10_three_tu_chain")
        chain = [(program.function_index()[c.callerId].demangledName,
                  program.function_index()[c.calleeId].demangledName, c.isCrossTU)
                 for c in program.callEdges]
        self.assertEqual(sorted(chain), [("first(int)", "second(int)", True),
                                         ("main", "first(int)", True)])

    def test_short_circuit_nested_call_is_extracted(self):  # case 03
        program = case("03_if_else")
        main = fn_named(program, "main")
        calls = [c for c in program.callEdges if c.callerId == main.id]
        self.assertEqual(len(calls), 2)
        nested = next(op for op in main.operations if op.id == calls[1].callOpId)
        parent = next(op for op in main.operations if op.id == nested.parentOpId)
        self.assertEqual(parent.name, "cir.ternary")

    def test_recursion_dead_code_and_entry_points(self):
        program = case("stress")
        names = [[program.function_index()[f].demangledName for f in cycle]
                 for cycle in program.recursionCycles]
        self.assertIn(["fact(int)"], names)
        self.assertIn(["is_odd(int)", "is_even(int)"], names)
        self.assertTrue(fn_named(program, "main").isEntryPoint)
        self.assertTrue(fn_named(program, "unused_helper()").isDeadCode)
        self.assertFalse(fn_named(program, "loops(int)").isDeadCode)

    def test_indirect_call_through_function_pointer(self):
        program = case("stress")
        sq = fn_named(program, "sq(int)")
        self.assertTrue(sq.isAddressTaken)
        indirect = [c for c in program.callEdges if not c.isDirect]
        self.assertEqual([c.calleeId for c in indirect], [sq.id])

    def test_unresolved_external_function(self):
        program = case("stress")
        ext = fn_named(program, "ext_fn(int)")
        self.assertTrue(ext.isDeclaration)
        self.assertIsNone(ext.tuId)
        (call,) = [c for c in program.callEdges if c.calleeId == ext.id]
        self.assertTrue(call.isExternal)
        self.assertFalse(call.isCrossTU)

    def test_globals_static_and_extern(self):
        shared = case("07_extern_global").globals
        self.assertEqual(len(shared), 1)
        (gv,) = shared.values()
        self.assertFalse(gv.isDeclaration)
        self.assertTrue(gv.tuId.endswith(":state.cpp"))
        program = case("09_static_global")
        self.assertEqual(len(program.globals), 2)
        accessed = {(op.split("#")[0].rsplit(":", 2)[-2], gid.rsplit(":", 2)[-2])
                    for op, gid in program.globalAccesses}
        self.assertEqual(accessed, {("left.cpp", "left.cpp"), ("right.cpp", "right.cpp")})


# -----------------------------------------------------------------------------
# Meso layer: control flow
# -----------------------------------------------------------------------------
class MesoLayerTests(unittest.TestCase):
    def test_if_else_branch_kinds(self):
        choose = fn_named(case("03_if_else"), "choose(int)")
        entry = next(b for b in choose.blocks if b.isEntry)
        self.assertEqual(entry.terminatorOp, "cir.if")
        out = {(e.kind, e.dst) for e in choose.cfgEdges if e.src == entry.id}
        then_id, else_id = block_named(choose, "if.then")[0].id, block_named(choose, "if.else")[0].id
        self.assertEqual(out, {("true", then_id), ("false", else_id)})
        cond = {e.conditionValueId for e in choose.cfgEdges if e.src == entry.id}
        self.assertEqual(len(cond), 1)
        self.assertEqual(choose.values[cond.pop()].type, "!cir.bool")
        self.assertTrue(all(b.isExit for b in block_named(choose, "if.then") + block_named(choose, "if.else")))

    def test_while_loop_back_edge(self):
        fn = fn_named(case("04_while_loop"), "sum_to_three()")
        ordinal = {b.id: b.ordinal for b in fn.blocks}
        names = {b.id: b.name for b in fn.blocks}
        back = [e for e in fn.cfgEdges if e.isBackEdge]
        self.assertEqual(len(back), 1)
        self.assertLessEqual(ordinal[back[0].dst], ordinal[back[0].src])
        self.assertEqual(names[back[0].dst], "while.cond")
        kinds = {(names[e.src], e.kind, names[e.dst]) for e in fn.cfgEdges}
        self.assertIn(("while.cond", "true", "while.body"), kinds)
        self.assertIn(("while.cond", "false", "while.end"), kinds)

    def test_for_do_switch_break_continue(self):
        loops = fn_named(case("stress"), "loops(int)")
        names = {b.id: b.name for b in loops.blocks}
        edges = {(names[e.src], e.kind, e.label, names[e.dst]) for e in loops.cfgEdges}
        self.assertIn(("for.cond", "true", None, "for.body"), edges)
        self.assertIn(("if.then", "unconditional", None, "for.inc"), edges)   # continue
        self.assertIn(("if.then", "unconditional", None, "for.end"), edges)   # break
        self.assertIn(("do.cond", "true", None, "do.body"), edges)
        labels = {e.label for e in loops.cfgEdges if e.kind == "switch"}
        self.assertEqual(labels, {"1", "2", "3", "default"})
        self.assertIn(("switch.case", "unconditional", None, "switch.case"), edges)  # fallthrough
        self.assertEqual(sum(e.isBackEdge for e in loops.cfgEdges), 2)

    def test_every_operation_in_exactly_one_block(self):
        for name in ("03_if_else", "stress"):
            for fn in case(name).all_functions():
                placed = [o for b in fn.blocks for o in b.operations]
                self.assertEqual(sorted(placed), sorted(o.id for o in fn.operations))
                self.assertEqual(len(placed), len(set(placed)))
                block_ids = {b.id for b in fn.blocks}
                self.assertTrue(all(o.blockId in block_ids for o in fn.operations))
                self.assertTrue(all(e.src in block_ids and e.dst in block_ids for e in fn.cfgEdges))
                self.assertEqual([b.ordinal for b in fn.blocks], list(range(len(fn.blocks))))

    def test_flat_and_structured_cir_agree(self):
        structured = case("stress")
        flat = build_program([str(FIXTURES / "stress" / "stress.flat.cir")], "stress")
        key = lambda p: sorted((c.callerId, c.calleeId, c.isDirect, c.callSiteLine) for c in p.callEdges)
        self.assertEqual(key(structured), key(flat))
        flat_index = flat.function_index()
        for fn in structured.all_functions():
            other = flat_index[fn.id]
            self.assertEqual(sum(e.isBackEdge for e in fn.cfgEdges),
                             sum(e.isBackEdge for e in other.cfgEdges), fn.demangledName)
        main = fn_named(flat, "main")
        self.assertTrue(any(f.kind == "block_arg" for f in main.flows))


# -----------------------------------------------------------------------------
# Micro layer: SSA values and data flow
# -----------------------------------------------------------------------------
class MicroLayerTests(unittest.TestCase):
    def test_store_to_load_is_branch_sensitive(self):
        choose = fn_named(case("03_if_else"), "choose(int)")
        block_of = {o.id: o.blockId for o in choose.operations}
        s2l = [f for f in choose.flows if f.kind == "store_to_load"]
        retval = [f for f in s2l if choose.values[f.dst].varName == "__retval"]
        self.assertEqual(len(retval), 2)
        for flow in retval:  # each branch's store reaches only its own load
            self.assertEqual(block_of[flow.sourceOpId], block_of[flow.targetOpId])
        param = choose.values[choose.parameters[0]]
        self.assertEqual(param.varName, "value")
        self.assertIn(param.id, {f.src for f in s2l})

    def test_loop_carried_store_reaches_condition(self):
        fn = fn_named(case("04_while_loop"), "sum_to_three()")
        names = {b.id: b.name for b in fn.blocks}
        block_of = {o.id: o.blockId for o in fn.operations}
        carried = [f for f in fn.flows if f.kind == "store_to_load"
                   and names[block_of[f.sourceOpId]] == "while.body"
                   and names[block_of[f.targetOpId]] == "while.cond"]
        self.assertEqual({fn.values[f.dst].varName for f in carried}, {"i"})

    def test_def_use_and_region_yield(self):
        main = fn_named(case("03_if_else"), "main")
        ternary = next(o for o in main.operations if o.name == "cir.ternary")
        yields = [f for f in main.flows if f.kind == "region_yield" and f.dst == ternary.results[0]]
        self.assertEqual(len(yields), 2)
        cmp_op = next(o for o in main.operations if o.name == "cir.cmp")
        def_use = [f for f in main.flows if f.kind == "def_use" and f.targetOpId == cmp_op.id]
        self.assertEqual([f.src for f in def_use], cmp_op.operands)
        consts = {v.constValue for v in main.values.values() if v.kind == "constant"}
        self.assertTrue({"0", "1", "2", "false"} <= consts)

    def test_sibling_regions_reuse_ssa_names_without_collision(self):
        choose = fn_named(case("03_if_else"), "choose(int)")
        fives = [v for v in choose.values.values() if v.name == "%5"]
        self.assertEqual(len(fives), 2)
        self.assertEqual({v.constValue for v in fives}, {"1", "2"})
        self.assertEqual(len({v.id for v in fives}), 2)

    def test_defines_and_uses_are_consistent(self):
        for fn in case("stress").all_functions():
            for op in fn.operations:
                for vid in op.results:
                    self.assertEqual(fn.values[vid].definingOpId, op.id)
                for vid in op.operands:
                    self.assertIn(vid, fn.values)
                    self.assertNotEqual(fn.values[vid].kind, "unresolved")


# -----------------------------------------------------------------------------
# Cross-layer bridges
# -----------------------------------------------------------------------------
class BridgeTests(unittest.TestCase):
    def test_argument_and_return_bindings_across_tus(self):
        for name in ("06_header_function", "10_three_tu_chain"):
            program = case(name)
            index = program.function_index()
            for call in program.callEdges:
                callee = index[call.calleeId]
                caller = index[call.callerId]
                op = next(o for o in caller.operations if o.id == call.callOpId)
                args = [f for f in program.interFlows
                        if f.kind == "call_argument" and f.targetOpId == op.id]
                self.assertEqual([(f.src, f.dst) for f in args],
                                 list(zip(op.operands, callee.parameters)))
                rets = [f for f in program.interFlows
                        if f.kind == "call_return" and f.targetOpId == op.id]
                self.assertEqual({f.dst for f in rets}, set(op.results))
                self.assertTrue(all(f.src.startswith(callee.id + "#") for f in rets))

    def test_static_bindings_stay_in_tu(self):
        program = case("08_static_function")
        tu_of = {v: fn.tuId for fn in program.all_functions() for v in fn.values}
        for flow in program.interFlows:
            if flow.kind == "call_argument" and "offset" in flow.dst:
                self.assertEqual(tu_of[flow.src], tu_of[flow.dst])

    def test_reference_parameter_memory_flow(self):
        program = case("stress")
        main, bump = fn_named(program, "main"), fn_named(program, "bump(int&)")
        inter = [f for f in program.interFlows if f.kind == "store_to_load"]
        into_bump = [f for f in inter if f.src in main.values and f.dst in bump.values]
        out_of_bump = [f for f in inter if f.src in bump.values and f.dst in main.values]
        self.assertTrue(into_bump, "caller store should reach callee load through r")
        self.assertTrue(out_of_bump, "callee write through r should reach caller loads of a")
        self.assertTrue(all(main.values[f.dst].varName == "a" for f in out_of_bump))


# -----------------------------------------------------------------------------
# Exports and Kùzu
# -----------------------------------------------------------------------------
class ExportTests(unittest.TestCase):
    def test_json_layers(self):
        program = case("08_static_function")
        data = json.loads(json.dumps(to_json(program)))
        self.assertEqual(set(data["layers"]), {"macro", "meso", "micro", "bridges"})
        self.assertEqual(data["summary"]["crossTUCalls"], 2)
        defined = [f["id"] for f in data["layers"]["macro"]["functions"]]
        self.assertEqual(set(data["layers"]["meso"]), set(defined))
        site = data["layers"]["bridges"]["callSites"][0]
        self.assertTrue(site["argumentBindings"] and site["returnBindings"])

    def test_dot_and_cypher_are_deterministic(self):
        first = build_program([str(p) for p in sorted((CASES / "10_three_tu_chain").glob("*.cir"))], "x")
        second = build_program([str(p) for p in sorted((CASES / "10_three_tu_chain").glob("*.cir"))], "x")
        self.assertEqual(import_statements(first), import_statements(second))
        self.assertEqual(to_dot(first), to_dot(second))
        self.assertTrue(to_dot(first).startswith("digraph cir_graph {"))


@unittest.skipUnless(HAVE_KUZU, "kuzu not installed")
class KuzuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cir-graph-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, program, db_path):
        from cir_graph.cli import load_kuzu
        load_kuzu(import_statements(program), Path(db_path), reset=False)

    def _counts(self, db_path):
        import kuzu
        db = kuzu.Database(db_path, read_only=True)
        conn = kuzu.Connection(db)
        counts = {}
        for table in NODE_TABLES:
            counts[table] = conn.execute(f"MATCH (n:{table}) RETURN count(n)").get_next()[0]
        for rel, *_ in REL_TABLES:
            counts[rel] = conn.execute(f"MATCH ()-[r:{rel}]->() RETURN count(r)").get_next()[0]
        conn.close()
        db.close()
        return counts

    def test_import_is_idempotent_and_programs_coexist(self):
        db_path = os.path.join(self.tmp, "graph.kuzu")
        self._load(case("08_static_function", "p08"), db_path)
        first = self._counts(db_path)
        self._load(case("08_static_function", "p08"), db_path)
        self.assertEqual(self._counts(db_path), first)
        self._load(case("03_if_else", "p03"), db_path)
        both = self._counts(db_path)
        self.assertEqual(both["Program"], 2)
        self.assertGreater(both["Operation"], first["Operation"])

    def test_layered_queries(self):
        import kuzu
        db_path = os.path.join(self.tmp, "graph.kuzu")
        self._load(case("08_static_function", "p08"), db_path)
        self._load(case("04_while_loop", "p04"), db_path)
        conn = kuzu.Connection(kuzu.Database(db_path, read_only=True))

        def rows(query):
            result = conn.execute(query)
            out = []
            while result.has_next():
                out.append(result.get_next())
            return out

        # Macro: static offset() resolved within each TU.
        calls = rows("MATCH (a:Function)-[c:CALLS]->(b:Function) WHERE b.name = '_ZL6offseti' "
                     "RETURN a.tuId, b.tuId, c.isCrossTU")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(r[0] == r[1] and r[2] is False for r in calls))
        # Meso: loop back edge.
        back = rows("MATCH (a:BasicBlock)-[e:CFG_NEXT]->(b:BasicBlock) WHERE e.isBackEdge "
                    "RETURN a.ordinal, b.ordinal, b.name")
        self.assertEqual(len(back), 1)
        self.assertLessEqual(back[0][1], back[0][0])
        # Micro: store-to-load edges exist for loop variables.
        s2l = rows("MATCH (a:Value)-[f:FLOWS_TO {kind: 'store_to_load'}]->(b:Value) "
                   "WHERE b.programId = 'p04' AND b.varName = 'i' RETURN count(*)")
        self.assertGreater(s2l[0][0], 0)
        # Bridges: call op -> callee and argument -> parameter.
        bridge = rows("MATCH (o:Operation)-[:INVOKES]->(g:Function)-[:HAS_PARAMETER]->(p:Value), "
                      "(arg:Value)-[f:FLOWS_TO {kind: 'call_argument'}]->(p) "
                      "WHERE f.targetOpId = o.id AND g.programId = 'p08' "
                      "RETURN o.id, g.demangledName")
        self.assertEqual(len(bridge), 4)

    def test_cypher_script_matches_direct_import(self):
        program = case("10_three_tu_chain")
        statements = import_statements(program)
        script = "".join(s + ";\n\n" for s in statements)
        self.assertEqual(split_cypher(script), statements)


def _find_clang():
    candidate = os.environ.get("CLANG")
    return candidate if candidate and Path(candidate).is_file() else None


@unittest.skipUnless(HAVE_KUZU and _find_clang(),
                     "needs kuzu and CLANG set to a CIR-enabled clang")
class PipelineTests(unittest.TestCase):
    def test_analyze_three_tu_chain(self):
        out = tempfile.mkdtemp(prefix="cir-graph-pipe-")
        try:
            sources = sorted(str(p) for p in (REPO / "testbench" / "cases" / "10_three_tu_chain").glob("*.cpp"))
            env = dict(os.environ, CLANG=_find_clang(), PYTHON=sys.executable)
            subprocess.run(["bash", str(REPO / "analyze.sh"), *sources,
                            "--output-dir", out, "--program-id", "chain"],
                           check=True, env=env, capture_output=True, text=True)
            for artifact in ("graph.kuzu", "graph.cypher", "graph.json", "graph.dot"):
                self.assertTrue(os.path.exists(os.path.join(out, artifact)), artifact)
            data = json.loads(Path(out, "graph.json").read_text())
            self.assertEqual(data["summary"]["crossTUCalls"], 2)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_analyze_c_demo_out_parameter_flow(self):
        out = tempfile.mkdtemp(prefix="cir-graph-pipe-c-")
        try:
            demo = REPO / "examples" / "demo_c"
            env = dict(os.environ, CLANG=_find_clang(), PYTHON=sys.executable)
            subprocess.run(["bash", str(REPO / "analyze.sh"), str(demo / "main.c"), str(demo / "stack.c"),
                            "--output-dir", out, "--program-id", "stack", "--no-database", "--no-svg"],
                           check=True, env=env, capture_output=True, text=True)
            data = json.loads(Path(out, "graph.json").read_text())
            self.assertEqual(data["summary"]["crossTUCalls"], 3)
            # stack_pop writes *out in stack.c; sum_all reads `value` in main.c.
            flows = {(f["from"].split("#")[0], f["to"].split("#")[0])
                     for f in data["layers"]["bridges"]["interproceduralFlows"]
                     if f["kind"] == "store_to_load"}
            self.assertIn(("stack:stack.c:stack_pop", "stack:main.c:sum_all"), flows)
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
