"""Parse supported MTProxy secrets without importing a Telegram client."""

import ipaddress
import re
from dataclasses import dataclass, field

MAX_DOMAIN_LENGTH = 182
SECRET_FORMAT_ERROR = (
    "MTProxy secret must contain 32 hexadecimal characters, dd and 32 hexadecimal "
    "characters, or ee, 32 hexadecimal characters and a hexadecimal DNS hostname"
)
FAKETLS_DOMAIN_ERROR = (
    "MTProxy FakeTLS secret must contain a valid hexadecimal DNS hostname of up to 182 bytes"
)
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


@dataclass(frozen=True)
class ProxySecret:
    """Keep credential and SNI values out of incidental object representations."""

    mode: str
    key: bytes = field(repr=False)
    domain: str | None = field(repr=False)
    canonical: str = field(repr=False)


def parse_secret(raw):
    """Return a complete plain/dd/FakeTLS secret, without truncating its domain."""
    if not isinstance(raw, str):
        raise ValueError(SECRET_FORMAT_ERROR)
    value = raw.strip()
    if _HEX32.fullmatch(value):
        return ProxySecret("plain", bytes.fromhex(value), None, value.upper())
    if len(value) == 34 and value[:2].lower() == "dd" and _HEX32.fullmatch(value[2:]):
        return ProxySecret("dd", bytes.fromhex(value[2:]), None, value.upper())
    if value[:2].lower() != "ee":
        raise ValueError(SECRET_FORMAT_ERROR)
    if not 36 <= len(value) <= 34 + 2 * MAX_DOMAIN_LENGTH or len(value) % 2:
        raise ValueError(FAKETLS_DOMAIN_ERROR)
    if not _HEX.fullmatch(value):
        raise ValueError(SECRET_FORMAT_ERROR)
    key, encoded_domain = bytes.fromhex(value[2:34]), bytes.fromhex(value[34:])
    try:
        domain = encoded_domain.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(FAKETLS_DOMAIN_ERROR) from None
    if not all(_LABEL.fullmatch(label) for label in domain.split(".")):
        raise ValueError(FAKETLS_DOMAIN_ERROR)
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError(FAKETLS_DOMAIN_ERROR)
    return ProxySecret("faketls", key, domain, value.upper())
