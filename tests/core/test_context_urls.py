"""Local farm context is carried without replacing unrelated filters or granting access."""

from urllib.parse import parse_qsl, urlsplit

import pytest

from farmcore.core.urls import with_farm


@pytest.mark.parametrize(
    "path",
    [
        "https://example.com/x",
        "//example.com/x",
        "/\\example.com",
        "#eu",
        "mailto:qa@example.invalid",
    ],
)
def test_nonlocal_targets_are_unchanged(path):
    assert with_farm(path, 2) == path


def test_local_context_preserves_repeated_filters_blanks_and_fragment():
    target = urlsplit(
        with_farm("/operations/register?farm=1&year=2024&tag=a&tag=b&empty=&farm=3#eu", 2)
    )
    assert target.path == "/operations/register" and target.fragment == "eu"
    assert parse_qsl(target.query, keep_blank_values=True) == [
        ("year", "2024"),
        ("tag", "a"),
        ("tag", "b"),
        ("empty", ""),
        ("farm", "2"),
    ]


def test_generated_queries_preserve_filters_and_only_replace_farm():
    import random
    import string
    from urllib.parse import urlencode

    rng = random.Random(20261017)
    alphabet = string.ascii_letters + " &?=#/é"
    for case in range(200):
        filters = [("tag", "".join(rng.choices(alphabet, k=rng.randrange(12)))) for _ in range(3)]
        pairs = [*filters, ("farm", "old"), ("farm", "duplicate"), ("blank", "")]
        farm_id = case + 1
        target = urlsplit(with_farm("/operations/register?" + urlencode(pairs) + "#eu", farm_id))
        assert (target.scheme, target.netloc, target.path, target.fragment) == (
            "",
            "",
            "/operations/register",
            "eu",
        )
        assert parse_qsl(target.query, keep_blank_values=True) == [
            *filters,
            ("blank", ""),
            ("farm", str(farm_id)),
        ]


def test_control_character_host_target_is_not_qualified():
    assert with_farm("/\t/evil.example/x", 2) == "/\t/evil.example/x"


def test_no_request_context_leaves_target_unchanged():
    from farmcore.web import context_url

    assert context_url(None, "/operations/register") == "/operations/register"
