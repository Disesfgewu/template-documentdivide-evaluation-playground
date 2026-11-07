import ipaddress
import socket
import unittest
from unittest import mock

from lab_api.endpoint_policy import (
    EndpointPolicy,
    EndpointPolicyError,
    MAX_ALLOWED_CIDRS,
    MAX_ALLOWED_HOSTS,
    MAX_HOSTNAME_LENGTH,
    MAX_RESOLVED_ADDRESSES,
    MAX_URL_LENGTH,
    ValidatedEndpoint,
    validate_endpoint_url,
)


class EndpointPolicyPositiveTests(unittest.TestCase):
    def test_valid_https_hostname_with_public_resolved_ips(self):
        policy = EndpointPolicy(allowed_hosts=[("Method.Example.COM", 8443)])
        result = validate_endpoint_url(
            "HTTPS://method.example.com:8443/v1/query",
            policy,
            resolved_ips=["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"],
        )
        self.assertEqual(
            result,
            ValidatedEndpoint(
                scheme="https",
                host="method.example.com",
                port=8443,
                path="/v1/query",
                resolved_ips=("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
            ),
        )
        self.assertEqual(result.normalized_url, "https://method.example.com:8443/v1/query")

    def test_default_ports_and_root_path_normalization(self):
        policy = EndpointPolicy(
            allowed_hosts=[("api.example.com", 443), ("plain.example.com", 80)],
            allow_http=True,
        )
        r_https = validate_endpoint_url(
            "https://api.example.com",
            policy,
            resolver=lambda host, port: ["93.184.216.34"],
        )
        self.assertEqual((r_https.port, r_https.path), (443, "/"))
        self.assertEqual(r_https.normalized_url, "https://api.example.com:443/")

        r_http = validate_endpoint_url(
            "http://plain.example.com/v1/capabilities",
            policy,
            resolver=lambda host, port: ["93.184.216.34"],
        )
        self.assertEqual((r_http.scheme, r_http.port, r_http.path), ("http", 80, "/v1/capabilities"))

    def test_public_ip_literal_validated_without_calling_resolver(self):
        policy = EndpointPolicy(
            allowed_hosts=[("93.184.216.34", 443), ("2606:2800:220:1:248:1893:25c8:1946", 8443)]
        )
        called = False

        def boom_resolver(host: str, port: int):
            nonlocal called
            called = True
            return []

        r4 = validate_endpoint_url("https://93.184.216.34/v1/prepare", policy, resolver=boom_resolver)
        self.assertFalse(called)
        self.assertEqual(r4.resolved_ips, ("93.184.216.34",))

        r6 = validate_endpoint_url(
            "https://[2606:2800:220:1:248:1893:25c8:1946]:8443/v1/prepare",
            policy,
            resolver=boom_resolver,
        )
        self.assertFalse(called)
        self.assertEqual(r6.host, "2606:2800:220:1:248:1893:25c8:1946")
        self.assertEqual(
            r6.normalized_url,
            "https://[2606:2800:220:1:248:1893:25c8:1946]:8443/v1/prepare",
        )

    def test_explicit_nonpublic_cidr_exception_allows_matching_private_or_loopback(self):
        policy = EndpointPolicy(
            allowed_hosts=[("local-agent.internal", 8080), ("127.0.0.1", 9000), ("::1", 9001)],
            allow_http=True,
            allowed_nonpublic_cidrs=["10.20.0.0/16", "127.0.0.1/32", "::1/128"],
        )
        r_host = validate_endpoint_url(
            "http://local-agent.internal:8080/v1/query",
            policy,
            resolved_ips=["::ffff:10.20.3.4", "10.20.3.4"],
        )
        self.assertEqual(r_host.resolved_ips, ("10.20.3.4",))

        r_loop4 = validate_endpoint_url("http://127.0.0.1:9000/v1/query", policy)
        self.assertEqual(r_loop4.resolved_ips, ("127.0.0.1",))

        r_loop6 = validate_endpoint_url("http://[::1]:9001/v1/query", policy)
        self.assertEqual(r_loop6.resolved_ips, ("::1",))


class EndpointPolicyUrlAndAllowlistNegativeTests(unittest.TestCase):
    def setUp(self):
        self.policy = EndpointPolicy(
            allowed_hosts=[("allowed.example.com", 443), ("allowed.example.com", 8443)]
        )

    def assert_rejected(self, url, code, *, policy=None, resolved_ips=("93.184.216.34",), resolver=None):
        active_policy = self.policy if policy is None else policy
        kwargs = {"resolver": resolver} if resolver is not None else {"resolved_ips": resolved_ips}
        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url(url, active_policy, **kwargs)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(str(cm.exception), code)

    def test_empty_allowlist_fails_closed(self):
        empty_policy = EndpointPolicy(allowed_hosts=())
        self.assert_rejected(
            "https://allowed.example.com/v1/query",
            "host_or_port_not_allowed",
            policy=empty_policy,
        )

    def test_exact_host_and_port_allowlist_rejects_unlisted_host_subdomain_or_port(self):
        cases = [
            "https://other.example.com/v1/query",
            "https://sub.allowed.example.com/v1/query",
            "https://allowed.example.com:9443/v1/query",
            "https://allowed.example.com:80/v1/query",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assert_rejected(url, "host_or_port_not_allowed")

    def test_dangerous_and_disallowed_schemes_rejected(self):
        for url in (
            "http://allowed.example.com:443/v1/query",
            "file:///etc/passwd",
            "ftp://allowed.example.com/file",
            "gopher://allowed.example.com:70/_",
            "ws://allowed.example.com/ws",
            "wss://allowed.example.com/ws",
            "javascript:alert(1)",
            "data:text/plain;base64,SGVsbG8=",
            "://allowed.example.com/v1/query",
        ):
            with self.subTest(url=url):
                self.assert_rejected(url, "disallowed_scheme")

    def test_userinfo_and_credentials_rejected_without_echoing_secret(self):
        secret = "super-secret-token-999"
        for url in (
            f"https://user:{secret}@allowed.example.com/v1/query",
            f"https://{secret}@allowed.example.com/v1/query",
            "https://@allowed.example.com/v1/query",
            "https://allowed.example.com@evil.example.com/v1/query",
        ):
            with self.subTest(url=url):
                with self.assertRaises(EndpointPolicyError) as cm:
                    validate_endpoint_url(url, self.policy, resolved_ips=["93.184.216.34"])
                self.assertEqual(cm.exception.code, "credentials_forbidden")
                self.assertNotIn(secret, str(cm.exception))
                self.assertNotIn("allowed.example.com", str(cm.exception))

    def test_fragments_and_queries_rejected(self):
        for url in (
            "https://allowed.example.com/v1/query#frag",
            "https://allowed.example.com/v1/query#",
        ):
            with self.subTest(url=url):
                self.assert_rejected(url, "fragment_forbidden")

        for url in (
            "https://allowed.example.com/v1/query?x=1",
            "https://allowed.example.com/v1/query?",
        ):
            with self.subTest(url=url):
                self.assert_rejected(url, "query_forbidden")

    def test_invalid_ports_rejected(self):
        for url in (
            "https://allowed.example.com:0/v1/query",
            "https://allowed.example.com:65536/v1/query",
            "https://allowed.example.com:99999/v1/query",
            "https://allowed.example.com:abc/v1/query",
            "https://allowed.example.com:-80/v1/query",
        ):
            with self.subTest(url=url):
                self.assert_rejected(url, "invalid_port")

    def test_malformed_hosts_and_numeric_ip_evasions_rejected(self):
        for url in (
            "https:///v1/query",
            "https://.example.com/v1/query",
            "https://allowed..example.com/v1/query",
            "https://allowed.example.com./v1/query",
            "https://-allowed.example.com/v1/query",
            "https://allowed-.example.com/v1/query",
            "https://allowed_host.example.com/v1/query",
            "https://[fe80::1%25eth0]/v1/query",
            "https://[fe80::1%eth0]/v1/query",
            "https://2130706433/v1/query",
            "https://0x7f000001/v1/query",
            "https://0177.0.0.1/v1/query",
            "https://127.1/v1/query",
            "https://127.0.0.0x1/v1/query",
            "https://999.999.999.999/v1/query",
        ):
            with self.subTest(url=url):
                self.assert_rejected(url, "invalid_host")

    def test_path_traversal_and_malformed_paths_rejected(self):
        for url in (
            "https://allowed.example.com/../v1/query",
            "https://allowed.example.com/v1/./query",
            "https://allowed.example.com/v1/../admin",
            "https://allowed.example.com/v1//query",
            "https://allowed.example.com/v1/%2e%2e/admin",
            "https://allowed.example.com/%252e%252e/admin",
            "https://allowed.example.com/v1/%252fadmin",
            "https://allowed.example.com/v1/%2fadmin",
            "https://allowed.example.com/v1/%5cadmin",
            "https://allowed.example.com/v1/%00admin",
        ):
            with self.subTest(url=url):
                self.assert_rejected(url, "invalid_path")

    def test_malformed_url_syntax_and_control_chars_rejected(self):
        for url in (
            "",
            "https://[::1/v1/query",
            "https://[2001:db8::zzzz]/v1/query",
            "https://allowed.example.com/v1/query with space",
            "https://allowed.example.com/v1/query\r\nX-Injected: 1",
            "https://allowed.example.com\\@evil.example.com/v1/query",
            "https://allowed.example.com/v1/\u0080",
            12345,
        ):
            with self.subTest(url=url):
                self.assert_rejected(url, "invalid_url")


class EndpointPolicyResolvedAddressNegativeTests(unittest.TestCase):
    def setUp(self):
        self.policy = EndpointPolicy(allowed_hosts=[("svc.example.com", 443)])

    def test_nonpublic_ipv4_and_ipv6_addresses_rejected(self):
        nonpublic_ips = [
            "0.0.0.0",
            "0.1.2.3",
            "10.0.0.1",
            "100.64.0.1",
            "100.127.255.254",
            "127.0.0.1",
            "127.255.255.254",
            "169.254.169.254",
            "169.254.0.1",
            "172.16.0.1",
            "172.31.255.254",
            "192.0.0.1",
            "192.0.2.1",
            "192.88.99.1",
            "192.168.1.1",
            "198.18.0.1",
            "198.19.255.254",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "100::1",
            "2001:2::1",
            "2001:db8::1",
            "fc00::1",
            "fd12:3456:789a::1",
            "fe80::1",
            "fec0::1",
            "ff02::1",
        ]
        for ip in nonpublic_ips:
            with self.subTest(ip=ip):
                with self.assertRaises(EndpointPolicyError) as cm:
                    validate_endpoint_url(
                        "https://svc.example.com/v1/query",
                        self.policy,
                        resolved_ips=[ip],
                    )
                self.assertEqual(cm.exception.code, "nonpublic_ip")

    def test_mapped_and_transition_ipv6_forms_rejected(self):
        mapped_and_tunnel_ips = [
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "::ffff:169.254.169.254",
            "::ffff:100.64.0.1",
            "::ffff:0:127.0.0.1",          # IPv4-translated (RFC 2765)
            "::ffff:0:93.184.216.34",      # IPv4-translated even with public v4
            "64:ff9b::127.0.0.1",          # NAT64 well-known prefix with loopback
            "64:ff9b::93.184.216.34",      # NAT64 well-known prefix
            "64:ff9b:1::10.0.0.1",         # NAT64 local-use prefix
            "2002:7f00:0001::1",           # 6to4 embedding 127.0.0.1
            "2002:5db8:d822::1",           # 6to4 embedding 93.184.216.34
            "2001:0000:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
            "::127.0.0.1",                 # Deprecated IPv4-compatible IPv6
            "::93.184.216.34",             # Deprecated IPv4-compatible IPv6
        ]
        for ip in mapped_and_tunnel_ips:
            with self.subTest(ip=ip):
                with self.assertRaises(EndpointPolicyError) as cm:
                    validate_endpoint_url(
                        "https://svc.example.com/v1/query",
                        self.policy,
                        resolved_ips=[ip],
                    )
                self.assertEqual(cm.exception.code, "nonpublic_ip")

    def test_ipv4_mapped_url_literal_cannot_bypass_nonpublic_or_allowlist_checks(self):
        # Bracketed IPv4-mapped public IP when not in allowed_hosts -> host_or_port_not_allowed
        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url("https://[::ffff:93.184.216.34]:443/v1/query", self.policy)
        self.assertEqual(cm.exception.code, "host_or_port_not_allowed")

        # Bracketed IPv4-mapped loopback is rejected as nonpublic_ip even if 127.0.0.1 is in allowed_hosts
        policy_host_only = EndpointPolicy(allowed_hosts=[("127.0.0.1", 443)])
        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url("https://[::ffff:127.0.0.1]:443/v1/query", policy_host_only)
        self.assertEqual(cm.exception.code, "nonpublic_ip")

        # Policy itself rejects raw IPv4-mapped IPv6 host literals
        with self.assertRaises(EndpointPolicyError) as cm:
            EndpointPolicy(allowed_hosts=[("::ffff:127.0.0.1", 443)])
        self.assertEqual(cm.exception.code, "invalid_policy")

    def test_mixed_public_and_nonpublic_resolved_ips_rejected_all_or_nothing(self):
        # Prevents DNS round-robin / dual-record rebinding where one record is internal
        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url(
                "https://svc.example.com/v1/query",
                self.policy,
                resolved_ips=["93.184.216.34", "169.254.169.254"],
            )
        self.assertEqual(cm.exception.code, "nonpublic_ip")

    def test_empty_malformed_or_scoped_resolved_ips_rejected(self):
        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url("https://svc.example.com/v1/query", self.policy, resolved_ips=[])
        self.assertEqual(cm.exception.code, "empty_resolution")

        for bad_ip in (
            "not-an-ip",
            "fe80::1%eth0",
            "2606:4700:4700::1111%eth0",
            ipaddress.IPv6Address("2606:4700:4700::1111%eth0"),
            ipaddress.IPv6Address("fe80::1%eth0"),
            "93.184.216.34/32",
            "",
            "127.0.0.1:8080",
            12345,
        ):
            with self.subTest(bad_ip=bad_ip):
                with self.assertRaises(EndpointPolicyError) as cm:
                    validate_endpoint_url(
                        "https://svc.example.com/v1/query",
                        self.policy,
                        resolved_ips=[bad_ip],  # type: ignore[list-item]
                    )
                self.assertEqual(cm.exception.code, "invalid_resolved_ip")

    def test_resolver_failure_and_socket_resolver_integration(self):
        def failing_resolver(host: str, port: int):
            raise OSError("temporary failure in name resolution")

        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url(
                "https://svc.example.com/v1/query",
                self.policy,
                resolver=failing_resolver,
            )
        self.assertEqual(cm.exception.code, "dns_resolution_failed")

        fake_addrinfo = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
        ]
        with mock.patch("lab_api.endpoint_policy.socket.getaddrinfo", return_value=fake_addrinfo) as m_gai:
            res = validate_endpoint_url("https://svc.example.com/v1/query", self.policy)
            m_gai.assert_called_once_with(
                "svc.example.com",
                443,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        self.assertEqual(res.resolved_ips, ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"))


class EndpointPolicyBoundsTests(unittest.TestCase):
    def test_url_and_hostname_length_bounds(self):
        policy = EndpointPolicy(allowed_hosts=[("svc.example.com", 443)])
        overlong_url = "https://svc.example.com/" + ("a" * MAX_URL_LENGTH)
        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url(overlong_url, policy, resolved_ips=["93.184.216.34"])
        self.assertEqual(cm.exception.code, "invalid_url")

        long_label = "a" * 64 + ".example.com"
        with self.assertRaises(EndpointPolicyError) as cm:
            EndpointPolicy(allowed_hosts=[(long_label, 443)])
        self.assertEqual(cm.exception.code, "invalid_policy")

        many_labels = ".".join(["a" * 50] * 6)
        self.assertGreater(len(many_labels), MAX_HOSTNAME_LENGTH)
        with self.assertRaises(EndpointPolicyError) as cm:
            EndpointPolicy(allowed_hosts=[(many_labels, 443)])
        self.assertEqual(cm.exception.code, "invalid_policy")

    def test_resolved_address_count_bound(self):
        policy = EndpointPolicy(allowed_hosts=[("svc.example.com", 443)])
        ips = [str(ipaddress.IPv4Address("93.184.216.1") + i) for i in range(MAX_RESOLVED_ADDRESSES + 1)]
        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url("https://svc.example.com/v1/query", policy, resolved_ips=ips)
        self.assertEqual(cm.exception.code, "too_many_resolved_ips")

    def test_policy_allowlist_and_cidr_bounds_and_invalid_inputs(self):
        too_many_hosts = [(f"h{i}.example.com", 443) for i in range(MAX_ALLOWED_HOSTS + 1)]
        with self.assertRaises(EndpointPolicyError) as cm:
            EndpointPolicy(allowed_hosts=too_many_hosts)
        self.assertEqual(cm.exception.code, "invalid_policy")

        # Duplicate host entries still count toward the consumed iterable bound
        consumed_hosts = 0

        def duplicate_host_stream():
            nonlocal consumed_hosts
            for _ in range(MAX_ALLOWED_HOSTS * 10):
                consumed_hosts += 1
                yield ("svc.example.com", 443)

        with self.assertRaises(EndpointPolicyError) as cm:
            EndpointPolicy(allowed_hosts=duplicate_host_stream())
        self.assertEqual(cm.exception.code, "invalid_policy")
        self.assertEqual(consumed_hosts, MAX_ALLOWED_HOSTS + 1)

        too_many_cidrs = [f"10.{i // 256}.{i % 256}.0/24" for i in range(MAX_ALLOWED_CIDRS + 1)]
        with self.assertRaises(EndpointPolicyError) as cm:
            EndpointPolicy(allowed_hosts=[("svc.example.com", 443)], allowed_nonpublic_cidrs=too_many_cidrs)
        self.assertEqual(cm.exception.code, "invalid_policy")

        # Duplicate CIDR entries still count toward the consumed iterable bound
        consumed_cidrs = 0

        def duplicate_cidr_stream():
            nonlocal consumed_cidrs
            for _ in range(MAX_ALLOWED_CIDRS * 10):
                consumed_cidrs += 1
                yield "10.0.0.0/8"

        with self.assertRaises(EndpointPolicyError) as cm:
            EndpointPolicy(
                allowed_hosts=[("svc.example.com", 443)],
                allowed_nonpublic_cidrs=duplicate_cidr_stream(),
            )
        self.assertEqual(cm.exception.code, "invalid_policy")
        self.assertEqual(consumed_cidrs, MAX_ALLOWED_CIDRS + 1)

        # Wildcard /0 nonpublic CIDR override is forbidden
        for wildcard_cidr in ("0.0.0.0/0", "::/0"):
            with self.subTest(cidr=wildcard_cidr):
                with self.assertRaises(EndpointPolicyError) as cm:
                    EndpointPolicy(
                        allowed_hosts=[("svc.example.com", 443)],
                        allowed_nonpublic_cidrs=[wildcard_cidr],
                    )
                self.assertEqual(cm.exception.code, "invalid_policy")

        # Host bits set in CIDR (strict=True) or non-boolean allow_http rejected
        with self.assertRaises(EndpointPolicyError):
            EndpointPolicy(
                allowed_hosts=[("svc.example.com", 443)],
                allowed_nonpublic_cidrs=["10.0.0.1/8"],
            )
        with self.assertRaises(EndpointPolicyError):
            EndpointPolicy(allowed_hosts=[("svc.example.com", True)])  # type: ignore[list-item]
        with self.assertRaises(EndpointPolicyError):
            EndpointPolicy(allowed_hosts=[("svc.example.com", 443)], allow_http=1)  # type: ignore[arg-type]

    def test_conflicting_or_invalid_resolution_args_rejected(self):
        policy = EndpointPolicy(allowed_hosts=[("svc.example.com", 443), ("93.184.216.34", 443)])
        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url(
                "https://svc.example.com/v1/query",
                policy,
                resolved_ips=["93.184.216.34"],
                resolver=lambda h, p: ["93.184.216.34"],
            )
        self.assertEqual(cm.exception.code, "invalid_resolution_args")

        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url(
                "https://93.184.216.34/v1/query",
                policy,
                resolved_ips=["93.184.216.34"],
            )
        self.assertEqual(cm.exception.code, "invalid_resolution_args")

        with self.assertRaises(EndpointPolicyError) as cm:
            validate_endpoint_url(
                "https://svc.example.com/v1/query",
                policy,
                resolved_ips="93.184.216.34",  # type: ignore[arg-type]
            )
        self.assertEqual(cm.exception.code, "invalid_resolved_ip")


if __name__ == "__main__":
    unittest.main()
