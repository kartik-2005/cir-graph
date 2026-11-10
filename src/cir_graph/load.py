"""Load a Cypher import script (written by ``cir-graph --output``) into Kùzu.

  cir-graph-load out/graph.cypher --database out/graph.kuzu [--reset]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from .cli import load_kuzu


def split_cypher(script: str) -> list[str]:
    """Split Cypher on top-level semicolons without breaking literals/comments."""

    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = 0

    while index < len(script):
        char = script[index]
        following = script[index + 1] if index + 1 < len(script) else ""

        if line_comment:
            current.append(char)
            if char == "\n":
                line_comment = False
            index += 1
            continue

        if block_comment:
            current.append(char)
            if char == "*" and following == "/":
                current.append(following)
                block_comment = False
                index += 2
            else:
                index += 1
            continue

        if quote is not None:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote != "`":
                escaped = True
            elif char == quote:
                if following == quote:
                    current.append(following)
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char == "/" and following == "/":
            current.extend((char, following))
            line_comment = True
            index += 2
            continue
        if char == "/" and following == "*":
            current.extend((char, following))
            block_comment = True
            index += 2
            continue
        if char in ("'", '"', "`"):
            quote = char
            current.append(char)
            index += 1
            continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current.clear()
            index += 1
            continue

        current.append(char)
        index += 1

    if quote is not None:
        raise ValueError(f"unterminated {quote} literal in Cypher file")
    if block_comment:
        raise ValueError("unterminated block comment in Cypher file")

    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements




def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cir-graph-load", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("script", type=Path, help="Cypher script written by cir-graph --output")
    parser.add_argument("--database", "-d", type=Path, required=True, help="Kùzu database path")
    parser.add_argument("--reset", action="store_true", help="delete the database before importing")
    args = parser.parse_args(argv)
    statements = [s for s in split_cypher(args.script.read_text(encoding="utf-8")) if s.strip()]
    load_kuzu(statements, args.database, args.reset)
    print(f"[+] Loaded {len(statements)} statement(s) into {args.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
