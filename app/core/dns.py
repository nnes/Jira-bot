"""DNS override — redirect specific hostnames to fixed IPs at socket level.

Used to reach internal services (Jira, Confluence) from cloud deployments where
platform DNS cannot resolve private hostnames. The hostname and TLS SNI remain
unchanged so SSL certificates validate correctly; only the TCP target IP changes.

Activated by the DNS_OVERRIDES env var:
    DNS_OVERRIDES=jira.zalopay.vn:49.213.117.10,confluence.zalopay.vn:49.213.117.10
"""
import logging
import socket

logger = logging.getLogger(__name__)

_original_getaddrinfo = socket.getaddrinfo
_overrides: dict[str, str] = {}


def _patched_getaddrinfo(host, port, *args, **kwargs):
    if isinstance(host, str) and host in _overrides:
        host = _overrides[host]
    return _original_getaddrinfo(host, port, *args, **kwargs)


def apply_dns_overrides(overrides_str: str) -> None:
    """Parse DNS_OVERRIDES and install the socket.getaddrinfo patch.

    Safe to call multiple times — re-parses and updates the override table.
    No-op when overrides_str is empty.
    """
    if not overrides_str.strip():
        return

    for entry in overrides_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.warning("dns: invalid override entry %r (expected host:ip) — skipped", entry)
            continue
        hostname, ip = entry.rsplit(":", 1)
        hostname, ip = hostname.strip(), ip.strip()
        if hostname and ip:
            _overrides[hostname] = ip
            logger.info("dns: override %s → %s", hostname, ip)

    if _overrides and socket.getaddrinfo is not _patched_getaddrinfo:
        socket.getaddrinfo = _patched_getaddrinfo
        logger.info("dns: patch installed for %d hostname(s)", len(_overrides))
