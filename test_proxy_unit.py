from urllib.parse import quote
from unittest.mock import Mock

import pytest
import requests

from antidetect.models import ProxyConfig
from antidetect.proxy import check_proxy, get_public_ip_info, parse_proxy_line


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


def test_ipwho_success_normalizes_dict_timezone(monkeypatch):
    get = Mock(return_value=Response({
        "success": True,
        "ip": "203.0.113.1",
        "country": "Example",
        "country_code": "EX",
        "city": "Example City",
        "region": "Example Region",
        "timezone": {"id": "Etc/UTC"},
        "latitude": 1.5,
        "longitude": 2.5,
        "connection": {"isp": "Example ISP"},
    }))
    monkeypatch.setattr("antidetect.proxy.requests.get", get)

    result = get_public_ip_info(timeout=3)

    assert result == {
        "success": True,
        "ip": "203.0.113.1",
        "country": "Example",
        "country_code": "EX",
        "city": "Example City",
        "region": "Example Region",
        "timezone": "Etc/UTC",
        "lat": 1.5,
        "lon": 2.5,
        "isp": "Example ISP",
    }
    get.assert_called_once()
    assert get.call_args.kwargs["timeout"] == 3


@pytest.mark.parametrize(
    "data, location",
    [
        ({"success": False, "message": "unavailable"}, "1.25,2.5"),
        ({"success": False}, None),
    ],
)
def test_ipwho_failure_falls_back_to_ipinfo(monkeypatch, data, location):
    get = Mock(side_effect=[Response(data), Response({
        "ip": "203.0.113.2",
        "country": "EX",
        "city": "Fallback City",
        "org": "Fallback ISP",
        "loc": location,
    })])
    monkeypatch.setattr("antidetect.proxy.requests.get", get)

    result = get_public_ip_info()

    assert result["success"] is True
    assert result["lat"] == ("1.25" if location else None)
    assert result["lon"] == ("2.5" if location else None)
    assert result["country_code"] == "EX"
    assert result["timezone"] == "UTC"
    assert result["isp"] == "Fallback ISP"
    assert get.call_count == 2
    assert get.call_args_list[1].args[0] == "https://ipinfo.io/json"


def test_geoip_failure_reports_both_errors(monkeypatch):
    get = Mock(side_effect=[
        requests.Timeout("primary timed out"),
        requests.ConnectionError("fallback unavailable"),
    ])
    monkeypatch.setattr("antidetect.proxy.requests.get", get)

    result = get_public_ip_info()

    assert result == {
        "success": False,
        "error": (
            "ipwho.is: Timeout: primary timed out; "
            "ipinfo.io: ConnectionError: fallback unavailable"
        ),
    }


@pytest.mark.parametrize(
    "proxy",
    [
        ProxyConfig(enabled=False, host="proxy.example", port=8080),
        ProxyConfig(enabled=True, host="", port=8080),
        ProxyConfig(enabled=True, host="proxy.example", port=0),
    ],
)
def test_check_proxy_reports_missing_configuration(monkeypatch, proxy):
    get = Mock(side_effect=AssertionError("request must not be made"))
    monkeypatch.setattr("antidetect.proxy.requests.get", get)

    assert check_proxy(proxy)["success"] is False
    assert check_proxy(proxy)["error"] == "Proxy chua duoc cau hinh"
    get.assert_not_called()


def test_check_proxy_reports_configuration_error(monkeypatch):
    get = Mock(side_effect=AssertionError("request must not be made"))
    monkeypatch.setattr("antidetect.proxy.requests.get", get)
    proxy = ProxyConfig(enabled=True, protocol="invalid", host="proxy.example", port=8080)

    result = check_proxy(proxy)

    assert result["success"] is False
    assert result["code"] == "INVALID_PROXY_CONFIG"
    get.assert_not_called()


def test_check_proxy_reports_missing_socks_dependencies(monkeypatch):
    monkeypatch.setattr(
        "antidetect.proxy.requests.get",
        Mock(side_effect=requests.exceptions.InvalidSchema(
            "Missing dependencies for SOCKS support"
        )),
    )
    proxy = ProxyConfig(enabled=True, protocol="socks5", host="proxy.example", port=1080)

    result = check_proxy(proxy)

    assert result["success"] is False
    assert result["error"] == (
        "Thieu thu vien PySocks de chay proxy SOCKS5. Vui long cai dat: pip install PySocks"
    )


def test_check_proxy_redacts_proxy_url_and_password_forms(monkeypatch):
    password = "sëcret/word"
    proxy = ProxyConfig(
        enabled=True,
        host="proxy.example",
        port=8080,
        username="alice",
        password=password,
    )
    proxy_url = proxy.to_url()
    encoded_password = quote(password, safe="")
    exception = requests.RequestException(
        f"failed via {proxy_url}; raw={password}; encoded={encoded_password}"
    )
    monkeypatch.setattr("antidetect.proxy.requests.get", Mock(side_effect=exception))

    result = check_proxy(proxy)

    assert result["success"] is False
    assert proxy_url not in result["error"]
    assert password not in result["error"]
    assert encoded_password not in result["error"]
    assert proxy.to_display_url() in result["error"]


def test_parse_proxy_line_without_scheme():
    proxy = parse_proxy_line("proxy.example:8080")

    assert (proxy.protocol, proxy.host, proxy.port) == ("http", "proxy.example", 8080)


def test_parse_proxy_line_ipv6():
    proxy = parse_proxy_line("[::1]:8080")

    assert (proxy.host, proxy.port) == ("::1", 8080)


def test_parse_proxy_line_rejects_invalid_ipv6():
    with pytest.raises(ValueError):
        parse_proxy_line("http://[not-ipv6]:8080")


@pytest.mark.parametrize("url", [
    "http://proxy.example:8080/path",
    "http://proxy.example:8080/?mode=1",
])
def test_parse_proxy_line_rejects_path_and_query(url):
    with pytest.raises(ValueError):
        parse_proxy_line(url)


@pytest.mark.parametrize("scheme, protocol", [("socks5h", "socks5"), ("socks4a", "socks4")])
def test_parse_proxy_line_normalizes_socks_schemes(scheme, protocol):
    proxy = parse_proxy_line(f"{scheme}://proxy.example:1080")

    assert proxy.protocol == protocol


def test_parse_proxy_line_rejects_unsupported_scheme():
    with pytest.raises(ValueError):
        parse_proxy_line("ftp://proxy.example:21")


def test_parse_proxy_line_decodes_percent_encoded_credentials():
    proxy = parse_proxy_line(
        "http://alice%40example.com:p%40ss%2Fword@proxy.example:8080"
    )

    assert (proxy.username, proxy.password) == ("alice@example.com", "p@ss/word")


@pytest.mark.parametrize("line", ["", "   "])
def test_parse_proxy_line_rejects_empty_input(line):
    with pytest.raises(ValueError):
        parse_proxy_line(line)


def test_parse_proxy_line_requires_port():
    with pytest.raises(ValueError):
        parse_proxy_line("proxy.example")
