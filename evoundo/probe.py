"""Declarative reconciliation probes for external SaaS and HTTP APIs.

Secured against Server-Side Request Forgery (SSRF) attacks targeting cloud
metadata services (IMDS), RFC 1918 private subnets, loopback addresses,
alternate IP encodings, dangerous URI schemes, and redirect hopping.
"""

from __future__ import annotations
import http.client
import ipaddress
import json
import logging
import re
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Union

logger = logging.getLogger("evoundo.probe")


class SecurityError(Exception):
    """Base exception for security violations in EvoUndo."""
    pass


class SSRFSecurityError(SecurityError):
    """Raised when an HTTP probe attempts to target a private, loopback, or cloud metadata endpoint."""
    pass


# Explicitly blocked cloud instance metadata addresses
BLOCKED_METADATA_IPS: Set[str] = {
    "169.254.169.254",  # AWS/GCP/Azure IMDS IPv4
    "169.254.169.253",  # AWS DNS / local metadata
    "169.254.170.2",    # AWS ECS Task metadata
    "fd00:ec2::254",    # AWS IMDS IPv6
}

BLOCKED_DOMAINS: Set[str] = {
    "localhost",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}

BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.64.0.0/10"),   # Shared transition space / Carrier Grade NAT
    ipaddress.ip_network("198.18.0.0/15"),   # Benchmarking
    ipaddress.ip_network("fc00::/7"),        # Unique local IPv6
]


def _check_ip_address(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> None:
    """Validate that an IP address is public and globally routable.

    Raises SSRFSecurityError if the IP is private, loopback, link-local,
    multicast, reserved, unspecified, or a known cloud metadata endpoint.
    """
    # Check IPv4-mapped IPv6 (e.g., ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        _check_ip_address(ip.ipv4_mapped)
        return

    ip_str = str(ip)
    if ip_str in BLOCKED_METADATA_IPS:
        raise SSRFSecurityError(f"Blocked SSRF attempt to cloud metadata IP: '{ip_str}'")

    if ip.is_loopback:
        raise SSRFSecurityError(f"Blocked SSRF attempt to loopback IP: '{ip_str}'")

    if ip.is_private:
        raise SSRFSecurityError(f"Blocked SSRF attempt to private network IP: '{ip_str}'")

    if ip.is_link_local:
        raise SSRFSecurityError(f"Blocked SSRF attempt to link-local IP: '{ip_str}'")

    if ip.is_multicast:
        raise SSRFSecurityError(f"Blocked SSRF attempt to multicast IP: '{ip_str}'")

    if ip.is_reserved:
        raise SSRFSecurityError(f"Blocked SSRF attempt to reserved IP: '{ip_str}'")

    if ip.is_unspecified:
        raise SSRFSecurityError(f"Blocked SSRF attempt to unspecified IP: '{ip_str}'")

    for net in BLOCKED_NETWORKS:
        if ip in net:
            raise SSRFSecurityError(f"Blocked SSRF attempt to restricted subnet '{net}': '{ip_str}'")


def _parse_alternate_ip_encoding(hostname: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """Detect and convert integer, hex, octal, or dotted octal IP encodings."""
    # Strip brackets if IPv6
    clean_host = hostname.strip("[]")

    # Direct standard parse
    try:
        return ipaddress.ip_address(clean_host)
    except ValueError:
        pass

    # Check for single integer (decimal, hex like 0x7f000001, or octal like 017700000001)
    try:
        int_val = int(clean_host, 0)
        if 0 <= int_val <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(int_val)
    except (ValueError, TypeError):
        pass

    # Check for octal dotted format (e.g. 0177.0.0.1)
    if "." in clean_host:
        parts = clean_host.split(".")
        if len(parts) == 4:
            try:
                oct_parts = [int(p, 8) if p.startswith("0") and len(p) > 1 else int(p, 10) for p in parts]
                if all(0 <= p <= 255 for p in oct_parts):
                    return ipaddress.IPv4Address(bytes(oct_parts))
            except (ValueError, TypeError):
                pass

    return None


def validate_probe_destination(url: str) -> None:
    """Strict SSRF validation of probe target URL.

    Enforces scheme whitelist (http, https), blocks cloud metadata domains/IPs,
    RFC 1918 subnets, loopbacks, alternate encodings, and resolves DNS to verify
    all resolved IP destinations.
    """
    if not url or not isinstance(url, str):
        raise SSRFSecurityError("Probe destination URL cannot be empty")

    parsed = urllib.parse.urlsplit(url.strip())

    # 1. Scheme Whitelist
    if parsed.scheme.lower() not in ("http", "https"):
        raise SSRFSecurityError(
            f"Disallowed URI scheme '{parsed.scheme}'. Only 'http' and 'https' are permitted for probes."
        )

    hostname = parsed.hostname
    if not hostname:
        raise SSRFSecurityError(f"Invalid probe URL: missing hostname in '{url}'")

    hostname_lower = hostname.lower()

    # 2. Blocked Hostnames & Internal Domains
    if hostname_lower in BLOCKED_DOMAINS:
        raise SSRFSecurityError(f"Blocked SSRF attempt to restricted internal domain: '{hostname}'")

    if hostname_lower.endswith(".internal") or hostname_lower.endswith(".local"):
        raise SSRFSecurityError(f"Blocked SSRF attempt to internal network domain: '{hostname}'")

    # 3. Check Alternate IP Encodings
    direct_ip = _parse_alternate_ip_encoding(hostname)
    if direct_ip is not None:
        _check_ip_address(direct_ip)

    # 4. Resolve Hostname to all destination IPs and validate each
    try:
        addr_info = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        # If hostname does not resolve but matches known metadata or internal names, block
        if "metadata" in hostname_lower or "internal" in hostname_lower or "localhost" in hostname_lower:
            raise SSRFSecurityError(f"Blocked SSRF attempt to unresolved internal target: '{hostname}'") from e
        # Otherwise gaierror will be handled during connection
        return

    resolved_ips: Set[str] = set()
    for entry in addr_info:
        ip_addr_str = entry[4][0]
        resolved_ips.add(ip_addr_str)

    if not resolved_ips:
        raise SSRFSecurityError(f"No IP address could be resolved for probe destination '{hostname}'")

    for ip_str in resolved_ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            _check_ip_address(ip_obj)
        except ValueError:
            raise SSRFSecurityError(f"Invalid resolved IP address '{ip_str}' for probe destination '{hostname}'")

    return list(resolved_ips)[0]


class PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that pins destination to a pre-validated IP address."""
    def __init__(self, host, port=None, pinned_ip=None, **kwargs):
        super().__init__(host, port, **kwargs)
        self.pinned_ip = pinned_ip
        if pinned_ip:
            self._audit_destination = pinned_ip

    def connect(self):
        self._audit_destination = self.pinned_ip
        if self.pinned_ip:
            self.sock = socket.create_connection(
                (self.pinned_ip, self.port), self.timeout, self.source_address
            )
        else:
            super().connect()


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that pins destination to a pre-validated IP address."""
    def __init__(self, host, port=None, pinned_ip=None, **kwargs):
        super().__init__(host, port, **kwargs)
        self.pinned_ip = pinned_ip
        if pinned_ip:
            self._audit_destination = pinned_ip

    def connect(self):
        self._audit_destination = self.pinned_ip
        if self.pinned_ip:
            self.sock = socket.create_connection(
                (self.pinned_ip, self.port), self.timeout, self.source_address
            )
            if self._tunnel_host:
                self._tunnel()
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=self.host
            )
        else:
            super().connect()


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip: Optional[str] = None):
        super().__init__()
        self.pinned_ip = pinned_ip

    def http_open(self, req):
        return self.do_open(
            lambda host, **kwargs: PinnedHTTPConnection(host, pinned_ip=self.pinned_ip, **kwargs),
            req,
        )


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: Optional[str] = None):
        super().__init__()
        self.pinned_ip = pinned_ip

    def https_open(self, req):
        return self.do_open(
            lambda host, **kwargs: PinnedHTTPSConnection(host, pinned_ip=self.pinned_ip, **kwargs),
            req,
        )


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTP redirect handler that validates every redirect target against the SSRF policy."""

    def __init__(self, max_redirects: int = 3):
        self.max_redirects = max_redirects
        self.redirect_count = 0
        self.latest_validated_ip: Optional[str] = None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirect_count += 1
        if self.redirect_count > self.max_redirects:
            raise SSRFSecurityError(
                f"Exceeded maximum redirect limit ({self.max_redirects}). Potential SSRF redirect loop."
            )

        # Validate redirect target URL
        self.latest_validated_ip = validate_probe_destination(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class HTTPProbe:
    """Declarative HTTP GET/POST probe for verifying external side-effects with SSRF protection."""
    url: str
    method: str = "GET"
    headers: Dict[str, str] = field(default_factory=dict)
    validator: Optional[Callable[[Any], bool]] = None
    timeout: float = 3.0

    def execute_probe(self, bound_args: Dict[str, Any]) -> Any:
        """Interpolate URL template with runtime arguments, validate SSRF policy, and execute probe."""
        try:
            # Safely interpolate known keys
            formatted_url = self.url.format(**bound_args)
        except KeyError:
            formatted_url = self.url

        # Validate target against SSRF policy before opening connection
        validated_ip = validate_probe_destination(formatted_url)

        # Use safe opener equipped with SafeRedirectHandler and pinned connection handlers
        redirect_handler = SafeRedirectHandler(max_redirects=3)
        handlers = [
            redirect_handler,
            PinnedHTTPHandler(pinned_ip=validated_ip),
            PinnedHTTPSHandler(pinned_ip=validated_ip),
        ]
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(formatted_url, headers=self.headers, method=self.method)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
        except SSRFSecurityError:
            # Fail closed on security violations: never swallow SSRFSecurityError!
            raise
        except Exception as e:
            logger.debug("Probe execution failed for '%s': %s", formatted_url, e)
            return None

    def evaluate_validator(self, probe_result: Any) -> bool:
        """Check if probe response indicates that the operation committed."""
        if probe_result is None:
            return False
        if self.validator is not None:
            return bool(self.validator(probe_result))
        # Default: if probe_result is non-empty list or dict
        if isinstance(probe_result, (list, dict)):
            return len(probe_result) > 0
        return bool(probe_result)
