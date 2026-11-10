import gzip
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from cir_graph.external_type_aliases import prepare


class ExternalAliasesTests(unittest.TestCase):
    def test_compressed_input_preserves_original_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);raw=b'!x = !cir.struct<"x" {'+b'data !cir.int<u, 8>, '*100+b'}>\nmodule @x {\n}\n'
            (root/'in.cir.gz').write_bytes(gzip.compress(raw))
            receipt=prepare(root/'in.cir.gz',root/'out',root/'aliases',64)
            self.assertEqual(receipt['original_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(len(receipt['external_aliases']),1)

    def test_exact_alias_roundtrip_and_operations_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            alias = b'!rec_x = !cir.struct<"x" {'+b'data !cir.int<u, 8>, '*100+b'}>\n'
            operation = b'  %x = cir.const #cir.int<2> : !cir.int<u, 8>\n'
            raw = alias+b'module @x {\n'+operation+b'}\n'
            (root/'in').write_bytes(raw)
            receipt = prepare(root/'in', root/'out', root/'aliases', 64)
            self.assertEqual(receipt['original_sha256'], hashlib.sha256(raw).hexdigest())
            record, = receipt['external_aliases']
            self.assertEqual(gzip.decompress(Path(record['gzip_path']).read_bytes()), alias)
            self.assertEqual(record['sha256'], hashlib.sha256(alias).hexdigest())
            self.assertIn(operation, (root/'out').read_bytes())
            self.assertIn(b'!cs5187.external_type_alias', (root/'out').read_bytes())

    def test_large_operations_and_small_aliases_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = b'!small = !cir.int<u, 8>\n'+b'%x = cir.const "'+b'a'*500+b'"\n'
            (root/'in').write_bytes(raw)
            receipt = prepare(root/'in', root/'out', root/'aliases', 64)
            self.assertEqual(receipt['external_aliases'], [])
            self.assertEqual((root/'out').read_bytes(), raw)
