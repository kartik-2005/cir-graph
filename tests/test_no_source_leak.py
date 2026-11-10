"""The graph replaces the source for the AI model, so exports must not carry source text.

Identifiers, types, literals and file/line locations are program facts and do
appear. Comments and verbatim source lines must not: they would let a model
read the original code through the graph.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CASES = REPO / "testbench" / "cases"
sys.path.insert(0, str(REPO / "src"))

from cir_graph.exporters import to_dot, to_json  # noqa: E402
from cir_graph.linker import build_program  # noqa: E402
from cir_graph.schema import import_statements  # noqa: E402


def normalize(text: str) -> str:
    return " ".join(text.split())


def comments(source: str):
    found = re.findall(r"//([^\n]*)", source) + re.findall(r"/\*(.*?)\*/", source, re.S)
    return {normalize(c) for c in found if len(normalize(c)) >= 12}


def code_lines(source: str):
    """Distinctive statement lines (long enough not to collide with IR text by chance)."""
    lines = set()
    for line in source.splitlines():
        line = line.strip()
        if len(line) >= 25 and not line.startswith(("#", "//", "/*", "*")) and any(c in line for c in "=();"):
            lines.add(normalize(line))
    return lines


class NoSourceTextInGraph(unittest.TestCase):
    def test_exports_contain_no_comments_or_source_lines(self):
        checked_comments = checked_lines = 0
        for case in sorted(p for p in CASES.iterdir() if p.is_dir()):
            source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(case.iterdir())
                               if p.suffix in (".c", ".cpp", ".h"))
            program = build_program(sorted(str(p) for p in case.glob("*.cir")), case.name)
            exports = {
                "json": normalize(json.dumps(to_json(program))),
                "cypher": normalize("\n".join(import_statements(program))),
                "dot": normalize(to_dot(program)),
            }
            for kind, text in exports.items():
                for comment in comments(source):
                    self.assertNotIn(comment, text, f"{case.name}: comment leaked into {kind}")
                for line in code_lines(source):
                    self.assertNotIn(line, text, f"{case.name}: source line leaked into {kind}")
            checked_comments += len(comments(source))
            checked_lines += len(code_lines(source))
        # Guard against the check silently matching nothing.
        self.assertGreaterEqual(checked_comments, 4)
        self.assertGreaterEqual(checked_lines, 20)


if __name__ == "__main__":
    unittest.main()
