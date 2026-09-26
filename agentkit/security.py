"""
agentkit/security.py - helpers for handling untrusted content and secrets.

    wrap_untrusted(text, source)   delimit tool output / fetched pages so the
                                   model can tell data from instructions
    redact(text, secrets)          scrub known secret values from anything
                                   that leaves the VM (events, journal, results)
    redact_value(obj, secrets)     the same for every string inside a JSON-like
                                   value; used before serialising, so a secret
                                   containing quotes or backslashes (which JSON
                                   escapes) is still found
    host_allowed(host, rules)      egress allowlist matching
    is_private_host(host)          loopback / private / link-local / metadata
                                   names and literals, plus non-canonical
                                   numeric hosts (2130706433, 0x7f.1, 127.1)
    is_public_ip(address)          the address-level test the fetch transport
                                   applies to every DNS answer before it
                                   connects (names are checked by address,
                                   not by spelling)

Wrapping reduces prompt-injection risk; it does not remove it. The real
controls are capability limits in the PolicyGate (docs/decisions/0002).
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Iterable, Mapping

REDACTED = "[REDACTED]"
_CLOSE_TAG = re.compile(r"</\s*untrusted", re.IGNORECASE)
_OPEN_TAG = re.compile(r"<\s*untrusted", re.IGNORECASE)
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE|MNEMONIC|PRIVATE)", re.IGNORECASE)
_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}
# A label that a URL parser or getaddrinfo may read as part of an IPv4
# number: decimal, octal (leading 0) or hex (0x...).
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
# NAT64 prefixes embed an IPv4 address in the low 32 bits.
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def wrap_untrusted(text: str, source: str) -> str:
    """Put untrusted text inside <untrusted source="..."> ... </untrusted>.

    Any tag in the text that could close (or reopen) the block is escaped, so
    content cannot break out of its delimiters.
    """
    body = _CLOSE_TAG.sub(lambda m: "&lt;" + m.group(0)[1:], text or "")
    body = _OPEN_TAG.sub(lambda m: "&lt;" + m.group(0)[1:], body)
    src = (source or "unknown").replace('"', "'").replace("<", "&lt;").replace(">", "&gt;")
    return f'<untrusted source="{src}">\n{body}\n</untrusted>'


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of each secret (4+ chars) with [REDACTED].

    Longer secrets go first so one secret containing another is fully hidden.
    """
    if not text:
        return text
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def redact_value(value: Any, secrets: Iterable[str]) -> Any:
    """A copy of a JSON-like value (dicts, lists, strings) with every secret
    replaced in every string, keys included."""
    secrets = [s for s in secrets if s and len(s) >= 4]
    if not secrets:
        return value

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return redact(v, secrets)
        if isinstance(v, dict):
            return {walk(k): walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v

    return walk(value)


def secrets_from_env(env: Mapping[str, str]) -> list[str]:
    """Values of env vars whose names look secret (API keys, tokens, ...)."""
    return [v for k, v in env.items() if _SECRET_NAME.search(k) and v and len(v) >= 8]


def normalize_host(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries (mapped, NAT64, 6to4, Teredo,
    IPv4-compatible), if any."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if any(ip in net for net in _NAT64) or int(ip) >> 32 == 0:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    return None


def is_public_ip(address: "str | ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True only for a globally routable unicast address.

    Loopback, private, link-local (169.254/16, cloud metadata), shared
    (100.64/10), "this network" (0/8), multicast, reserved, unspecified and
    IPv6 site-local addresses are not public, and neither is an IPv6 address
    that embeds a non-public IPv4 address. A well-known-prefix NAT64 address
    (64:ff9b::/96) of a public IPv4 address is public: that is how DNS64
    answers for every IPv4 host on an IPv6-only network.
    """
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        v4 = _embedded_ipv4(ip)  # type: ignore[arg-type]
        if v4 is not None and not is_public_ip(v4):
            return False
        if v4 is not None and ip in _NAT64[0]:
            return True
        if ip.is_site_local:  # type: ignore[union-attr]
            return False
    if (ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback
            or ip.is_link_local or ip.is_private):
        return False
    return ip.is_global


def is_numeric_host(host: str) -> bool:
    """True when the last label is numeric, i.e. parsers treat the host as an
    IPv4 number (possibly in decimal, octal, hex or shortened form)."""
    host = normalize_host(host)
    return bool(host) and ":" not in host and bool(_NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]))


def is_private_host(host: str) -> bool:
    """True for localhost, cloud metadata names, non-public IP literals and
    numeric hosts that are not a canonical dotted quad (2130706433, 127.1,
    0177.0.0.1, 0x7f000001 - getaddrinfo on Linux reads these as IPv4).

    A hostname can still resolve to a private address; the fetch transport
    checks every resolved address with is_public_ip before connecting.
    """
    host = normalize_host(host)
    if not host or host == "localhost" or host.endswith(".localhost") or host in _METADATA_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return is_numeric_host(host)  # a non-canonical IPv4 spelling
    return not is_public_ip(ip)


def host_allowed(host: str, rules: Iterable[str]) -> bool:
    """Match a hostname against allowlist rules.

    "example.com"     exactly that host
    "*.example.com"   any subdomain of example.com (not the apex itself)
    ".example.com"    example.com and any subdomain
    "*"               any public host

    Private and metadata hosts never match, whatever the rules say.
    """
    host = normalize_host(host)
    if not host or is_private_host(host) or not re.fullmatch(r"[a-z0-9.\-:]+", host):
        return False
    for rule in rules:
        rule = normalize_host(rule)
        if not rule:
            continue
        if rule == "*":
            return True
        if rule.startswith("*."):
            if host.endswith(rule[1:]) and host != rule[2:]:
                return True
        elif rule.startswith("."):
            if host == rule[1:] or host.endswith(rule):
                return True
        elif host == rule:
            return True
    return False


__all__ = ["REDACTED", "host_allowed", "is_numeric_host", "is_private_host", "is_public_ip",
           "normalize_host", "redact", "redact_value", "secrets_from_env", "wrap_untrusted"]
