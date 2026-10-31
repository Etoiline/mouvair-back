"""Construit une identité de limitation à partir de l’adresse réseau du client.

Les en-têtes de proxy ne sont interprétés que si le pair directement connecté
appartient à un réseau de confiance. L’adresse retenue est transformée par HMAC
avant de servir de clé Redis, sans conserver cette adresse en clair dans la clé.
"""

import hashlib
import hmac
import ipaddress
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network

from fastapi import Request

from .config import Settings


@lru_cache(maxsize=32)
def _trusted_networks(cidrs: tuple[str, ...]) -> tuple[IPv4Network | IPv6Network, ...]:
    """Parse une fois les listes de réseaux réutilisées par les requêtes suivantes."""
    try:
        return tuple(ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)
    except ValueError as exc:
        raise ValueError("TRUSTED_PROXY_CIDRS contient un réseau invalide") from exc


def _is_trusted_proxy(address: str, cidrs: tuple[str, ...]) -> bool:
    """Vérifie une adresse IPv4 ou IPv6 et refuse une adresse réseau mal formée."""
    try:
        client_ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(client_ip in network for network in _trusted_networks(cidrs))


def _forwarded_addresses(request: Request) -> tuple[str, ...] | None:
    """Valide toute la chaîne X-Forwarded-For avant d’en utiliser une adresse.

    Une seule entrée invalide fait ignorer l’en-tête complet pour éviter de
    reconstruire une chaîne de confiance à partir de fragments ambigus.
    """
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


def _real_address(request: Request) -> str | None:
    """Accepte X-Real-IP uniquement sous la forme d’un en-tête unique et d’une IP."""
    values = request.headers.getlist("X-Real-IP")
    if len(values) != 1:
        return None
    try:
        return str(ipaddress.ip_address(values[0].strip()))
    except ValueError:
        return None


def _proxied_client_address(
    request: Request,
    direct_address: str,
    trusted_proxy_cidrs: tuple[str, ...],
) -> str:
    """Résout l’adresse derrière un proxy déjà reconnu comme fiable.

    X-Real-IP valide est prioritaire. À défaut, la chaîne X-Forwarded-For est
    parcourue depuis le proxy le plus proche jusqu’au premier pair non fiable.
    """
    # Railway fournit une adresse unique et normalisée dans X-Real-IP. Elle n'est
    # acceptée que lorsque le pair directement connecté est un proxy de confiance.
    real_address = _real_address(request)
    if real_address is not None:
        return real_address

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
    """Produit un identifiant stable pour une IP et le secret configuré.

    Une adresse partagée, par exemple derrière un même accès Internet, partage
    aussi les limites. Le HMAC identifie un client réseau, pas un compte utilisateur.
    """
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
