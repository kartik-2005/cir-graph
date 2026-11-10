from pathlib import Path
import os
import sys
import unittest
import json
import shutil
import subprocess
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from cir_graph.public_api.c_project import function_macros

class CProjectApiTests(unittest.TestCase):
    def test_function_macro_names_and_params_without_expansions(self):
        text='#define bfd_call(abfd, location) ((abfd)->xvec->call((abfd), (location)))\n#define bfd_flag 4\n#define other_fn(x) secret_body(x)\n'
        result=function_macros(text,['bfd_'])
        self.assertEqual(set(result),{'bfd_call'})
        self.assertEqual(result['bfd_call']['parameters'],['abfd','location'])
        self.assertEqual(result['bfd_call']['parameter_types'],'not available')
        self.assertNotIn('xvec',str(result))
        self.assertNotIn('secret_body',str(result))
    def test_empty_argument_and_variadic_macros(self):
        r=function_macros('#define bfd_empty() 1\n#define bfd_log(fmt, ...) consume(fmt, __VA_ARGS__)',['bfd_'])
        self.assertEqual(r['bfd_empty']['parameters'],[])
        self.assertTrue(r['bfd_log']['variadic'])
    def test_empty_prefix_rejected(self):
        with self.assertRaises(ValueError):function_macros('#define any(x) x',[''])

    @unittest.skipUnless(shutil.which('clang'),'clang required')
    def test_cli_defaults_preserve_function_prefix_types(self):
        with tempfile.TemporaryDirectory() as folder:
            header=Path(folder)/'input.h';output=Path(folder)/'api.json'
            header.write_text('typedef struct bfd_item { int value; } bfd_item;\nint bfd_consume(bfd_item *item);\n#define bfd_forward(x) bfd_consume(x)\n')
            subprocess.run([sys.executable,'-m','cir_graph.public_api.c_project',str(header),
                '--output',str(output),'--function-prefix','bfd_','--constant-prefix','BFD_'],check=True,capture_output=True,
                env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')))
            data=json.loads(output.read_bytes())
            self.assertIn('bfd_item',data['types'])
            self.assertEqual(data['function_macros']['bfd_forward']['parameters'],['x'])
            self.assertEqual(data['types']['bfd_item']['fields'][0]['name'],'value')

if __name__=='__main__':unittest.main()
