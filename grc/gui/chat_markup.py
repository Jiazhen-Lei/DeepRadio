"""Markdown → Pango markup for GTK chat bubbles.

Gtk.Label 只认 Pango,不认 Markdown。本模块把模型常用的子集转成
``<b>`` / ``<i>`` / ``<tt>`` 与换行;缺 ``markdown`` 包时走内置降级。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def escape_pango(text: str) -> str:
    return html.escape(text or "", quote=False)


def markdown_to_pango(text: str) -> str:
    """把 Markdown 转成 Gtk.Label 可用的 Pango markup。"""
    raw = text or ""
    if not raw.strip():
        return ""
    converted = _html_to_pango(_md_to_html(raw))
    return _collapse_blank_lines(converted).strip()


def _md_to_html(text: str) -> str:
    try:
        import markdown as md
        return md.markdown(
            text,
            extensions=["fenced_code", "nl2br", "sane_lists", "tables"],
        )
    except Exception:
        return _fallback_md_to_html(text)


def _fallback_md_to_html(text: str) -> str:
    fences: List[str] = []

    def _keep_fence(match: re.Match) -> str:
        fences.append(match.group(1).rstrip("\n"))
        return "\x00FENCE{}\x00".format(len(fences) - 1)

    body = _FENCE_RE.sub(_keep_fence, text)
    out: List[str] = []
    in_list = False
    for line in body.split("\n"):
        heading = _HEADING_RE.match(line)
        ul = _UL_RE.match(line)
        ol = _OL_RE.match(line)
        if heading:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<p><b>{}</b></p>".format(
                _inline_fallback(heading.group(2))))
            continue
        if ul or ol:
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = (ul or ol).group(1)
            out.append("<li>{}</li>".format(_inline_fallback(item)))
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line.strip():
            out.append("<br/>")
            continue
        out.append("<p>{}</p>".format(_inline_fallback(line)))
    if in_list:
        out.append("</ul>")
    html_text = "".join(out)
    for idx, block in enumerate(fences):
        html_text = html_text.replace(
            "\x00FENCE{}\x00".format(idx),
            "<pre><code>{}</code></pre>".format(html.escape(block)),
        )
    return html_text


def _inline_fallback(text: str) -> str:
    codes: List[str] = []

    def _keep_code(match: re.Match) -> str:
        codes.append(match.group(1))
        return "\x00CODE{}\x00".format(len(codes) - 1)

    body = _CODE_RE.sub(_keep_code, text)
    body = escape_pango(body)
    body = _BOLD_RE.sub(r"<b>\1</b>", body)
    body = _ITALIC_RE.sub(r"<i>\1</i>", body)
    for idx, code in enumerate(codes):
        body = body.replace(
            "\x00CODE{}\x00".format(idx),
            "<code>{}</code>".format(escape_pango(code)),
        )
    return body


class _PangoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._list_kind: List[str] = []
        self._ol_index: List[int] = []
        self._in_pre = False
        self._skip_data = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = (tag or "").lower()
        if tag in ("script", "style"):
            self._skip_data = True
            return
        if tag in ("strong", "b"):
            self.parts.append("<b>")
        elif tag in ("em", "i"):
            self.parts.append("<i>")
        elif tag == "code" and not self._in_pre:
            self.parts.append("<tt>")
        elif tag == "pre":
            self._in_pre = True
            self._break()
            self.parts.append("<tt>")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("p", "div"):
            self._break()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._break()
            self.parts.append("<b>")
        elif tag == "li":
            self._break()
            if self._list_kind and self._list_kind[-1] == "ol":
                self._ol_index[-1] += 1
                self.parts.append("{}. ".format(self._ol_index[-1]))
            else:
                self.parts.append("• ")
        elif tag == "ul":
            self._list_kind.append("ul")
        elif tag == "ol":
            self._list_kind.append("ol")
            self._ol_index.append(0)
        elif tag == "tr":
            self._break()
        elif tag in ("td", "th"):
            if self.parts and not str(self.parts[-1]).endswith(("\n", " ")):
                self.parts.append("  ")
        elif tag == "table":
            self._break()
        elif tag == "hr":
            self._break()
            self.parts.append("———")
            self._break()

    def handle_endtag(self, tag: str) -> None:
        tag = (tag or "").lower()
        if tag in ("script", "style"):
            self._skip_data = False
            return
        if tag in ("strong", "b", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("</b>")
            if tag.startswith("h"):
                self._break()
        elif tag in ("em", "i"):
            self.parts.append("</i>")
        elif tag == "code" and not self._in_pre:
            self.parts.append("</tt>")
        elif tag == "pre":
            self.parts.append("</tt>")
            self._in_pre = False
            self._break()
        elif tag in ("p", "div", "li", "tr"):
            self._break()
        elif tag in ("td", "th"):
            if self.parts and not str(self.parts[-1]).endswith("\n"):
                self.parts.append("  ")
        elif tag == "table":
            self._break()
        elif tag == "ul":
            if self._list_kind:
                self._list_kind.pop()
            self._break()
        elif tag == "ol":
            if self._list_kind:
                self._list_kind.pop()
            if self._ol_index:
                self._ol_index.pop()
            self._break()

    def handle_data(self, data: str) -> None:
        if self._skip_data or not data:
            return
        self.parts.append(escape_pango(data))

    def _break(self) -> None:
        if self.parts and not str(self.parts[-1]).endswith("\n"):
            self.parts.append("\n")


def _html_to_pango(html_text: str) -> str:
    parser = _PangoHTMLParser()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:
        return escape_pango(re.sub(r"<[^>]+>", "", html_text or ""))
    return "".join(parser.parts)


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text or "")
