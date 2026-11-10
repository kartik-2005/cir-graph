"""Testbench: expected graph facts for every case in testbench/cases.

Each case is a small, self-checking C/C++ program (it exits 0 when run natively).
Its raw CIR is committed next to the sources, so this suite needs no LLVM build;
regenerate the .cir files with testbench/regenerate.sh after changing a case.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "testbench" / "cases"
sys.path.insert(0, str(REPO / "src"))

from cir_graph.exporters import to_json  # noqa: E402
from cir_graph.linker import build_program  # noqa: E402
from cir_graph.schema import import_statements  # noqa: E402

try:
    import kuzu  # noqa: F401
    HAVE_KUZU = True
except ImportError:
    HAVE_KUZU = False

# Per case: translation units, defined functions, call edges as
# (caller, callee, crossTU) with one entry per call site, recursion cycles, globals.
EXPECTED = {
    "01_constant": dict(tus=1, functions={"main"}, calls=[], cycles=[], globals=0),
    "02_local_assignment": dict(tus=1, functions={"main"}, calls=[], cycles=[], globals=0),
    "03_if_else": dict(tus=1, functions={"choose(int)", "main"},
                       calls=[("main", "choose(int)", False)] * 2, cycles=[], globals=0),
    "04_while_loop": dict(tus=1, functions={"sum_to_three()", "main"},
                          calls=[("main", "sum_to_three()", False)], cycles=[], globals=0),
    "05_same_tu_call": dict(tus=1, functions={"double_value(int)", "main"},
                            calls=[("main", "double_value(int)", False)], cycles=[], globals=0),
    "06_header_function": dict(tus=2, functions={"add_one(int)", "main"},
                               calls=[("main", "add_one(int)", True)], cycles=[], globals=0),
    "07_extern_global": dict(tus=2, functions={"main"}, calls=[], cycles=[], globals=1),
    "08_static_function": dict(
        tus=3, functions={"from_left(int)", "from_right(int)", "offset(int)", "main"},
        calls=[("main", "from_left(int)", True), ("main", "from_right(int)", True),
               ("from_left(int)", "offset(int)", False), ("from_right(int)", "offset(int)", False)],
        cycles=[], globals=0),
    "09_static_global": dict(tus=3, functions={"read_left()", "read_right()", "main"},
                             calls=[("main", "read_left()", True), ("main", "read_right()", True)],
                             cycles=[], globals=2),
    "10_three_tu_chain": dict(tus=3, functions={"first(int)", "second(int)", "main"},
                              calls=[("main", "first(int)", True), ("first(int)", "second(int)", True)],
                              cycles=[], globals=0),
    "11_discount_if": dict(tus=1, functions={"apply_discount(int, int)", "demo()", "main"},
                           calls=[("main", "demo()", False), ("main", "apply_discount(int, int)", False),
                                  ("demo()", "apply_discount(int, int)", False)],
                           cycles=[], globals=0),
    "12_reference_side_effect": dict(tus=2, functions={"add_bonus(int&, int)", "main"},
                                     calls=[("main", "add_bonus(int&, int)", True)], cycles=[], globals=0),
    "13_recursion": dict(
        tus=1, functions={"fact(int)", "is_even(unsigned int)", "is_odd(unsigned int)", "main"},
        calls=[("fact(int)", "fact(int)", False),
               ("is_even(unsigned int)", "is_odd(unsigned int)", False),
               ("is_odd(unsigned int)", "is_even(unsigned int)", False),
               ("main", "fact(int)", False), ("main", "is_even(unsigned int)", False),
               ("main", "is_odd(unsigned int)", False)],
        cycles=[["fact(int)"], ["is_even(unsigned int)", "is_odd(unsigned int)"]], globals=0),
    "14_switch_loop": dict(tus=1, functions={"classify(int)", "total(int)", "main"},
                           calls=[("main", "total(int)", False), ("total(int)", "classify(int)", False)],
                           cycles=[], globals=0),
    "15_struct_field": dict(tus=2, functions={"deposit(Account*, int)", "available(Account const&)", "main"},
                            calls=[("main", "deposit(Account*, int)", True),
                                   ("main", "available(Account const&)", True)],
                            cycles=[], globals=0),
    # The indirect call op(a, b) inside apply() is not resolved (see docs/KNOWN_ISSUES.md).
    "16_c_function_pointer": dict(tus=2, functions={"add", "mul", "apply", "main"},
                                  calls=[("main", "apply", True)] * 2, cycles=[], globals=0),
}

_CACHE = {}


def graph(name):
    if name not in _CACHE:
        files = sorted(str(p) for p in (CASES / name).glob("*.cir"))
        _CACHE[name] = to_json(build_program(files, name))
    return _CACHE[name]


def functions_by_id(data):
    return {f["id"]: f for f in data["layers"]["macro"]["functions"]}


def cfg_kinds(data, function):
    """{(kind, isBackEdge)} over the CFG edges of the function with this demangled name."""
    ids = [fid for fid, f in functions_by_id(data).items() if f["demangledName"] == function]
    return {(edge["kind"], bool(edge.get("isBackEdge")))
            for fid in ids for block in data["layers"]["meso"][fid]["blocks"]
            for edge in block.get("successors", [])}


def inter_flows(data, kind):
    """(source function, target function) for interprocedural flows of one kind."""
    names = {fid: f["demangledName"] for fid, f in functions_by_id(data).items()}
    owner = lambda value_id: names.get(value_id.split("#")[0], value_id)
    return {(owner(f["from"]), owner(f["to"]))
            for f in data["layers"]["bridges"]["interproceduralFlows"] if f["kind"] == kind}


class TestbenchCases(unittest.TestCase):
    def test_every_case_has_sources_cir_and_expectations(self):
        cases = sorted(p.name for p in CASES.iterdir() if p.is_dir())
        self.assertEqual(cases, sorted(EXPECTED))
        for name in cases:
            sources = [p for p in (CASES / name).iterdir() if p.suffix in (".c", ".cpp")]
            cirs = list((CASES / name).glob("*.cir"))
            self.assertEqual(sorted(p.stem for p in sources), sorted(p.stem for p in cirs), name)

    def test_macro_layer(self):
        for name, expected in EXPECTED.items():
            with self.subTest(case=name):
                data = graph(name)
                fns = functions_by_id(data)
                self.assertEqual(data["summary"]["translationUnits"], expected["tus"])
                self.assertEqual({f["demangledName"] for f in fns.values() if not f["isDeclaration"]},
                                 expected["functions"])
                calls = sorted((fns[c["caller"]]["demangledName"], fns[c["callee"]]["demangledName"],
                                c["isCrossTU"]) for c in data["layers"]["macro"]["calls"])
                self.assertEqual(calls, sorted(expected["calls"]))
                self.assertEqual(sorted(sorted(c) for c in data["summary"]["recursionCycles"]),
                                 sorted(expected["cycles"]))
                self.assertEqual(data["summary"]["globals"], expected["globals"])
                self.assertEqual(data["summary"]["entryPoints"], ["main"])
                self.assertEqual(data["summary"]["deadCode"], [])

    def test_static_symbols_stay_per_translation_unit(self):
        offsets = [f for f in functions_by_id(graph("08_static_function")).values()
                   if f["demangledName"] == "offset(int)"]
        self.assertEqual(len({f["tuId"] for f in offsets}), 2)
        self.assertTrue(all(f["isStatic"] for f in offsets))
        counters = graph("09_static_global")["layers"]["macro"]["globals"]
        self.assertEqual(sorted(g["initializer"] for g in counters), ["#cir.int<1>", "#cir.int<2>"])
        self.assertEqual(len({g["tuId"] for g in counters}), 2)

    def test_extern_global_resolves_to_its_definition(self):
        data = graph("07_extern_global")
        (shared,) = data["layers"]["macro"]["globals"]
        self.assertEqual((shared["name"], shared["sourceFile"], shared["isDeclaration"]),
                         ("shared_count", "state.cpp", False))
        self.assertEqual([a["global"] for a in data["layers"]["bridges"]["globalAccesses"]], [shared["id"]])

    def test_meso_layer_branch_loop_and_switch_edges(self):
        self.assertTrue({("true", False), ("false", False)} <= cfg_kinds(graph("03_if_else"), "choose(int)"))
        self.assertIn(("unconditional", True), cfg_kinds(graph("04_while_loop"), "sum_to_three()"))
        self.assertIn(("unconditional", True), cfg_kinds(graph("14_switch_loop"), "total(int)"))
        self.assertIn(("switch", False), cfg_kinds(graph("14_switch_loop"), "classify(int)"))
        self.assertEqual(cfg_kinds(graph("05_same_tu_call"), "double_value(int)"), set())

    def test_micro_layer_local_memory_flows(self):
        flows = graph("02_local_assignment")["summary"]["flowsByKind"]
        self.assertGreater(flows["store_to_load"], 0)
        self.assertGreater(flows["def_use"], 0)

    def test_bridges_arguments_and_returns(self):
        data = graph("10_three_tu_chain")
        self.assertEqual(inter_flows(data, "call_argument"), {("main", "first(int)"), ("first(int)", "second(int)")})
        self.assertEqual(inter_flows(data, "call_return"), {("first(int)", "main"), ("second(int)", "first(int)")})

    def test_reference_write_flows_back_to_caller(self):
        self.assertIn(("add_bonus(int&, int)", "main"),
                      inter_flows(graph("12_reference_side_effect"), "store_to_load"))

    def test_field_written_in_one_call_reaches_read_in_another(self):
        self.assertIn(("deposit(Account*, int)", "available(Account const&)"),
                      inter_flows(graph("15_struct_field"), "store_to_load"))

    def test_function_pointer_targets_are_address_taken_not_dead(self):
        fns = {f["demangledName"]: f for f in functions_by_id(graph("16_c_function_pointer")).values()}
        self.assertTrue(fns["add"]["isAddressTaken"] and fns["mul"]["isAddressTaken"])
        self.assertFalse(fns["add"]["isDeadCode"] or fns["mul"]["isDeadCode"])


@unittest.skipUnless(HAVE_KUZU, "kuzu not installed")
class TestbenchKuzu(unittest.TestCase):
    def test_all_cases_import_into_one_database(self):
        from cir_graph.cli import load_kuzu

        tmp = tempfile.mkdtemp(prefix="cir-graph-testbench-")
        try:
            db_path = Path(tmp) / "testbench.kuzu"
            for name in EXPECTED:
                files = sorted(str(p) for p in (CASES / name).glob("*.cir"))
                load_kuzu(import_statements(build_program(files, name)), db_path, reset=False)
            db = kuzu.Database(str(db_path), read_only=True)
            conn = kuzu.Connection(db)
            programs = conn.execute("MATCH (p:Program) RETURN count(p)").get_next()[0]
            cross = conn.execute("MATCH (:Function)-[c:CALLS]->(:Function) WHERE c.isCrossTU "
                                 "RETURN count(c)").get_next()[0]
            conn.close()
            db.close()
            self.assertEqual(programs, len(EXPECTED))
            self.assertEqual(cross, sum(c[2] for e in EXPECTED.values() for c in e["calls"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
