"""Vérifie la frontière de confiance des adresses utilisées pour limiter les clients.

Les requêtes ASGI simulées distinguent le pair directement connecté des adresses
déclarées par en-tête. Les assertions comparent les HMAC plutôt que l’IP en clair.
"""

from types import SimpleNamespace

from starlette.requests import Request

from mouvair_api.client_identity import client_identity
from mouvair_api.config import Settings


def request_from(
    address: str,
    forwarded: str | None = None,
    real_address: str | None = None,
) -> Request:
    """Construit une requête minimale avec un pair réseau et des en-têtes contrôlés."""
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    if real_address is not None:
        headers.append((b"x-real-ip", real_address.encode()))
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
    """Un client direct conserve sa propre identité malgré un X-Forwarded-For forgé."""
    settings = Settings(client_id_hmac_secret="secret")

    direct = client_identity(request_from("203.0.113.2"), settings)
    spoofed = client_identity(
        request_from("203.0.113.2", forwarded="198.51.100.10"), settings
    )

    assert direct == spoofed


def test_untrusted_clients_cannot_spoof_real_address() -> None:
    """X-Real-IP ne fait autorité qu’à travers un proxy explicitement approuvé."""
    settings = Settings(client_id_hmac_secret="secret")

    direct = client_identity(request_from("203.0.113.2"), settings)
    spoofed = client_identity(
        request_from("203.0.113.2", real_address="198.51.100.10"), settings
    )

    assert direct == spoofed


def test_trusted_proxy_uses_real_address() -> None:
    """Le proxy Railway de confiance retrouve le même HMAC que le client connecté directement."""
    settings = Settings(
        client_id_hmac_secret="secret",
        trusted_proxy_cidrs=("100.0.0.0/8",),
    )

    proxied = client_identity(
        request_from("100.64.0.2", real_address="198.51.100.10"),
        settings,
    )
    direct = client_identity(request_from("198.51.100.10"), settings)

    assert proxied == direct


def test_trusted_proxy_ignores_spoofed_addresses_before_the_client() -> None:
    """La lecture depuis la droite s’arrête au vrai client avant une adresse injectée à gauche."""
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
