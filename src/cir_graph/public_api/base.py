"""Export public declaration facts as graph metadata; no bodies or comments.

This optional layer complements CIR, which lowers away C enum names and macros.
It is not an ownership/behavior contract or a copy of a source/header file.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

def extract(ast, macros, function_prefix="git_", constant_prefix="GIT_", *,
            function_prefixes=None, type_prefixes=None, type_names=None, constant_prefixes=None):
    function_prefixes = tuple(function_prefixes if function_prefixes is not None else [function_prefix])
    constant_prefixes = tuple(constant_prefixes if constant_prefixes is not None else [constant_prefix])
    type_prefixes = tuple(type_prefixes if type_prefixes is not None else function_prefixes)
    type_names = set(type_names or [])
    if any(not prefix for prefix in (*function_prefixes, *constant_prefixes, *type_prefixes)):
        raise ValueError("Empty declaration prefixes would expose unrelated transitive declarations.")
    selected_type = lambda name: bool(name) and (name in type_names or name.startswith(type_prefixes))
    functions, types, constants = {}, {}, {}
    records, enums, enum_aliases = {}, {}, {}
    def collect(node):
        if node.get("kind") == "EnumDecl":
            enums[node.get("id")] = [c["name"] for c in node.get("inner", []) if c.get("kind") == "EnumConstantDecl"]
        if node.get("kind") == "RecordDecl":
            records[node.get("id")] = [{"name": c.get("name", ""), "c_type": c["type"]["qualType"]}
                for c in node.get("inner", []) if c.get("kind") == "FieldDecl"]
        for child in node.get("inner", []):
            collect(child)
    collect(ast)
    def record_fields(node):
        for key in ("ownedTagDecl", "decl"):
            found = records.get(node.get(key, {}).get("id"))
            if found:
                return found
        for child in node.get("inner", []):
            found = record_fields(child)
            if found:
                return found
        return []
    def enum_ids(node):
        found=[]
        for key in ("ownedTagDecl", "decl"):
            identifier=node.get(key, {}).get("id")
            if identifier in enums:found.append(identifier)
        for child in node.get("inner", []):found.extend(enum_ids(child))
        return found
    def number(node):
        if node.get("kind") == "ConstantExpr" and "value" in node:
            return int(node["value"], 0)
        for child in node.get("inner", []):
            value = number(child)
            if value is not None:
                return value
        if node.get("kind") == "IntegerLiteral":
            return int(node["value"], 0)
        return None
    def walk(node):
        name, kind = node.get("name", ""), node.get("kind")
        if kind == "FunctionDecl" and name.startswith(function_prefixes):
            functions[name] = {"name": name, "c_type": node["type"]["qualType"],
                "parameters": [{"name": p.get("name", ""), "c_type": p["type"]["qualType"]}
                    for p in node.get("inner", []) if p.get("kind") == "ParmVarDecl"]}
            return  # Function bodies, comments and statements are never exported.
        if kind == "TypedefDecl" and selected_type(name):
            types.setdefault(name, {"name": name})["c_type"] = node["type"]["qualType"]
            enum_aliases[name] = enum_ids(node)
            fields = record_fields(node)
            if fields:
                types[name]["fields"] = fields
        if kind == "RecordDecl" and selected_type(name):
            fields = [{"name": p.get("name", ""), "c_type": p["type"]["qualType"]}
                      for p in node.get("inner", []) if p.get("kind") == "FieldDecl"]
            if fields:
                types.setdefault(name, {"name": name})["fields"] = fields
        if kind == "EnumDecl":
            previous = -1
            for child in node.get("inner", []):
                if child.get("kind") != "EnumConstantDecl":
                    continue
                value = number(child)
                previous = previous + 1 if value is None else value
                symbol = child.get("name", "")
                if symbol.startswith(constant_prefixes):
                    constants[symbol] = {"name": symbol, "kind": "enum", "value": previous,
                        "enum_type": name or None}
        for child in node.get("inner", []):
            walk(child)
    walk(ast)
    for alias, identifiers in enum_aliases.items():
        names=[name for identifier in set(identifiers) for name in enums[identifier] if name in constants]
        if names:
            types[alias]["enumerators"]=[{"name":name,"value":constants[name]["value"]} for name in names]
            for name in names:constants[name]["enum_type"]=alias
    pattern = re.compile(r"^#define (\w+)\s+(.*)$")
    for line in macros.splitlines():
        match = pattern.match(line)
        if match and match.group(1).startswith(constant_prefixes):
            name, value = match.groups()
            constants.setdefault(name, {"name": name, "kind": "macro", "expression": value})
    return {"schemaVersion": "v3-public-api-1", "provenance": "Clang public declarations and object-like macros",
            "limitations": ["No bodies, source excerpts, comments, tests or inferred behavioral contracts.",
                             "Macro expressions are preserved, not evaluated; function-like macros are excluded."],
            "functions": functions, "types": types, "constants": constants}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("header", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--define", action="append", default=[])
    parser.add_argument("--function-prefix", action="append", help="Allowed function prefix; repeatable. Default git_.")
    parser.add_argument("--constant-prefix", action="append", help="Allowed constant prefix; repeatable. Default GIT_.")
    parser.add_argument("--type-prefix", action="append", help="Independent allowed type prefix; repeatable.")
    parser.add_argument("--type-name", action="append", help="Additional exact C type name; repeatable.")
    args = parser.parse_args()
    flags = [args.clang, "-x", "c", "-std=c99", *["-I"+x for x in args.include], *["-D"+x for x in args.define]]
    commands = [flags + ["-Xclang", "-ast-dump=json", "-fsyntax-only", str(args.header)],
                flags + ["-E", "-dM", str(args.header)]]
    results = [subprocess.run(c, check=True, capture_output=True, text=True) for c in commands]
    data = extract(json.loads(results[0].stdout), results[1].stdout,
                   function_prefixes=args.function_prefix or ["git_"],
                   constant_prefixes=args.constant_prefix or ["GIT_"],
                   type_prefixes=args.type_prefix, type_names=args.type_name)
    data["commands"] = commands
    data["compiler_version"] = subprocess.check_output([args.clang, "--version"], text=True)
    data["public_header_sha256"] = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in args.include for p in sorted(Path(folder).rglob("*.h"))}
    data["header_sha256"] = hashlib.sha256(args.header.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:len(data[k]) for k in ("functions","types","constants")}))
if __name__ == "__main__":
    main()
