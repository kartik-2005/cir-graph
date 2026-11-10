"""Disk graph identity, global links, exact body preservation and query bounds."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'src'))
from cir_graph.disk_store import DiskGraph
from cir_graph.linker import build_program
from cir_graph.exporters import to_json

class DiskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'graph.sqlite'
        self.store=DiskGraph(self.path,'project')
    def tearDown(self):self.store.close();self.temp.cleanup()

    def add(self,folder):
        graphs=[]
        for index,path in enumerate(sorted((HERE.parent/'testbench'/'cases'/folder).glob('*.cir'))):
            graph=to_json(build_program([str(path)],f'project@tu{index}'))
            self.store.add_graph(graph);graphs.append(graph)
        self.store.link();return graphs

    def test_links_cross_tu_chain_and_bindings(self):
        self.add('10_three_tu_chain')
        main=self.store.find('main',exact_method=True)[0][0]
        first=self.store.find('_Z5firsti',exact_method=True)[0][0]
        second=self.store.find('_Z6secondi',exact_method=True)[0][0]
        self.assertEqual(main['callees'],[first['id']]);self.assertEqual(first['callees'],[second['id']])
        self.assertEqual(first['callers'],[main['id']])
        bindings,total=self.store.bindings(main['id'])
        self.assertEqual(total,1);self.assertEqual(bindings[0]['callee'],first['id'])
        self.assertTrue(bindings[0]['argumentBindings']);self.assertTrue(bindings[0]['returnBindings'])
        self.assertEqual(self.store.meta('summary')['crossTUCalls'],2)

    def test_body_is_lossless(self):
        graphs=self.add('03_if_else')
        f=next(f for f in graphs[0]['layers']['macro']['functions'] if not f.get('isDeclaration'))
        cfg,micro=self.store.body(f['id'])
        self.assertEqual(cfg,graphs[0]['layers']['meso'][f['id']])
        self.assertEqual(micro,graphs[0]['layers']['micro'][f['id']])

    def test_read_only_legacy_store_and_writer_index_upgrade(self):
        self.add('03_if_else');expected=self.store.find('',limit=2)
        self.store.db.execute('DROP INDEX fn_search_cover');self.store.db.commit()
        reader=DiskGraph(self.path,readonly=True)
        try:self.assertEqual(reader.find('',limit=2),expected)
        finally:reader.close()
        upgrader=DiskGraph(self.path,'project')
        try:
            self.assertTrue(upgrader.has_search_index)
            self.assertEqual(upgrader.find('',limit=2),expected)
        finally:upgrader.close()

    def test_ambiguous_strong_definitions_do_not_bind_arbitrarily(self):
        folder=HERE.parent/'testbench'/'cases'/'10_three_tu_chain'
        for filename,namespace in [('main.cir','caller'),('first.cir','a'),('first.cir','b')]:
            self.store.add_graph(to_json(build_program([str(folder/filename)],'project@'+namespace)))
        self.store.link()
        main=self.store.find('main',exact_method=True)[0][0]
        callees,_=self.store.relations(main['id'])
        self.assertTrue(callees[0]['isDeclaration'])
        self.assertEqual(self.store.meta('summary')['ambiguousNonstaticSymbols'],1)
        self.assertEqual(self.store.find('_Z5firsti',exact_method=True)[1],2)

    def test_static_symbols_stay_local(self):
        self.add('08_static_function')
        matches,total=self.store.find('offset',exact_method=True)
        self.assertGreaterEqual(total,2)
        for f in matches:
            self.assertTrue(f['isStatic'])
            callers,_=self.store.relations(f['id'],'callers')
            self.assertTrue(all(c['tuId']==f['tuId'] for c in callers))

    def test_no_implicit_replace_or_program_change(self):
        graphs=self.add('03_if_else')
        with self.assertRaises(ValueError):self.store.add_graph(graphs[0])
        with self.assertRaises(ValueError):DiskGraph(self.path,'other-program')

    def test_readonly_and_query_bounds(self):
        self.add('03_if_else')
        reader=DiskGraph(self.path,readonly=True)
        try:
            self.assertTrue(reader.meta('linked'))
            matches,total=reader.find('',limit=1)
            self.assertEqual(len(matches),1);self.assertGreaterEqual(total,1)
            with self.assertRaises(ValueError):reader.find('',limit=201)
            with self.assertRaises(ValueError):reader.relations(matches[0]['id'],offset=-1)
            self.assertIn('not propagated between TUs',reader.meta('analysisLimits')['memory'])
        finally:reader.close()

    def test_readonly_lookup_across_tool_handler_threads(self):
        self.add('10_three_tu_chain');reader=DiskGraph(self.path,readonly=True)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                values=list(pool.map(lambda _:reader.find('main',exact_method=True)[1],range(4)))
            self.assertEqual(values,[1,1,1,1])
        finally:reader.close()

if __name__=='__main__':unittest.main()
