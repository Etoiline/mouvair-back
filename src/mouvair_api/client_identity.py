import hashlib
import hmac
import ipaddress
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network

from fastapi import Request

from .config import Settings


@lru_cache(maxsize=32)
def _trusted_networks(cidrs: tuple[str, ...]) -> tuple[IPv4Network | IPv6Network, ...]:
    try:
        return tuple(ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)
    except ValueError as exc:
        raise ValueError("TRUSTED_PROXY_CIDRS contient un réseau invalide") from exc


def _is_trusted_proxy(address: str, cidrs: tuple[str, ...]) -> bool:
    try:
        client_ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(client_ip in network for network in _trusted_networks(cidrs))


def _forwarded_addresses(request: Request) -> tuple[str, ...] | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded is None:
        return None
    addresses = []
    for item in forwarded.split(","):
        try:
            addresses.append(str(ipaddress.ip_address(item.strip())))
        except ValueError:
            return None
    return tuple(addresses) or None


def _proxied_client_address(
    request: Request,
    direct_address: str,
    trusted_proxy_cidrs: tuple[str, ...],
) -> str:
    forwarded = _forwarded_addresses(request)
    if forwarded is None:
        return direct_address

    # On remonte depuis le proxy directement connecté pour ignorer les valeurs
    # qu’un client aurait pu injecter à gauche de la chaîne.
    for address in reversed(forwarded):
        if not _is_trusted_proxy(address, trusted_proxy_cidrs):
            return address
    return forwarded[0]


def client_identity(request: Request, settings: Settings) -> str:
    direct_address = request.client.host if request.client is not None else "unknown"
    address = direct_address
    if _is_trusted_proxy(direct_address, settings.trusted_proxy_cidrs):
        address = _proxied_client_address(
            request,
            direct_address,
            settings.trusted_proxy_cidrs,
        )
    return hmac.new(
        settings.client_id_hmac_secret.encode(),
        address.encode(),
        hashlib.sha256,
    ).hexdigest()
