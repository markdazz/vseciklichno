from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOT = (ROOT / 'bot.py').read_text(encoding='utf-8')
DB = (ROOT / 'db.py').read_text(encoding='utf-8')
SECTIONS = (ROOT / 'admin_sections.py').read_text(encoding='utf-8')
SEARCH = (ROOT / 'admin_search.py').read_text(encoding='utf-8')


class AdminOrderUX23Tests(unittest.TestCase):
    def test_build_marker(self):
        self.assertIn('ADMIN-ORDER-UX-23', BOT)

    def test_public_order_code_schema_and_generator(self):
        self.assertIn("'public_code':'TEXT'", DB)
        self.assertIn('idx_orders_public_code', DB)
        self.assertIn('secrets.choice(ORDER_CODE_ALPHABET)', DB)
        self.assertRegex(DB, r'range\(6\)')

    def test_admin_search_accepts_public_code(self):
        self.assertIn("public_code = q.lstrip('#')", SEARCH)
        self.assertIn("UPPER(COALESCE(public_code,''))=?", SEARCH)

    def test_payment_confirmation_does_not_open_tracking_fsm(self):
        block = BOT.split('@router.callback_query(F.data.startswith("payok:"))', 1)[1]
        block = block.split('@router.callback_query(F.data.startswith("payno:"))', 1)[0]
        self.assertNotIn('AdminTracking.waiting_track', block)
        self.assertIn('adm:shipping', block)

    def test_no_manual_assembly_queue_in_new_admin_ui(self):
        self.assertNotIn('📦 Сборка', SECTIONS)
        # legacy callback can remain, but user-facing new orders dashboard must use shipping
        orders_block = BOT.split('@router.callback_query(F.data=="adm:orders")', 1)[1]
        orders_block = orders_block.split('@router.callback_query(F.data=="adm:active")', 1)[0]
        self.assertNotIn('📦 Сборка', orders_block)
        self.assertIn('🚚 К отправке', orders_block)

    def test_shipping_queue_contains_all_pre_shipping_legacy_statuses(self):
        self.assertIn("'Подтверждён','Собирается','Собран','Передан в доставку'", BOT)


if __name__ == '__main__':
    unittest.main()
