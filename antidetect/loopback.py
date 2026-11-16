"""Local control transports that never use environment proxies or redirects."""
import ipaddress
import math
import urllib.request
from urllib.parse import urlsplit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validated_parts(url, schemes):
    try:
        parts = urlsplit(url)
        address = ipaddress.ip_address(parts.hostname or "")
        valid = (
            parts.scheme in schemes and address.is_loopback
            and parts.port is not None and 0 < parts.port <= 65535
            and parts.username is None and parts.password is None
            and not parts.fragment
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Control requests require a literal loopback origin with an explicit port")
    return parts


def _check_timeout(timeout):
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("Control request timeout must be finite and positive")


def open_loopback(request_or_url, *, timeout):
    """Open local HTTP(S), leaving user traffic and global proxy settings alone."""
    url = (
        request_or_url.full_url
        if isinstance(request_or_url, urllib.request.Request)
        else request_or_url
    )
    parts = _validated_parts(url, {"http", "https"})
    _check_timeout(timeout)
    if isinstance(request_or_url, urllib.request.Request):
        host = request_or_url.get_header("Host")
        if host is not None and host.lower() != parts.netloc.lower():
            raise ValueError("Control request Host does not match its origin")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request_or_url, timeout=timeout)


def connect_loopback(url, **kwargs):
    """Return the pinned websockets connector for a local CDP endpoint.

    websockets 16 discovers environment proxies and follows redirects by default.
    CDP must do neither, including when an endpoint was returned by /json/list.
    """
    import websockets

    _validated_parts(url, {"ws", "wss"})
    if any(key in kwargs for key in ("host", "port", "sock", "unix", "server_hostname")):
        raise ValueError("Control WebSocket endpoint overrides are not allowed")
    if kwargs.get("proxy") is not None:
        raise ValueError("Control WebSockets cannot use a proxy")
    kwargs["proxy"] = None
    kwargs.setdefault("open_timeout", 10)
    kwargs.setdefault("close_timeout", 5)
    _check_timeout(kwargs["open_timeout"])
    _check_timeout(kwargs["close_timeout"])
    connection = websockets.connect(url, **kwargs)
    # Per-connector redirect policy; no mutation of the websockets module.
    # This is covered with a real HTTP redirect fixture against the pinned build.
    connection.process_redirect = lambda exc: exc
    return connection
