"""Public declaration graph facts must preserve C names, without bodies."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from cir_graph.public_api.base import extract
class PublicApiTests(unittest.TestCase):
    def test_clang_declarations_and_constants(self):
        clang=shutil.which("clang")
        if not clang:
            self.skipTest("Clang unavailable")
        with tempfile.TemporaryDirectory() as directory:
            header=Path(directory)/"api.h"
            header.write_text("""#define GIT_INIT { 0, 0 }
#define GIT_APPLY(x) ((x)+1)
typedef struct { char *ptr; unsigned long size; } git_buf;
typedef enum { GIT_NEG=-3, GIT_NEXT, GIT_FLAG=1<<4 } git_kind;
int git_output(git_buf *out, const char *input);
static int git_hidden(void) { return 123456789; }
""")
            ast=json.loads(subprocess.check_output([clang,"-x","c","-Xclang","-ast-dump=json","-fsyntax-only",str(header)],text=True))
            macros=subprocess.check_output([clang,"-E","-dM",str(header)],text=True)
            data=extract(ast,macros)
        self.assertEqual(data["types"]["git_buf"]["fields"][0],{"name":"ptr","c_type":"char *"})
        self.assertEqual(data["constants"]["GIT_NEG"]["value"],-3)
        self.assertEqual(data["constants"]["GIT_NEXT"]["value"],-2)
        self.assertEqual(data["constants"]["GIT_FLAG"]["value"],16)
        self.assertEqual(data["types"]["git_kind"]["enumerators"][0],{"name":"GIT_NEG","value":-3})
        self.assertEqual(data["constants"]["GIT_INIT"]["expression"],"{ 0, 0 }")
        self.assertNotIn("GIT_APPLY",data["constants"])
        self.assertEqual(data["functions"]["git_output"]["parameters"][0]["c_type"],"git_buf *")
        self.assertNotIn("123456789",json.dumps(data))
if __name__=="__main__": unittest.main()
