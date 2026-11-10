"""Independent multi-prefix declaration filters; preserve legacy output."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from cir_graph.public_api.base import extract


class FilterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('clang'),'clang required')
    def test_postgresql_names_without_transitive_system_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'api.h'
            p.write_text('''#include <stdlib.h>
            #define PG_COPYRES_ATTRS 1
            #define PG_DIAG_SAMPLE 2
            #define PQ_VERSION 18
            #define PQ_APPLY(x) ((x)+1)
            typedef unsigned int Oid;
            typedef struct {char *name; Oid typid; int format;} PGresAttDesc;
            typedef enum {PGRES_BAD=-1,PGRES_OK} ExecStatusType;
            typedef enum {CONNECTION_OK,CONNECTION_BAD} ConnStatusType;
            typedef struct hidden PGresult;
            PGresult *PQmakeEmptyPGresult(void *conn,ExecStatusType status);
            static int PQhidden(void){return 918273645;}
            int unrelated(void);
            ''')
            ast=json.loads(subprocess.check_output(['clang','-x','c','-Xclang','-ast-dump=json','-fsyntax-only',str(p)],text=True))
            macros=subprocess.check_output(['clang','-E','-dM',str(p)],text=True)
        data=extract(ast,macros,function_prefixes=['PQ'],type_prefixes=['PG','PQ'],
                     type_names=['Oid','ExecStatusType','ConnStatusType'],
                     constant_prefixes=['PGRES_','CONNECTION_','PG_COPYRES_','PG_DIAG_','PQ_'])
        self.assertIn('PQmakeEmptyPGresult',data['functions'])
        self.assertIn('Oid',data['types']);self.assertIn('PGresult',data['types'])
        self.assertEqual([x['name'] for x in data['types']['PGresAttDesc']['fields']],['name','typid','format'])
        self.assertEqual(data['constants']['PGRES_BAD']['value'],-1)
        self.assertEqual(data['constants']['PGRES_OK']['value'],0)
        self.assertIn('PG_COPYRES_ATTRS',data['constants']);self.assertNotIn('PQ_APPLY',data['constants'])
        self.assertNotIn('malloc',data['functions']);self.assertNotIn('unrelated',data['functions'])
        self.assertNotIn('size_t',data['types']);self.assertNotIn('918273645',json.dumps(data))
        self.assertEqual(extract(ast,macros),extract(ast,macros,function_prefixes=['git_'],constant_prefixes=['GIT_']))

    def test_empty_prefix_is_not_a_safe_allowlist(self):
        with self.assertRaises(ValueError):extract({},'',function_prefixes=[''])


if __name__=='__main__':unittest.main()
