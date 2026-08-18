import unittest

from emoji_text import canonical_emoji
from premium_emoji import load_rules, apply_to_html, button_text_without_fallback_for_icon
from premium_semantic import semantic_fallbacks


class EmojiRegressionTests(unittest.TestCase):
    def setUp(self):
        load_rules([])

    def test_variation_selector_is_equivalent(self):
        self.assertEqual(canonical_emoji('⭐'), canonical_emoji('⭐️'))

    def test_text_replacement_removes_unicode_fallback(self):
        load_rules([{'fallback_text': '⭐️', 'custom_emoji_id': '123'}])
        rendered = apply_to_html('⭐ Отзывы')
        self.assertIn('emoji-id="123"', rendered)
        # Fallback is inside tg-emoji, not duplicated before the text.
        self.assertFalse(rendered.startswith('⭐ '))

    def test_button_fallback_removed_for_premium_icon(self):
        load_rules([{'fallback_text': '🗑', 'custom_emoji_id': '456'}])
        self.assertEqual(button_text_without_fallback_for_icon('🗑 Удалить', '456'), 'Удалить')
        self.assertEqual(button_text_without_fallback_for_icon('🗑', '456'), '')

    def test_semantic_search_expands_delivery(self):
        values = semantic_fallbacks('доставка')
        self.assertIn(canonical_emoji('🚚'), values)
        self.assertIn(canonical_emoji('📦'), values)


if __name__ == '__main__':
    unittest.main()
