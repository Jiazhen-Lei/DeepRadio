import unittest

from grc.gui.chat_markup import (
    _fallback_md_to_html,
    _html_to_pango,
    escape_pango,
    markdown_to_pango,
)


class ChatMarkupTest(unittest.TestCase):
    def test_escape_pango_ampersand(self):
        self.assertEqual(escape_pango("A & B <C>"), "A &amp; B &lt;C&gt;")

    def test_markdown_bold_and_list(self):
        markup = markdown_to_pango("先看 **EVM**\n\n- 噪声\n- 频偏")
        self.assertIn("<b>EVM</b>", markup)
        self.assertIn("• 噪声", markup)
        self.assertIn("• 频偏", markup)
        self.assertNotIn("**", markup)

    def test_markdown_code_fence_is_monospace(self):
        markup = markdown_to_pango("结果:\n```\nnoise=0.02\n```")
        self.assertIn("<tt>", markup)
        self.assertIn("noise=0.02", markup)
        self.assertNotIn("```", markup)

    def test_markdown_table_becomes_wrapped_rows(self):
        markup = markdown_to_pango(
            "| 参数 | 方向 |\n|---|---|\n| chan.noise_voltage | 减小 |"
        )
        self.assertIn("chan.noise_voltage", markup)
        self.assertIn("减小", markup)
        self.assertGreaterEqual(markup.count("\n"), 1)

    def test_html_to_pango_strips_unsafe_tags(self):
        markup = _html_to_pango("<p>安全</p><script>alert(1)</script>")
        self.assertIn("安全", markup)
        self.assertNotIn("alert", markup)
        self.assertNotIn("<script>", markup)

    def test_fallback_path_without_markdown_library(self):
        import grc.gui.chat_markup as chat_markup

        original = chat_markup._md_to_html
        chat_markup._md_to_html = _fallback_md_to_html
        try:
            markup = markdown_to_pango("标题\n\n**确认** 后改成 `qpsk_awgn`")
            self.assertIn("<b>确认</b>", markup)
            self.assertIn("<tt>qpsk_awgn</tt>", markup)
        finally:
            chat_markup._md_to_html = original
