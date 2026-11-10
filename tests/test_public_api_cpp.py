"""C++ declaration access, overload and default facts from a real Clang AST."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cir_graph.public_api.cpp import extract_cpp


@unittest.skipUnless(shutil.which("clang++"), "Clang C++ frontend is required")
class CppDeclarationFacts(unittest.TestCase):
    def test_access_qualified_names_overloads_defaults_and_no_bodies(self):
        code = '''namespace Demo {
        class Value {
            int hidden();
        protected: int protectedMethod();
        public:
            Value(int n = -1);
            static int parse(const char *text, int base = 10);
            int size() const { return 123456789; }
            int size(int maximum) const;
            void reset(bool enabled = true);
            void *pointer(void *p = nullptr);
            void removed() = delete;
            enum Status { Negative = -3, Next, Flag = 1 << 4 };
            int publicOutput;
        private: int secret;
        };
        struct PublicStruct { int visible(int n = 7); };
        class Other { public: int leak(); };
        }'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.cpp"
            path.write_text(code, encoding="utf-8")
            raw = subprocess.check_output(["clang++", "-std=c++17", "-Xclang", "-ast-dump=json",
                                           "-fsyntax-only", str(path)], text=True)
        facts = extract_cpp(json.loads(raw), ["Demo::Value", "Demo::PublicStruct"])
        methods = list(facts["functions"].values())
        self.assertEqual(facts["missing_types"], [])
        self.assertEqual(len([m for m in methods if m["name"] == "Demo::Value::size"]), 2)
        self.assertFalse(any("hidden" in m["name"] or "protected" in m["name"] or "leak" in m["name"] for m in methods))
        parse = next(m for m in methods if m["name"].endswith("::parse"))
        self.assertTrue(parse["static"])
        self.assertEqual(parse["parameters"][1]["default"]["value"], "10")
        constructor = next(m for m in methods if m["kind"] == "CXXConstructorDecl")
        self.assertEqual(constructor["parameters"][0]["default"]["operator"], "-")
        reset = next(m for m in methods if m["name"].endswith("::reset"))
        self.assertIs(reset["parameters"][0]["default"]["value"], True)
        removed = next(m for m in methods if m["name"].endswith("::removed"))
        self.assertTrue(removed["deleted"])
        record = facts["types"]["Demo::Value"]
        self.assertEqual(record["fields"], [{"name": "publicOutput", "cpp_type": "int"}])
        self.assertEqual([item["value"] for item in record["enums"][0]["enumerators"]], [-3, -2, 16])
        self.assertNotIn("123456789", json.dumps(facts))
        self.assertNotIn("CompoundStmt", json.dumps(facts))
        missing = extract_cpp(json.loads(raw), ["Demo::Missing"])
        self.assertEqual(missing["missing_types"], ["Demo::Missing"])


if __name__ == "__main__":
    unittest.main()
