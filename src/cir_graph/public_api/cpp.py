"""Export selected C++ public declarations, without bodies or source excerpts.

This is a separate schema/tool. The existing C exporter stays unchanged.
Templates and inherited members need separate resolution; they are not expanded.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def extract_cpp(ast, type_names):
    selected = set(type_names)
    if not selected or any(not name for name in selected):
        raise ValueError("Select explicit qualified C++ type names.")
    types, functions = {}, {}
    owners = {}

    def index(node, scope=()):
        kind, name = node.get("kind"), node.get("name", "")
        if name and node.get("id"):
            owners[node["id"]] = "::".join((*scope, name))
        nested = (*scope, name) if name and kind in (
            "NamespaceDecl", "CXXRecordDecl", "ClassTemplateDecl", "EnumDecl") else scope
        for child in node.get("inner", []):
            index(child, nested)
    index(ast)

    def default(node):
        kind = node.get("kind")
        if kind in ("IntegerLiteral", "FloatingLiteral", "StringLiteral", "CharacterLiteral", "CXXBoolLiteralExpr"):
            return {"kind": kind, "value": node.get("value")}
        if kind == "CXXNullPtrLiteralExpr":
            return {"kind": kind, "value": "nullptr"}
        inner = node.get("inner", [])
        if kind in ("ImplicitCastExpr", "ConstantExpr", "ExprWithCleanups", "ParenExpr") and len(inner) == 1:
            return default(inner[0])
        if kind == "UnaryOperator" and len(inner) == 1 and node.get("opcode") in ("-", "+"):
            return {"kind": "unary", "operator": node["opcode"], "operand": default(inner[0])}
        if kind == "DeclRefExpr":
            decl = node.get("referencedDecl", {})
            return {"kind": "declaration", "name": owners.get(decl.get("id"), decl.get("name"))}
        # Retain uncertainty instead of guessing a value or exporting source.
        return {"kind": "unresolved", "ast_kind": kind}

    def parameter(node):
        result = {"name": node.get("name", ""), "cpp_type": node["type"]["qualType"]}
        if "init" in node:
            inner = node.get("inner", [])
            result["default"] = default(inner[-1]) if inner else {"kind": "unresolved"}
        return result

    def visit(node, scope=()):
        kind, name = node.get("kind"), node.get("name", "")
        if kind == "NamespaceDecl":
            for child in node.get("inner", []):
                visit(child, (*scope, name) if name else scope)
            return
        if kind == "CXXRecordDecl" and node.get("completeDefinition") and not node.get("isImplicit"):
            qualified = "::".join((*scope, name))
            if qualified not in selected:
                return
            record = {"name": qualified, "kind": node.get("tagUsed", "class"), "methods": [], "fields": [], "enums": [],
                      "bases": [{"cpp_type": b["type"]["qualType"], "access": b.get("access")}
                                for b in node.get("bases", [])]}
            access = "private" if node.get("tagUsed") == "class" else "public"
            for child in node.get("inner", []):
                child_kind = child.get("kind")
                if child_kind == "AccessSpecDecl":
                    access = child["access"]
                elif child_kind == "FieldDecl" and access == "public":
                    record["fields"].append({"name": child.get("name", ""), "cpp_type": child["type"]["qualType"]})
                elif child_kind == "EnumDecl" and access == "public":
                    enum = {"name": qualified+"::"+child.get("name", ""), "enumerators": []}
                    previous = -1
                    for symbol in child.get("inner", []):
                        if symbol.get("kind") != "EnumConstantDecl":
                            continue
                        inner = symbol.get("inner", [])
                        value = inner[0] if inner else None
                        if value and value.get("kind") in ("ConstantExpr", "IntegerLiteral") and "value" in value:
                            previous = int(value["value"], 0)
                        elif value:
                            previous = None
                        elif previous is not None:
                            previous += 1
                        enum["enumerators"].append({"name": symbol["name"], "value": previous})
                    record["enums"].append(enum)
                elif child_kind in ("CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl", "CXXConversionDecl"):
                    if access != "public" or child.get("isImplicit"):
                        continue
                    full_name = qualified + "::" + child["name"]
                    signature = child["type"]["qualType"]
                    key = full_name + " | " + signature
                    fact = {"name": full_name, "cpp_type": signature, "kind": child_kind,
                            "static": child.get("storageClass") == "static",
                            "deleted": bool(child.get("explicitlyDeleted")),
                            "parameters": [parameter(p) for p in child.get("inner", [])
                                           if p.get("kind") == "ParmVarDecl"]}
                    # Stable signature keys preserve overloads and avoid raw AST IDs.
                    functions[key] = fact
                    record["methods"].append(key)
                elif child_kind == "CXXRecordDecl" and access == "public":
                    visit(child, (*scope, name))
            record["methods"] = sorted(set(record["methods"]))
            types[qualified] = record
            return
        # Do not walk class templates or method bodies as ordinary public APIs.
        if kind not in ("ClassTemplateDecl", "FunctionTemplateDecl", "FunctionDecl", "CompoundStmt"):
            for child in node.get("inner", []):
                visit(child, scope)
    visit(ast)
    return {"schemaVersion": "public-api-cpp-1", "types": types, "functions": functions,
            "missing_types": sorted(selected - types.keys()),
            "limitations": ["Selected declared public members only; no source excerpts, bodies or behavioral contracts.",
                            "No template instantiation, inherited-member expansion, global enum/macro catalog or access inference.",
                            "Unsupported default expressions are explicit unresolved facts."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("header", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clang", default="clang++")
    parser.add_argument("--std", default="c++17")
    parser.add_argument("--type-name", action="append", required=True)
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--define", action="append", default=[])
    args = parser.parse_args()
    command = [args.clang, "-x", "c++", "-std=" + args.std,
               *["-I" + path for path in args.include], *["-D" + value for value in args.define],
               "-Xclang", "-ast-dump=json", "-fsyntax-only", str(args.header)]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    data = extract_cpp(json.loads(result.stdout), args.type_name)
    if data["missing_types"]:
        raise ValueError("Requested definitions missing: " + ", ".join(data["missing_types"]))
    data["command"] = command
    data["compiler_version"] = subprocess.check_output([args.clang, "--version"], text=True)
    data["header_sha256"] = hashlib.sha256(args.header.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"types": len(data["types"]), "functions": len(data["functions"])}))


if __name__ == "__main__":
    main()
