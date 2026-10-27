"""Protocol HTML remains formatting, never executable or externally loaded content."""
import pytest
from plc_platform_backend.listening_studies.protocol_html import sanitize_protocol_html


def test_preserves_basic_formatting_and_plain_text():
    assert sanitize_protocol_html('<h2>Listen</h2><ul><li><strong>Quiet room</strong></li></ul>') == '<h2>Listen</h2><ul><li><strong>Quiet room</strong></li></ul>'
    assert sanitize_protocol_html('Headphones & quiet\nTake breaks') == 'Headphones &amp; quiet\nTake breaks'


def test_removes_attributes_active_content_and_remote_resources():
    source = '<p onclick="alert(1)" style="color:red">Safe <b>text</b></p><script>alert(1)</script><img src="https://example.com/x"><a href="javascript:alert(1)">Contact</a><svg><text>Hidden</text></svg>'
    assert sanitize_protocol_html(source) == '<p>Safe <b>text</b></p>Contact'


@pytest.mark.parametrize('value', ['', '  ', '<p><br></p>', '<script>Consent</script>', '<style>body{}</style>'])
def test_requires_visible_consent(value):
    with pytest.raises(ValueError, match='visible text'):
        sanitize_protocol_html(value)


def test_sanitizing_twice_is_stable():
    source = '<p>Hello &amp; <em>thanks</em></p>'
    assert sanitize_protocol_html(sanitize_protocol_html(source)) == source
