"""Secret parsing preserves FakeTLS credentials and rejects malformed SNI."""

import pytest

from telegram_proxy_secret import MAX_DOMAIN_LENGTH, parse_secret

KEY = "0123456789abcdef0123456789abcdef"


def faketls(domain):
    return "ee" + KEY + domain.encode().hex()


@pytest.mark.parametrize(
    ("raw", "mode", "key"),
    [
        (KEY, "plain", KEY),
        ("dd" + "ab" * 15, "plain", "dd" + "ab" * 15),
        ("ee" + "ab" * 15, "plain", "ee" + "ab" * 15),
        ("dd" + KEY, "dd", KEY),
        ("DD" + KEY.upper(), "dd", KEY),
    ],
)
def test_plain_and_dd_modes_do_not_misinterpret_key_prefixes(raw, mode, key):
    parsed = parse_secret(" " + raw + " ")
    assert parsed.mode == mode
    assert parsed.key == bytes.fromhex(key)
    assert parsed.domain is None
    assert parsed.canonical == raw.upper()


@pytest.mark.parametrize("domain", ["a", "Example.COM", "xn--bcher-kva.example"])
def test_faketls_preserves_domain_bytes_and_complete_secret(domain):
    secret = faketls(domain)
    parsed = parse_secret(secret.upper())
    assert parsed.mode == "faketls"
    assert parsed.key == bytes.fromhex(KEY)
    assert parsed.domain == domain
    assert parsed.canonical == secret.upper()
    assert bytes.fromhex(parsed.canonical) == b"\xee" + parsed.key + domain.encode("ascii")
    assert KEY not in repr(parsed).lower()
    assert repr(parsed) == "ProxySecret(mode='faketls')"


def test_official_domain_length_limit_is_checked_without_truncation():
    domain = ".".join(("a" * 63, "b" * 63, "c" * 54))
    assert len(domain) == MAX_DOMAIN_LENGTH == 182
    assert parse_secret(faketls(domain)).domain == domain
    with pytest.raises(ValueError, match="182 bytes"):
        parse_secret(faketls(domain + "c"))


@pytest.mark.parametrize(
    "domain",
    [
        "",
        ".example.com",
        "example.com.",
        "example..com",
        "-example.com",
        "example-.com",
        "under_score.example",
        "a" * 64 + ".example",
        "https://example.com",
        "example.com/path",
        "user@example.com",
        "example.com:443",
        "example.com\x00",
        "example\n.com",
        "example\t.com",
        "example com",
        "bücher.example",
        "127.0.0.1",
        "::1",
    ],
)
def test_invalid_faketls_domains_do_not_echo_credential_values(domain):
    secret = faketls(domain)
    with pytest.raises(ValueError) as error:
        parse_secret(secret)
    assert "DNS hostname" in str(error.value)
    assert secret not in str(error.value)
    assert KEY not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        KEY[:-1],
        "dd" + KEY[:-1],
        "ee" + KEY,
        "ee" + KEY + "6",
        "ee" + KEY + "zz",
        "ee" + "g" * 32 + "61",
        "ee" + KEY + "ff",
        "ee" + KEY + "61" * 20000,
        "tg://proxy?server=example.com&secret=" + KEY,
        "7gARIjNEVWZ3iJmqu8zd7v9leGFtcGxlLmNvbQ",
    ],
    ids=lambda value: f"{type(value).__name__}-{len(value) if isinstance(value, str) else value}",
)
def test_malformed_or_unsupported_secrets_fail_safely(value):
    with pytest.raises(ValueError) as error:
        parse_secret(value)
    assert str(error.value).startswith("MTProxy")
    assert KEY not in str(error.value)
