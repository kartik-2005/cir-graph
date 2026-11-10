from pathlib import Path
import sys
import tempfile
import unittest
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'src'))
from cir_graph.project_builder import build_program
from cir_graph.linker import build_program as historical
from cir_graph.exporters import to_json

CASE='''!i = !cir.int<s, 32>
module @"switch.cpp" {
  cir.func @choose(%arg0: !i) -> !i {
    cir.switch(%arg0 : !i) {
      cir.br ^bb1
    ^bb1:
      cir.case(equal, [#cir.int<1> : !i]) {
        %0 = cir.const #cir.int<10> : !i
        cir.return %0 : !i
      }
      cir.case(default, []) {
        %1 = cir.const #cir.int<20> : !i
        cir.return %1 : !i
      }
      cir.yield
    }
    cir.unreachable
  }
}
'''

class ProjectBuilderTests(unittest.TestCase):
    def test_forwarder_after_terminating_case_preserves_that_case(self):
        text=CASE.replace('      cir.br ^bb1', '''      cir.case(equal, [#cir.int<2> : !i]) {
        %90 = cir.const #cir.int<30> : !i
        cir.return %90 : !i
      }
    ^dead:
      cir.br ^bb1''')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'case.cir';path.write_text(text)
            program=build_program([str(path)],'new');fn=program.all_functions()[0]
            self.assertEqual(sum(e.kind=='switch' for e in fn.cfgEdges),3)
            self.assertEqual(sum(o.name=='cir.return' for o in fn.operations),3)
            self.assertFalse(any(o.name=='cir.br' for o in fn.operations))

    def test_forwarding_switch_has_only_guarded_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'case.cir';path.write_text(CASE)
            with self.assertRaises(ValueError):historical([str(path)],'old')
            program=build_program([str(path)],'new');fn=program.all_functions()[0]
            self.assertEqual(len(program.projectNormalizations),1)
            switches=[e for e in fn.cfgEdges if e.kind=='switch']
            self.assertEqual(len(switches),2)
            self.assertTrue(all(e.conditionValueId for e in switches))
            self.assertFalse(any(o.name=='cir.br' for o in fn.operations))
            self.assertEqual(sum(o.name=='cir.return' for o in fn.operations),2)

    def test_side_effectful_entry_is_not_silently_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'case.cir';path.write_text(CASE.replace('      cir.br ^bb1','      %99 = cir.const #cir.int<99> : !i\n      cir.br ^bb1'))
            with self.assertRaises(ValueError):build_program([str(path)],'unsafe')

    def test_unchanged_canonical_inputs_match_historical_builder(self):
        for folder in ('03_if_else','04_while_loop','08_static_function'):
            paths=[str(p) for p in sorted((HERE.parent/'testbench'/'cases'/folder).glob('*.cir'))]
            self.assertEqual(to_json(build_program(paths,'same')),to_json(historical(paths,'same')))

if __name__=='__main__':unittest.main()
