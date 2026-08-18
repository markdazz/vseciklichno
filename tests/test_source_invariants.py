import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SourceInvariantTests(unittest.TestCase):
    def test_no_bundled_env_in_production_zip_source(self):
        self.assertFalse((ROOT / '.env').exists())
        self.assertTrue((ROOT / '.env.example').exists())

    def test_runtime_uses_persistent_fsm(self):
        text = (ROOT / 'runtime_app.py').read_text(encoding='utf-8')
        self.assertIn('SQLiteStorage', text)
        self.assertIn('Dispatcher(storage=storage', text)

    def test_network_startup_is_retried(self):
        text = (ROOT / 'runtime_app.py').read_text(encoding='utf-8')
        self.assertIn('wait_for_telegram', text)
        self.assertIn('telegram_retry', text)
        self.assertIn('AiohttpSession(timeout=float(settings.telegram_request_timeout))', text)

    def test_premium_admin_changes_do_not_mass_push_customers(self):
        text = (ROOT / 'bot.py').read_text(encoding='utf-8')
        self.assertNotIn('refresh_all_customer_menus', text)
        self.assertNotIn('force_refresh_all_customer', text)

    def test_python_sources_parse(self):
        for path in ROOT.glob('*.py'):
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


if __name__ == '__main__':
    unittest.main()
