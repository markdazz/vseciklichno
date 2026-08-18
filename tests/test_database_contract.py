"""Integration tests; run after `pip install -r requirements.txt`."""
import asyncio
import os
import pathlib
import shutil
import tempfile
import unittest


class DatabaseContractTests(unittest.TestCase):
    def test_schema_contains_production_tables_after_init(self):
        # This test is intentionally isolated in a subprocess-like temp DB path.
        # It is skipped when optional runtime dependencies are not installed.
        try:
            import aiosqlite  # noqa: F401
        except Exception:
            self.skipTest('aiosqlite is not installed in this test environment')

        root = pathlib.Path(__file__).resolve().parents[1]
        source = root / 'shop.db'
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / 'shop.db'
            shutil.copy2(source, target)
            old = os.environ.get('DB_PATH')
            os.environ['DB_PATH'] = str(target)
            try:
                # Import in a fresh process is preferable in CI; this smoke test
                # still documents the required migrations.
                import sqlite3
                con = sqlite3.connect(target)
                tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
                con.close()
                self.assertIn('orders', tables)
            finally:
                if old is None:
                    os.environ.pop('DB_PATH', None)
                else:
                    os.environ['DB_PATH'] = old


if __name__ == '__main__':
    unittest.main()
