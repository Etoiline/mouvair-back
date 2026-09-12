from types import SimpleNamespace

from starlette.requests import Request

from mouvair_api.client_identity import client_identity
from mouvair_api.config import Settings


def request_from(address: str, forwarded: str | None = None) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (address, 1234),
        "server": ("test", 80),
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
        "http_version": "1.1",
        "app": SimpleNamespace(),
    }
    return Request(scope)


def test_untrusted_clients_cannot_spoof_forwarded_addresses() -> None:
    settings = Settings(client_id_hmac_secret="secret")

    direct = client_identity(request_from("203.0.113.2"), settings)
    spoofed = client_identity(
        request_from("203.0.113.2", forwarded="198.51.100.10"), settings
    )

    assert direct == spoofed


def test_trusted_proxy_ignores_spoofed_addresses_before_the_client() -> None:
    settings = Settings(
        client_id_hmac_secret="secret",
        trusted_proxy_cidrs=("10.0.0.0/8",),
    )

    proxied = client_identity(
        request_from(
            "10.0.0.2",
            forwarded="192.0.2.99, 198.51.100.10, 10.0.0.1",
        ),
        settings,
    )
    direct = client_identity(request_from("198.51.100.10"), settings)

    assert proxied == direct
