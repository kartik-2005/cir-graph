"""Optional public-API metadata extracted from Clang's JSON AST.

CIR lowers away enum names, macros and declaration-only details; these helpers
recover public declarations as graph metadata (no bodies or comments).

  base       C headers by name prefix   (python -m cir_graph.public_api.base)
  cpp        C++ headers                 (python -m cir_graph.public_api.cpp)
  c_project  configured C projects       (python -m cir_graph.public_api.c_project)
"""
