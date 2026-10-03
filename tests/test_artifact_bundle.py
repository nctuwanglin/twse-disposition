import importlib
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class TransactionTests(unittest.TestCase):
    def test_recovery_rejects_unrelated_in_workspace_file(self):
        a = importlib.import_module('artifact_bundle')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'README.md').write_text('keep')
            (root / '.dashboard-transaction.json').write_text(json.dumps({
                'backup': '.dashboard-backup-test', 'files': {'README.md': False}}))
            with self.assertRaises(ValueError):
                a.recover(root)
            self.assertEqual((root / 'README.md').read_text(), 'keep')

    def test_replace_failure_restores_all_old_files(self):
        a = importlib.import_module('artifact_bundle')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            stage.mkdir()
            for rel in ('index.html', 'dispo.json', 'build-manifest.json'):
                (root / rel).write_text('old ' + rel)
                (stage / rel).write_text('new ' + rel)
            real = a.os.replace
            def replace(src, dst):
                if Path(src) == stage / 'dispo.json':
                    raise OSError('injected replacement failure')
                return real(src, dst)
            with patch.object(a.os, 'replace', side_effect=replace):
                with self.assertRaises(OSError):
                    a.publish(stage, root)
            for rel in ('index.html', 'dispo.json', 'build-manifest.json'):
                self.assertEqual((root / rel).read_text(), 'old ' + rel)
            self.assertFalse((root / '.dashboard-transaction.json').exists())

    def test_recovery_removes_only_newly_created_transaction_files(self):
        a = importlib.import_module('artifact_bundle')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup = root / '.dashboard-backup-test'
            backup.mkdir()
            (backup / 'index.html').write_text('old')
            (root / 'index.html').write_text('partial new')
            (root / 'dispo.json').write_text('new')
            (root / 'unrelated.txt').write_text('keep')
            (root / '.dashboard-transaction.json').write_text(json.dumps({
                'backup': backup.name, 'files': {'index.html': True, 'dispo.json': False}}))
            a.recover(root)
            self.assertEqual((root / 'index.html').read_text(), 'old')
            self.assertFalse((root / 'dispo.json').exists())
            self.assertEqual((root / 'unrelated.txt').read_text(), 'keep')

    def test_recovery_rejects_paths_outside_root(self):
        a = importlib.import_module('artifact_bundle')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.dashboard-transaction.json').write_text(json.dumps({
                'backup': '../elsewhere', 'files': {'../outside': False}}))
            with self.assertRaises(ValueError):
                a.recover(root)
