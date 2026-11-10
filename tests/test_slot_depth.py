"""Pointer dependency resolution must not depend on Python recursion depth."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cir_graph.memory import MemoryModel

class SlotDepthTests(unittest.TestCase):
    def model(self, count, cycle=False):
        values = {str(i): SimpleNamespace(type="!cir.ptr<!s32i>")
                  for i in range(count + 1)}
        operations = [SimpleNamespace(id="op"+str(i), name="cir.cast",
                       operands=[str(i + 1)], results=[str(i)],
                       text="cir.cast bitcast") for i in range(count)]
        operations.append(SimpleNamespace(id="root", name="cir.cast" if cycle else "cir.alloca",
                          operands=["0"] if cycle else [], results=[str(count)],
                          text="cir.cast bitcast" if cycle else "cir.alloca"))
        fn = SimpleNamespace(values=values, operations=operations, parameters=[],
                             blocks=[], cfgEdges=[])
        return MemoryModel(fn, lambda name: name)

    def test_deep_chain(self):
        model = self.model(3000)
        self.assertEqual(model.slot_of("0"), (("L", "3000"),))
        self.assertEqual(len(model._memo), 3001)
    def test_deep_cycle_stays_unknown(self):
        self.assertIsNone(self.model(3000, cycle=True).slot_of("0"))
    def test_error_does_not_poison_pending_cache(self):
        model = self.model(3)
        def broken(vid):
            raise ValueError("bad input")
        model._resolve = broken
        with self.assertRaises(ValueError):
            model.slot_of("0")
        self.assertEqual(model._memo, {})
        self.assertFalse(model._resolving)
if __name__ == "__main__":
    unittest.main()
