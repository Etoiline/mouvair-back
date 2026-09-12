import os
from dataclasses import dataclass
from ipaddress import ip_network
from typing import Literal

RedisFailureMode = Literal["closed", "local"]

OFFICIAL_QUOTA_MINUTE = 600
OFFICIAL_QUOTA_HOUR = 5_000
OFFICIAL_QUOTA_DAY = 10_000
OFFICIAL_QUOTA_MONTH = 300_000


def _integer(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un entier") from exc


def _float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un nombre") from exc


def _items(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    frontend_origins: tuple[str, ...] = ("http://localhost:5173",)
    redis_url: str | None = None
    redis_key_prefix: str = "mouvair"
    redis_failure_mode: RedisFailureMode = "closed"
    redis_socket_timeout_seconds: float = 0.5
    redis_connect_timeout_seconds: float = 0.5
    redis_retry_delay_seconds: float = 5.0
    redis_max_connections: int = 20
    open_meteo_quota_scope: str = "free-api"
    open_meteo_quota_minute: int = 540
    open_meteo_quota_hour: int = 4_500
    open_meteo_quota_day: int = 9_000
    open_meteo_quota_month: int = 270_000
    geocoding_cache_ttl_seconds: int = 86_400
    geocoding_negative_cache_ttl_seconds: int = 300
    geocoding_local_cache_max_entries: int = 512
    geocoding_lock_ttl_seconds: int = 12
    geocoding_lock_wait_seconds: float = 4.0
    forecast_shared_cache_ttl_seconds: int = 86_400
    forecast_lock_ttl_seconds: int = 20
    forecast_lock_wait_seconds: float = 6.0
    location_rate_limit_minute: int = 30
    location_rate_limit_hour: int = 300
    forecast_rate_limit_minute: int = 10
    forecast_rate_limit_hour: int = 60
    client_id_hmac_secret: str = "mouvair-local-development"
    trusted_proxy_cidrs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.redis_failure_mode not in ("closed", "local"):
            raise ValueError("REDIS_FAILURE_MODE doit valoir closed ou local")
        if not self.redis_key_prefix.strip(": "):
            raise ValueError("REDIS_KEY_PREFIX ne peut pas être vide")
        if not self.open_meteo_quota_scope.strip(": "):
            raise ValueError("OPEN_METEO_QUOTA_SCOPE ne peut pas être vide")
        if not self.client_id_hmac_secret:
            raise ValueError("CLIENT_ID_HMAC_SECRET ne peut pas être vide")
        try:
            for cidr in self.trusted_proxy_cidrs:
                ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ValueError("TRUSTED_PROXY_CIDRS contient un réseau invalide") from exc

        positive_values = {
            "REDIS_SOCKET_TIMEOUT_SECONDS": self.redis_socket_timeout_seconds,
            "REDIS_CONNECT_TIMEOUT_SECONDS": self.redis_connect_timeout_seconds,
            "REDIS_RETRY_DELAY_SECONDS": self.redis_retry_delay_seconds,
            "REDIS_MAX_CONNECTIONS": self.redis_max_connections,
            "GEOCODING_CACHE_TTL_SECONDS": self.geocoding_cache_ttl_seconds,
            "GEOCODING_NEGATIVE_CACHE_TTL_SECONDS": self.geocoding_negative_cache_ttl_seconds,
            "GEOCODING_LOCAL_CACHE_MAX_ENTRIES": self.geocoding_local_cache_max_entries,
            "GEOCODING_LOCK_TTL_SECONDS": self.geocoding_lock_ttl_seconds,
            "GEOCODING_LOCK_WAIT_SECONDS": self.geocoding_lock_wait_seconds,
            "FORECAST_SHARED_CACHE_TTL_SECONDS": self.forecast_shared_cache_ttl_seconds,
            "FORECAST_LOCK_TTL_SECONDS": self.forecast_lock_ttl_seconds,
            "FORECAST_LOCK_WAIT_SECONDS": self.forecast_lock_wait_seconds,
            "LOCATION_RATE_LIMIT_MINUTE": self.location_rate_limit_minute,
            "LOCATION_RATE_LIMIT_HOUR": self.location_rate_limit_hour,
            "FORECAST_RATE_LIMIT_MINUTE": self.forecast_rate_limit_minute,
            "FORECAST_RATE_LIMIT_HOUR": self.forecast_rate_limit_hour,
        }
        for name, value in positive_values.items():
            if value <= 0:
                raise ValueError(f"{name} doit être strictement positif")

        quota_limits = (
            ("OPEN_METEO_QUOTA_MINUTE", self.open_meteo_quota_minute, OFFICIAL_QUOTA_MINUTE),
            ("OPEN_METEO_QUOTA_HOUR", self.open_meteo_quota_hour, OFFICIAL_QUOTA_HOUR),
            ("OPEN_METEO_QUOTA_DAY", self.open_meteo_quota_day, OFFICIAL_QUOTA_DAY),
            ("OPEN_METEO_QUOTA_MONTH", self.open_meteo_quota_month, OFFICIAL_QUOTA_MONTH),
        )
        for name, value, official_limit in quota_limits:
            if value < 2 or value >= official_limit:
                raise ValueError(f"{name} doit être compris entre 2 et {official_limit - 1}")

    @classmethod
    def from_env(cls) -> "Settings":
        redis_url = os.getenv("REDIS_URL")
        raw_failure_mode = os.getenv("REDIS_FAILURE_MODE", "closed")
        if raw_failure_mode not in ("closed", "local"):
            raise ValueError("REDIS_FAILURE_MODE doit valoir closed ou local")

        return cls(
            frontend_origins=_items("FRONTEND_ORIGINS", ("http://localhost:5173",)),
            redis_url=redis_url.strip() if redis_url and redis_url.strip() else None,
            redis_key_prefix=os.getenv("REDIS_KEY_PREFIX", "mouvair").strip(": "),
            redis_failure_mode=raw_failure_mode,
            redis_socket_timeout_seconds=_float("REDIS_SOCKET_TIMEOUT_SECONDS", 0.5),
            redis_connect_timeout_seconds=_float("REDIS_CONNECT_TIMEOUT_SECONDS", 0.5),
            redis_retry_delay_seconds=_float("REDIS_RETRY_DELAY_SECONDS", 5.0),
            redis_max_connections=_integer("REDIS_MAX_CONNECTIONS", 20),
            open_meteo_quota_scope=os.getenv(
                "OPEN_METEO_QUOTA_SCOPE", "free-api"
            ).strip(": "),
            open_meteo_quota_minute=_integer("OPEN_METEO_QUOTA_MINUTE", 540),
            open_meteo_quota_hour=_integer("OPEN_METEO_QUOTA_HOUR", 4_500),
            open_meteo_quota_day=_integer("OPEN_METEO_QUOTA_DAY", 9_000),
            open_meteo_quota_month=_integer("OPEN_METEO_QUOTA_MONTH", 270_000),
            geocoding_cache_ttl_seconds=_integer("GEOCODING_CACHE_TTL_SECONDS", 86_400),
            geocoding_negative_cache_ttl_seconds=_integer(
                "GEOCODING_NEGATIVE_CACHE_TTL_SECONDS", 300
            ),
            geocoding_local_cache_max_entries=_integer(
                "GEOCODING_LOCAL_CACHE_MAX_ENTRIES", 512
            ),
            geocoding_lock_ttl_seconds=_integer("GEOCODING_LOCK_TTL_SECONDS", 12),
            geocoding_lock_wait_seconds=_float("GEOCODING_LOCK_WAIT_SECONDS", 4.0),
            forecast_shared_cache_ttl_seconds=_integer(
                "FORECAST_SHARED_CACHE_TTL_SECONDS", 86_400
            ),
            forecast_lock_ttl_seconds=_integer("FORECAST_LOCK_TTL_SECONDS", 20),
            forecast_lock_wait_seconds=_float("FORECAST_LOCK_WAIT_SECONDS", 6.0),
            location_rate_limit_minute=_integer("LOCATION_RATE_LIMIT_MINUTE", 30),
            location_rate_limit_hour=_integer("LOCATION_RATE_LIMIT_HOUR", 300),
            forecast_rate_limit_minute=_integer("FORECAST_RATE_LIMIT_MINUTE", 10),
            forecast_rate_limit_hour=_integer("FORECAST_RATE_LIMIT_HOUR", 60),
            client_id_hmac_secret=os.getenv(
                "CLIENT_ID_HMAC_SECRET", "mouvair-local-development"
            ),
            trusted_proxy_cidrs=_items("TRUSTED_PROXY_CIDRS", ()),
        )
