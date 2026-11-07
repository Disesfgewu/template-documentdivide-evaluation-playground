"""Outbound endpoint URL and resolved-IP policy validation (SPEC §12, R19, T72).

This module validates candidate HTTP(S) endpoint URLs and resolved IP addresses
against an explicit operator policy before registration or outbound connection
planning. It enforces exact (hostname, port) allowlists, rejects dangerous URL
schemes, credentials/userinfo, fragments, and nonpublic resolved IP addresses
(including IPv4-mapped, 6to4, Teredo, NAT64, carrier-grade NAT, benchmark,
documentation, multicast, link-local, loopback, private, and unspecified ranges)
unless an explicit operator network CIDR allowlist permits them.

Scope and caller obligations:
- This module is a pure policy and address-validation primitive. It does NOT
  perform HTTP requests, pin DNS results to sockets, follow or restrict HTTP
  redirects, manage TLS handshakes, or provide complete SSRF protection on its
  own.
- Callers that resolve hostnames MUST pass the validated resolved IP address(es)
   directly to the socket/connection layer (or verify the peer address before
  sending data) to prevent DNS-rebinding / time-of-check-to-time-of-use (TOCTOU)
  gaps, MUST disable automatic HTTP redirects (or re-validate every target under
  the same policy), MUST ignore ambient proxy environment variables unless
  explicitly governed, and MUST enforce request timeouts and response bounds.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048
MAX_HOSTNAME_LENGTH = 253
MAX_LABEL_LENGTH = 63
MAX_ALLOWED_HOSTS = 256
MAX_ALLOWED_CIDRS = 256
MAX_RESOLVED_ADDRESSES = 64

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Non-global / special-purpose IPv4 and IPv6 networks that Python's stdlib
# ipaddress.is_global may not mark non-global across all Python versions or that
# require explicit defense-in-depth blocking unless operator CIDR-allowlisted.
_BLOCKED_NETS_V4 = (
    ipaddress.IPv4Network("0.0.0.0/8"),          # "This" network (RFC 1122)
    ipaddress.IPv4Network("10.0.0.0/8"),         # Private-Use (RFC 1918)
    ipaddress.IPv4Network("100.64.0.0/10"),      # Shared CGNAT (RFC 6598)
    ipaddress.IPv4Network("127.0.0.0/8"),        # Loopback (RFC 1122)
    ipaddress.IPv4Network("169.254.0.0/16"),     # Link-Local / cloud metadata (RFC 3927)
    ipaddress.IPv4Network("172.16.0.0/12"),      # Private-Use (RFC 1918)
    ipaddress.IPv4Network("192.0.0.0/24"),       # IETF Protocol Assignments (RFC 6890)
    ipaddress.IPv4Network("192.0.2.0/24"),       # TEST-NET-1 (RFC 5737)
    ipaddress.IPv4Network("192.88.99.0/24"),     # 6to4 Relay Anycast (RFC 7526)
    ipaddress.IPv4Network("192.168.0.0/16"),     # Private-Use (RFC 1918)
    ipaddress.IPv4Network("198.18.0.0/15"),      # Benchmarking (RFC 2544)
    ipaddress.IPv4Network("198.51.100.0/24"),    # TEST-NET-2 (RFC 5737)
    ipaddress.IPv4Network("203.0.113.0/24"),     # TEST-NET-3 (RFC 5737)
    ipaddress.IPv4Network("224.0.0.0/4"),        # Multicast (RFC 5771)
    ipaddress.IPv4Network("240.0.0.0/4"),        # Reserved / Limited Broadcast (RFC 1112)
)

_BLOCKED_NETS_V6 = (
    ipaddress.IPv6Network("::/128"),             # Unspecified
    ipaddress.IPv6Network("::1/128"),            # Loopback
    ipaddress.IPv6Network("::ffff:0:0/96"),      # IPv4-mapped (checked via embedded v4)
    ipaddress.IPv6Network("::ffff:0:0:0/96"),    # IPv4-translated (RFC 2765)
    ipaddress.IPv6Network("64:ff9b::/96"),       # NAT64 well-known prefix (RFC 6052)
    ipaddress.IPv6Network("64:ff9b:1::/48"),     # NAT64 local-use prefix (RFC 8215)
    ipaddress.IPv6Network("100::/64"),           # Discard-Only (RFC 6666)
    ipaddress.IPv6Network("2001::/32"),          # Teredo (RFC 4380)
    ipaddress.IPv6Network("2001:2::/48"),        # Benchmarking (RFC 5180)
    ipaddress.IPv6Network("2001:db8::/32"),      # Documentation (RFC 3849)
    ipaddress.IPv6Network("2002::/16"),          # 6to4 (RFC 3056)
    ipaddress.IPv6Network("fc00::/7"),           # Unique-Local (RFC 4193)
    ipaddress.IPv6Network("fe80::/10"),          # Link-Local (RFC 4291)
    ipaddress.IPv6Network("fec0::/10"),          # Site-Local deprecated (RFC 3879)
    ipaddress.IPv6Network("ff00::/8"),           # Multicast
)

_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_TRANSLATED = ipaddress.IPv6Network("::ffff:0:0:0/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Iterable[str | IPAddress]]


class EndpointPolicyError(ValueError):
    """Fixed error code; never echoes raw candidate URLs, credentials, or hosts."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _validate_port(port: object, *, error_code: str) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise EndpointPolicyError(error_code)
    return port


def _validate_path(raw_path: str) -> str:
    path = raw_path or "/"
    if not path.startswith("/") or "//" in path:
        raise EndpointPolicyError("invalid_path")
    segments = path.split("/")
    if any(seg in {".", ".."} for seg in segments):
        raise EndpointPolicyError("invalid_path")
    lower_path = path.lower()
    if any(tok in lower_path for tok in ("%2e", "%2f", "%5c", "%00")):
        raise EndpointPolicyError("invalid_path")
    return path


def _parse_ip_literal(value: str) -> IPAddress | None:
    if "%" in value:
        raise EndpointPolicyError("invalid_host")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    # Reject non-canonical IPv4 forms or leading zeros if any slipped through,
    # and ensure compressed canonical representation for IPv6.
    if isinstance(ip, ipaddress.IPv4Address) and str(ip) != value:
        raise EndpointPolicyError("invalid_host")
    return ip


def _normalize_host(raw_host: object, *, error_code: str) -> str:
    if not isinstance(raw_host, str) or not raw_host:
        raise EndpointPolicyError(error_code)
    if any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in raw_host):
        raise EndpointPolicyError(error_code)
    if "%" in raw_host:
        raise EndpointPolicyError(error_code)

    host = raw_host.lower()
    if len(host) > MAX_HOSTNAME_LENGTH:
        raise EndpointPolicyError(error_code)

    # Check if host is a valid IPv6 literal (urlsplit strips brackets).
    if ":" in host:
        try:
            ip = ipaddress.IPv6Address(host)
        except ValueError:
            raise EndpointPolicyError(error_code) from None
        # Reject IPv4-mapped IPv6 literals as URL/policy hostnames so targets
        # cannot use [::ffff:a.b.c.d] to alias IPv4 hosts or bypass allowlists.
        if ip.ipv4_mapped is not None:
            raise EndpointPolicyError(error_code)
        return str(ip)

    # Check if host might be an IPv4 literal or malformed numeric/dotted-numeric host.
    labels = host.split(".")
    if any(not label for label in labels):
        raise EndpointPolicyError(error_code)

    if labels[-1].isdigit():
        try:
            ip4 = ipaddress.IPv4Address(host)
        except ValueError:
            raise EndpointPolicyError(error_code) from None
        if str(ip4) != host:
            raise EndpointPolicyError(error_code)
        return str(ip4)

    for label in labels:
        if len(label) > MAX_LABEL_LENGTH:
            raise EndpointPolicyError(error_code)
        if label.startswith("-") or label.endswith("-"):
            raise EndpointPolicyError(error_code)
        if not all(c.isalnum() or c == "-" for c in label):
            raise EndpointPolicyError(error_code)
    # Single-label or multi-label hostnames whose final label is a hex literal
    # (e.g. 0x7f000001 or 127.0.0.0x1) are rejected as ambiguous IPv4 forms.
    if labels[-1].startswith("0x"):
        raise EndpointPolicyError(error_code)
    return host


def _embedded_ipv4(ip: IPAddress) -> tuple[ipaddress.IPv4Address | None, bool]:
    """Return (embedded_ipv4, is_transition_tunnel) for an IP address.

    Transition mechanisms like 6to4, Teredo, NAT64, and IPv4-translated addresses
    cannot be validated as public merely because their outer IPv6 prefix or embedded
    IPv4 looks global; only standard IPv4-mapped (::ffff:0:0/96) can unwrap to an
    embedded IPv4 for policy evaluation.
    """
    if isinstance(ip, ipaddress.IPv4Address):
        return ip, False
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped, False
    if ip.sixtofour is not None:
        return ip.sixtofour, True
    if ip.teredo is not None:
        # teredo is (server, client); both are IPv4 addresses, and the tunnel itself
        # is non-public for direct endpoint policy.
        return ip.teredo[1], True
    if ip in _NAT64_WELL_KNOWN or ip in _NAT64_LOCAL_USE or ip in _IPV4_TRANSLATED:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF), True
    # Check deprecated IPv4-compatible IPv6 ::a.b.c.d/96 (excluding :: and ::1)
    if int(ip) >> 32 == 0 and int(ip) > 1:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF), True
    return None, False


def _is_public_unicast_v4(ip: ipaddress.IPv4Address) -> bool:
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return not any(ip in net for net in _BLOCKED_NETS_V4)


def _is_public_unicast_v6(ip: ipaddress.IPv6Address) -> bool:
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_site_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return not any(ip in net for net in _BLOCKED_NETS_V6)


def _check_resolved_ip(
    raw_ip: str | IPAddress,
    allowed_cidrs: tuple[IPNetwork, ...],
) -> IPAddress:
    ip: IPAddress
    if isinstance(raw_ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        ip = raw_ip
    elif isinstance(raw_ip, str):
        if "%" in raw_ip or any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in raw_ip):
            raise EndpointPolicyError("invalid_resolved_ip")
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise EndpointPolicyError("invalid_resolved_ip") from None
    else:
        raise EndpointPolicyError("invalid_resolved_ip")

    v4, is_tunnel = _embedded_ipv4(ip)
    if is_tunnel:
        # Transition / tunnel addresses (6to4, Teredo, NAT64, v4-compatible, v4-translated)
        # are always rejected unless the exact outer IPv6 CIDR is explicitly allowlisted,
        # and even then only if the embedded IPv4 is also allowlisted or public.
        if v4 is not None and not _is_public_unicast_v4(v4):
            if not any(v4 in net for net in allowed_cidrs if isinstance(net, ipaddress.IPv4Network)):
                raise EndpointPolicyError("nonpublic_ip")
        if not any(ip in net for net in allowed_cidrs if isinstance(net, ipaddress.IPv6Network)):
            raise EndpointPolicyError("nonpublic_ip")
        return ip

    if isinstance(ip, ipaddress.IPv6Address) and v4 is not None:
        # IPv4-mapped IPv6 address (::ffff:a.b.c.d): canonicalize evaluation to the
        # underlying IPv4 address so IPv4-mapped forms cannot bypass IPv4 rules.
        if _is_public_unicast_v4(v4):
            return v4
        if any(
            (v4 in net if isinstance(net, ipaddress.IPv4Network) else ip in net)
            for net in allowed_cidrs
        ):
            return v4
        raise EndpointPolicyError("nonpublic_ip")

    if isinstance(ip, ipaddress.IPv4Address):
        if _is_public_unicast_v4(ip):
            return ip
        if any(ip in net for net in allowed_cidrs if isinstance(net, ipaddress.IPv4Network)):
            return ip
        raise EndpointPolicyError("nonpublic_ip")

    if _is_public_unicast_v6(ip):
        return ip
    if any(ip in net for net in allowed_cidrs if isinstance(net, ipaddress.IPv6Network)):
        return ip
    raise EndpointPolicyError("nonpublic_ip")


@dataclass(frozen=True)
class EndpointPolicy:
    """Immutable operator policy for outbound HTTP(S) endpoints.

    - ``allowed_hosts``: exact ``(hostname, port)`` pairs permitted as targets.
      Empty allowlist fails closed (rejects all endpoints).
    - ``allow_http``: whether ``http://`` is permitted in addition to ``https://``.
    - ``allowed_nonpublic_cidrs``: explicit IPv4/IPv6 CIDRs permitted when the
      operator intentionally allows specific private or loopback ranges (SPEC §12).
    """

    allowed_hosts: frozenset[tuple[str, int]]
    allow_http: bool = False
    allowed_nonpublic_cidrs: tuple[IPNetwork, ...] = ()

    def __init__(
        self,
        allowed_hosts: Iterable[tuple[str, int]],
        *,
        allow_http: bool = False,
        allowed_nonpublic_cidrs: Iterable[str | IPNetwork] = (),
    ):
        if type(allow_http) is not bool:
            raise EndpointPolicyError("invalid_policy")
        if isinstance(allowed_hosts, (str, bytes)) or not isinstance(allowed_hosts, Iterable):
            raise EndpointPolicyError("invalid_policy")
        normalized_hosts: set[tuple[str, int]] = set()
        for item in allowed_hosts:
            if not isinstance(item, tuple) or len(item) != 2:
                raise EndpointPolicyError("invalid_policy")
            raw_host, raw_port = item
            host = _normalize_host(raw_host, error_code="invalid_policy")
            port = _validate_port(raw_port, error_code="invalid_policy")
            normalized_hosts.add((host, port))
            if len(normalized_hosts) > MAX_ALLOWED_HOSTS:
                raise EndpointPolicyError("invalid_policy")

        if isinstance(allowed_nonpublic_cidrs, (str, bytes)) or not isinstance(
            allowed_nonpublic_cidrs, Iterable
        ):
            raise EndpointPolicyError("invalid_policy")
        cidrs: list[IPNetwork] = []
        seen_cidrs: set[IPNetwork] = set()
        for raw_cidr in allowed_nonpublic_cidrs:
            if isinstance(raw_cidr, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                net = raw_cidr
            elif isinstance(raw_cidr, str):
                if any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in raw_cidr):
                    raise EndpointPolicyError("invalid_policy")
                try:
                    net = ipaddress.ip_network(raw_cidr, strict=True)
                except ValueError:
                    raise EndpointPolicyError("invalid_policy") from None
            else:
                raise EndpointPolicyError("invalid_policy")
            # Forbid full-space wildcard nonpublic overrides (/0) that would disable IP checks.
            if net.prefixlen == 0:
                raise EndpointPolicyError("invalid_policy")
            if net not in seen_cidrs:
                seen_cidrs.add(net)
                cidrs.append(net)
            if len(cidrs) > MAX_ALLOWED_CIDRS:
                raise EndpointPolicyError("invalid_policy")

        object.__setattr__(self, "allowed_hosts", frozenset(normalized_hosts))
        object.__setattr__(self, "allow_http", allow_http)
        object.__setattr__(self, "allowed_nonpublic_cidrs", tuple(cidrs))


@dataclass(frozen=True)
class ValidatedEndpoint:
    scheme: str
    host: str
    port: int
    path: str
    resolved_ips: tuple[str, ...]

    @property
    def normalized_url(self) -> str:
        host_repr = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host_repr}:{self.port}{self.path}"


def default_socket_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError:
        raise EndpointPolicyError("dns_resolution_failed") from None
    out: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        if not sockaddr or not isinstance(sockaddr[0], str):
            raise EndpointPolicyError("dns_resolution_failed")
        out.append(sockaddr[0])
    return out


def validate_endpoint_url(
    url: object,
    policy: EndpointPolicy,
    *,
    resolved_ips: Iterable[str | IPAddress] | None = None,
    resolver: Resolver | None = None,
) -> ValidatedEndpoint:
    """Validate a candidate outbound URL and its resolved IP addresses against ``policy``.

    Either ``resolved_ips`` (already resolved by the caller) or ``resolver`` may be
    supplied for hostname targets; if neither is passed, ``default_socket_resolver``
    is used. If ``url`` uses an IP literal host, its IP is validated directly without
    calling ``resolver``.
    """
    if not isinstance(policy, EndpointPolicy):
        raise EndpointPolicyError("invalid_policy")
    if resolved_ips is not None and resolver is not None:
        raise EndpointPolicyError("invalid_resolution_args")
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise EndpointPolicyError("invalid_url")
    if any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in url) or "\\" in url:
        raise EndpointPolicyError("invalid_url")

    try:
        parts = urlsplit(url)
    except ValueError:
        raise EndpointPolicyError("invalid_url") from None

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise EndpointPolicyError("disallowed_scheme")
    if scheme == "http" and not policy.allow_http:
        raise EndpointPolicyError("disallowed_scheme")

    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise EndpointPolicyError("credentials_forbidden")
    if parts.fragment or "#" in url:
        raise EndpointPolicyError("fragment_forbidden")
    if parts.query or "?" in url:
        raise EndpointPolicyError("query_forbidden")

    try:
        raw_port = parts.port
    except ValueError:
        raise EndpointPolicyError("invalid_port") from None
    if raw_port is None:
        port = 443 if scheme == "https" else 80
    else:
        port = _validate_port(raw_port, error_code="invalid_port")

    # Check if host uses bracketed IPv6 syntax in URL netloc.
    raw_netloc_host = parts.netloc.rpartition("@")[2]
    if raw_netloc_host.startswith("["):
        bracket_end = raw_netloc_host.find("]")
        if bracket_end == -1:
            raise EndpointPolicyError("invalid_host")
        raw_ipv6 = raw_netloc_host[1:bracket_end]
        if "%" in raw_ipv6 or any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in raw_ipv6):
            raise EndpointPolicyError("invalid_host")
        try:
            ip6 = ipaddress.IPv6Address(raw_ipv6)
        except ValueError:
            raise EndpointPolicyError("invalid_host") from None
        if ip6.ipv4_mapped is not None:
            path = _validate_path(parts.path)
            host = str(ip6.ipv4_mapped)
            if (host, port) not in policy.allowed_hosts:
                raise EndpointPolicyError("host_or_port_not_allowed")
            if resolved_ips is not None:
                raise EndpointPolicyError("invalid_resolution_args")
            checked_v4 = _check_resolved_ip(ip6, policy.allowed_nonpublic_cidrs)
            return ValidatedEndpoint(
                scheme=scheme,
                host=str(checked_v4),
                port=port,
                path=path,
                resolved_ips=(str(checked_v4),),
            )

    if not parts.hostname:
        raise EndpointPolicyError("invalid_host")
    host = _normalize_host(parts.hostname, error_code="invalid_host")

    path = _validate_path(parts.path)

    if (host, port) not in policy.allowed_hosts:
        raise EndpointPolicyError("host_or_port_not_allowed")

    literal_ip = _parse_ip_literal(host)
    if literal_ip is not None:
        if resolved_ips is not None:
            raise EndpointPolicyError("invalid_resolution_args")
        checked = _check_resolved_ip(literal_ip, policy.allowed_nonpublic_cidrs)
        return ValidatedEndpoint(
            scheme=scheme,
            host=host,
            port=port,
            path=path,
            resolved_ips=(str(checked),),
        )

    if resolved_ips is None:
        active_resolver = resolver or default_socket_resolver
        try:
            raw_candidates = active_resolver(host, port)
        except EndpointPolicyError:
            raise
        except Exception:
            raise EndpointPolicyError("dns_resolution_failed") from None
    else:
        raw_candidates = resolved_ips

    if isinstance(raw_candidates, (str, bytes)) or not isinstance(raw_candidates, Iterable):
        raise EndpointPolicyError("invalid_resolved_ip")

    validated_ips: list[str] = []
    seen_ips: set[str] = set()
    count = 0
    for item in raw_candidates:
        count += 1
        if count > MAX_RESOLVED_ADDRESSES:
            raise EndpointPolicyError("too_many_resolved_ips")
        checked_ip = _check_resolved_ip(item, policy.allowed_nonpublic_cidrs)
        ip_str = str(checked_ip)
        if ip_str not in seen_ips:
            seen_ips.add(ip_str)
            validated_ips.append(ip_str)

    if not validated_ips:
        raise EndpointPolicyError("empty_resolution")

    return ValidatedEndpoint(
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        resolved_ips=tuple(validated_ips),
    )
