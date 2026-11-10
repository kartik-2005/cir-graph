"""cir_graph: unified Clang IR (CIR) knowledge graph — call graph, CFG and data flow.

Typical use::

    from cir_graph.linker import build_program
    from cir_graph.exporters import to_json

    program = build_program(["main.cir", "util.cir"], "demo")
    data = to_json(program)
"""

__version__ = "0.1.0"
