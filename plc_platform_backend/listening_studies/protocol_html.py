"""Allowlisted, attribute-free formatting for participant-facing protocol text."""
from html import escape
from html.parser import HTMLParser

FORMATTING_TAGS = frozenset({
    "p", "div", "br", "strong", "b", "em", "i", "u", "s", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "blockquote", "pre", "code", "hr", "span",
})
BLOCKED_TAGS = frozenset({"script", "style", "iframe", "object", "template", "svg", "math"})


class _ProtocolParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.text = []
        self.blocked = []

    def handle_starttag(self, tag, attrs):
        if tag in BLOCKED_TAGS or self.blocked:
            self.blocked.append(tag)
        elif tag in FORMATTING_TAGS:
            self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        if not self.blocked and tag in FORMATTING_TAGS:
            self.parts.append(f"<{tag}>")
            if tag not in {"br", "hr"}:
                self.parts.append(f"</{tag}>")

    def handle_endtag(self, tag):
        if self.blocked:
            if tag in self.blocked:
                self.blocked = self.blocked[:self.blocked.index(tag)]
        elif tag in FORMATTING_TAGS and tag not in {"br", "hr"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(escape(data))
            self.text.append(data)


def sanitize_protocol_html(value: str) -> str:
    """Retain text/basic formatting, never scripts, attributes or remote content.

    Require visible text so markup-only consent cannot be published. Plain text
    is escaped and remains supported; no owner input is trusted as executable HTML.
    """
    parser = _ProtocolParser()
    parser.feed(value)
    parser.close()
    if not "".join(parser.text).strip():
        raise ValueError("Instructions and consent must contain visible text")
    result = "".join(parser.parts)
    if len(result) > 10000:
        raise ValueError("Formatted instructions and consent must not exceed 10000 characters")
    return result
