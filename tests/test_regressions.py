"""Regression cases found during the review for graph-only test generation."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from cir_graph.cir_parser import parse_cir_text, strip_locs
from cir_graph.exporters import to_json
from cir_graph.linker import build_program
from cir_graph.memory import MemoryModel
from cir_graph.schema import node_rows, rel_rows


def build_text(text):
    with tempfile.TemporaryDirectory(prefix="cir-graph-regression-") as directory:
        path = Path(directory) / "input.cir"
        path.write_text(text, encoding="utf-8")
        return build_program([str(path)], "regression")


class ParserRegressionTests(unittest.TestCase):
    def test_location_removal_preserves_literal_whitespace(self):
        text = 'cir.const  #cir.const_array<"a  b\\22 c"> : !bytes loc("x.c":1:2)'
        self.assertEqual(strip_locs(text),
                         'cir.const #cir.const_array<"a  b\\22 c"> : !bytes')

    def test_return_type_keeps_nested_spaces(self):
        module = parse_cir_text('''module @"x.c" {
  cir.func @identity(%p: !cir.ptr<!cir.int<s, 32>>) -> !cir.ptr<!cir.int<s, 32>> attributes {nothrow} {
    cir.return %p : !cir.ptr<!cir.int<s, 32>>
  }
}
''')
        self.assertEqual(module.functions[0].resultTypes,
                         ["!cir.ptr<!cir.int<s, 32>>"])


class GraphRegressionTests(unittest.TestCase):
    def test_goto_reaches_cleanup_and_backward_label(self):
        program = build_text('''module @"x.c" {
  cir.func @f(%condition: !cir.bool) -> !cir.int<s, 32> {
    %x = cir.alloca "x" : !cir.ptr<!cir.int<s, 32>>
    %one = cir.const #cir.int<1> : !cir.int<s, 32>
    cir.label "again"
    cir.store %one, %x : !cir.int<s, 32>, !cir.ptr<!cir.int<s, 32>>
    cir.if %condition {
      cir.goto "cleanup"
    }
    cir.goto "again"
  ^bb1:
    cir.label "cleanup"
    %result = cir.load %x : !cir.ptr<!cir.int<s, 32>>, !cir.int<s, 32>
    cir.return %result : !cir.int<s, 32>
  }
}
''')
        fn = program.all_functions()[0]
        blocks = {b.id: b for b in fn.blocks}
        gotos = [op for op in fn.operations if op.name == "cir.goto"]
        self.assertEqual(len(gotos), 2)
        for op, expected in zip(gotos, ("label.cleanup", "label.again")):
            edges = [e for e in fn.cfgEdges if e.src == op.blockId]
            self.assertEqual(len(edges), 1)
            self.assertEqual(blocks[edges[0].dst].name, expected)
            self.assertEqual(blocks[op.blockId].operations[-1], op.id)
            self.assertTrue(op.isTerminator)
        self.assertTrue(any(e.isBackEdge for e in fn.cfgEdges))
        result = next(v.id for v in fn.values.values() if v.name == "%result")
        sources = [fn.values[f.src].constValue for f in fn.flows
                   if f.kind == "store_to_load" and f.dst == result]
        self.assertEqual(sources, ["1"])

    def test_branch_arguments_are_exported_as_ssa_uses(self):
        program = build_text('''module @"x.c" {
  cir.func @f() -> !cir.int<s, 32> {
    %value = cir.const #cir.int<42> : !cir.int<s, 32>
    cir.br ^bb1(%value : !cir.int<s, 32>)
  ^bb1(%answer: !cir.int<s, 32>):
    cir.return %answer : !cir.int<s, 32>
  }
}
''')
        fn = program.all_functions()[0]
        branch = next(op for op in fn.operations if op.name == "cir.br")
        value = next(v.id for v in fn.values.values() if v.name == "%value")
        uses = [row["dst"] for row in rel_rows(program)["USES_VALUE"]
                if row["src"] == branch.id]
        self.assertEqual(uses, [value])
        data = to_json(program)["layers"]["micro"][fn.id]
        exported = next(op for op in data["operations"] if op["id"] == branch.id)
        self.assertEqual(exported["operands"], [value])
        self.assertEqual(exported["successorOperands"], [[value]])

    def test_unknown_pointer_store_does_not_become_a_known_alias(self):
        program = build_text('''module @"x.c" {
  cir.func private @unknown() -> !cir.ptr<!cir.int<s, 32>>
  cir.func @f(%condition: !cir.bool) -> !cir.int<s, 32> {
    %x = cir.alloca "x" : !cir.ptr<!cir.int<s, 32>>
    %p = cir.alloca "p" : !cir.ptr<!cir.ptr<!cir.int<s, 32>>>
    %one = cir.const #cir.int<1> : !cir.int<s, 32>
    cir.store %one, %x : !cir.int<s, 32>, !cir.ptr<!cir.int<s, 32>>
    cir.store %x, %p : !cir.ptr<!cir.int<s, 32>>, !cir.ptr<!cir.ptr<!cir.int<s, 32>>>
    cir.if %condition {
      %other = cir.call @unknown() : () -> !cir.ptr<!cir.int<s, 32>>
      cir.store %other, %p : !cir.ptr<!cir.int<s, 32>>, !cir.ptr<!cir.ptr<!cir.int<s, 32>>>
    }
    %loaded = cir.load %p : !cir.ptr<!cir.ptr<!cir.int<s, 32>>>, !cir.ptr<!cir.int<s, 32>>
    %seven = cir.const #cir.int<7> : !cir.int<s, 32>
    cir.store %seven, %loaded : !cir.int<s, 32>, !cir.ptr<!cir.int<s, 32>>
    %answer = cir.load %x : !cir.ptr<!cir.int<s, 32>>, !cir.int<s, 32>
    cir.return %answer : !cir.int<s, 32>
  }
}
''')
        fn = next(f for f in program.all_functions() if f.name == "f")
        loaded = next(v.id for v in fn.values.values() if v.name == "%loaded")
        self.assertIsNone(MemoryModel(fn, str).slot_of(loaded))

    def test_type_aliases_survive_json_and_database_rows(self):
        program = build_text('''!integer = !cir.int<s, 32>
!record = !cir.struct<"Pair" {data !integer, data !integer}>
#one = #cir.int<1> : !integer
module @"x.c" {
  cir.func @f(%p: !cir.ptr<!record>) -> !integer {
    %v = cir.const #one
    cir.return %v : !integer
  }
}
''')
        data = to_json(program)
        tu = data["layers"]["macro"]["translationUnits"][0]
        self.assertEqual(tu["typeAliases"]["!record"],
                         '!cir.struct<"Pair" {data !integer, data !integer}>')
        self.assertEqual(tu["attrAliases"]["#one"], "#cir.int<1> : !integer")
        row = node_rows(program)["TranslationUnit"][0]
        self.assertEqual(json.loads(row["typeAliases"]), tu["typeAliases"])
        self.assertEqual(json.loads(row["attrAliases"]), tu["attrAliases"])
        fn = data["layers"]["macro"]["functions"][0]
        self.assertEqual(fn["returnType"], "!integer")
        self.assertEqual(fn["paramTypes"], ["!cir.ptr<!record>"])

    def test_recursive_pointer_summary_terminates(self):
        text = '''module @"x.c" {
  cir.func @walk(%p: !cir.ptr<!cir.int<s, 32>>, %n: !cir.int<s, 32>) {
    %one = cir.const #cir.int<1> : !cir.int<s, 32>
    cir.store %one, %p : !cir.int<s, 32>, !cir.ptr<!cir.int<s, 32>>
    %next = cir.ptr_stride %p, %one : (!cir.ptr<!cir.int<s, 32>>, !cir.int<s, 32>) -> !cir.ptr<!cir.int<s, 32>>
    cir.call @walk(%next, %n) : (!cir.ptr<!cir.int<s, 32>>, !cir.int<s, 32>) -> ()
    cir.return
  }
  cir.func @caller(%p: !cir.ptr<!cir.int<s, 32>>) -> !cir.int<s, 32> {
    %one = cir.const #cir.int<1> : !cir.int<s, 32>
    cir.call @walk(%p, %one) : (!cir.ptr<!cir.int<s, 32>>, !cir.int<s, 32>) -> ()
    %next = cir.ptr_stride %p, %one : (!cir.ptr<!cir.int<s, 32>>, !cir.int<s, 32>) -> !cir.ptr<!cir.int<s, 32>>
    %answer = cir.load %next : !cir.ptr<!cir.int<s, 32>>, !cir.int<s, 32>
    cir.return %answer : !cir.int<s, 32>
  }
}
'''
        # Run separately so an unbounded summary expansion cannot hang the suite.
        with tempfile.TemporaryDirectory(prefix="cir-graph-recursion-") as directory:
            path = Path(directory) / "recursive.cir"
            path.write_text(text, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "cir_graph.cli", str(path),
                 "--json", str(Path(directory) / "graph.json")],
                text=True, capture_output=True, timeout=10,
                env=dict(os.environ, PYTHONPATH=str(REPO / "src")))
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((Path(directory) / "graph.json").read_text(encoding="utf-8"))
            self.assertGreater(data["summary"]["flowsByKind"]["store_to_load"], 0)
            self.assertEqual(len(data["summary"]["recursionCycles"]), 1)


class AnalyzeScriptTests(unittest.TestCase):
    def test_directory_with_sources_and_cir_uses_each_translation_unit_once(self):
        # testbench cases hold both .cpp and committed .cir; with a CIR clang on the
        # machine this used to compile the sources *and* add the .cir (6 TUs, not 3).
        case = REPO / "testbench" / "cases" / "10_three_tu_chain"
        with tempfile.TemporaryDirectory(prefix="cir-graph-dir-") as directory:
            result = subprocess.run(
                ["bash", str(REPO / "analyze.sh"), str(case), "--no-database", "--no-svg",
                 "--output-dir", directory],
                env=dict(os.environ, PYTHON=sys.executable), capture_output=True, text=True,
                timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((Path(directory) / "graph.json").read_text(encoding="utf-8"))
            self.assertEqual(data["summary"]["translationUnits"], 3)
            self.assertFalse((Path(directory) / "cir").exists())


@unittest.skipUnless(os.environ.get("CLANG"), "set CLANG to a CIR-enabled compiler")
class PipelineRegressionTests(unittest.TestCase):
    def test_source_stems_cannot_overwrite_another_translation_unit(self):
        with tempfile.TemporaryDirectory(prefix="cir-graph-stems-") as directory:
            root = Path(directory)
            sources = []
            for index, relative in enumerate(("left/unit.c", "right/unit.c", "unit_2.c")):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"int function_{index}(void) {{ return {index}; }}\n",
                                encoding="utf-8")
                sources.append(str(path))
            env = dict(os.environ, PYTHON=sys.executable)
            result = subprocess.run(
                ["bash", str(REPO / "analyze.sh"), *sources,
                 "--no-database", "--output-dir", str(root / "out"), "--program-id", "stems"],
                env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / "out" / "graph.json").read_text(encoding="utf-8"))
            names = {f["name"] for f in data["layers"]["macro"]["functions"]}
            self.assertEqual(names, {"function_0", "function_1", "function_2"})
            self.assertEqual(data["summary"]["translationUnits"], 3)


if __name__ == "__main__":
    unittest.main()
