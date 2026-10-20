import pytest

from hoard.models import parse_post_url, post_comment_link


def test_parse_post():
    p = parse_post_url("https://t.me/bonyaviktorya/8561?single")
    assert p.username == "bonyaviktorya"
    assert p.message_id == 8561
    assert post_comment_link(p, 543) == "https://t.me/bonyaviktorya/8561?comment=543"


@pytest.mark.parametrize("value", [
    "", "https://example.com/a/12", "https://t.me/c/111/22",
    "https://t.me/bad/0", "https://t.me/channel/abcd", "javascript:alert(1)",
    "https://t.me/channel/123/789", "https://t.me/channel/123#foo?bar"
])
def test_invalid_post(value):
    with pytest.raises(ValueError):
        parse_post_url(value)
