import pathlib
import sqlite3
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProductionInvariantTests(unittest.TestCase):
    def test_checkout_has_atomic_reservation_and_idempotency_markers(self):
        text = (ROOT / 'db.py').read_text(encoding='utf-8')
        self.assertIn('BEGIN IMMEDIATE', text)
        self.assertIn('inventory_reservations', text)
        self.assertIn('benefits_applied_at', text)
        self.assertIn('payment_notice_sent_at', text)
        self.assertIn('consume_reservation_and_confirm', text)

    def test_admin_stock_changes_guard_active_reservations(self):
        text = (ROOT / 'db.py').read_text(encoding='utf-8')
        self.assertIn('зарезервировано в активных заказах', text)
        self.assertIn('Нельзя удалить товар', text)

    def test_runtime_limits_polling_concurrency_and_isolates_fsm_events(self):
        text = (ROOT / 'runtime_app.py').read_text(encoding='utf-8')
        self.assertIn('SimpleEventIsolation', text)
        self.assertIn('tasks_concurrency_limit', text)


    def test_expired_checkout_can_only_be_reacquired_after_stock_recheck(self):
        db_text = (ROOT / 'db.py').read_text(encoding='utf-8')
        bot_text = (ROOT / 'bot.py').read_text(encoding='utf-8')
        self.assertIn('async def ensure_order_reservation', db_text)
        self.assertIn('order_id<>?', db_text)
        self.assertIn('await ensure_order_reservation(order_id, hours=settings.reservation_receipt_hours)', db_text)
        self.assertIn('await db.ensure_order_reservation(oid, minutes=settings.reservation_minutes)', bot_text)

    def test_no_silent_exception_passes_in_root_modules(self):
        import re
        pattern = re.compile(r'except\s+(?:Exception|BaseException)(?:\s+as\s+\w+)?:\s*\n\s*pass\b')
        offenders = []
        for path in ROOT.glob('*.py'):
            if pattern.search(path.read_text(encoding='utf-8')):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_sqlite_migration_sql_is_valid_on_existing_shape(self):
        with tempfile.NamedTemporaryFile(suffix='.db') as tmp:
            con = sqlite3.connect(tmp.name)
            con.executescript('''
                CREATE TABLE orders(id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,phone TEXT,status TEXT,created_at TEXT);
                CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER,variant_id INTEGER);
                CREATE TABLE users(user_id INTEGER PRIMARY KEY,username TEXT,last_seen_at TEXT);
                CREATE TABLE cart_items_v5(id INTEGER PRIMARY KEY,user_id INTEGER,variant_id INTEGER,qty INTEGER);
                CREATE TABLE product_variants(id INTEGER PRIMARY KEY,product_id INTEGER,stock INTEGER);
                CREATE TABLE products(id INTEGER PRIMARY KEY,category TEXT);
                CREATE TABLE order_status_history(id INTEGER PRIMARY KEY,order_id INTEGER,created_at TEXT);
                CREATE TABLE inventory_reservations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,variant_id INTEGER NOT NULL,
                    qty INTEGER NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(order_id,variant_id));
                CREATE INDEX idx_inventory_reservations_variant ON inventory_reservations(variant_id,expires_at);
                CREATE INDEX idx_inventory_reservations_order ON inventory_reservations(order_id);
                CREATE INDEX idx_orders_user_created ON orders(user_id,created_at DESC);
                CREATE INDEX idx_orders_status_created ON orders(status,created_at DESC);
                CREATE INDEX idx_orders_username ON orders(username);
                CREATE INDEX idx_orders_phone ON orders(phone);
                CREATE INDEX idx_order_items_order ON order_items(order_id);
                CREATE INDEX idx_order_items_variant ON order_items(variant_id);
                CREATE INDEX idx_users_username ON users(username);
                CREATE INDEX idx_users_last_seen ON users(last_seen_at DESC);
                CREATE INDEX idx_cart_items_user ON cart_items_v5(user_id);
                CREATE INDEX idx_product_variants_product ON product_variants(product_id);
                CREATE INDEX idx_products_category ON products(category);
                CREATE INDEX idx_order_history_order ON order_status_history(order_id,created_at DESC);
            ''')
            self.assertEqual(con.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            con.close()


if __name__ == '__main__':
    unittest.main()
