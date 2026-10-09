"""Strict configuration models.

- :class:`ServiceSettings`: per-process settings from ``TRIAXIS_*`` environment
  variables (never secrets — those come from OpenBao, ADR 0017).
- :class:`TenantConfig`: everything needed to onboard a tenant without a code
  change (brands, workloads, quotas, guardrail policy, model selection).
- :class:`HardwareProfile` (ADR 0013) and :class:`ResilienceConfig` (ADR 0004).

All models reject unknown fields. YAML is parsed with ``yaml.safe_load`` only.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from triaxis_common.types import Environment, ModelId, Slug, Workload

MAX_TOKEN_LIFETIME_S = 900
"""Contract: access tokens live at most 15 minutes (ADR 0008)."""

CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]

_IN_CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local", ".cluster.local", ".internal")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def is_in_cluster_host(host: str) -> bool:
    """True for hosts that cannot be outside the cluster/private network.

    Accepted: ``localhost``, private/loopback/link-local IPs, single-label
    service names and names under cluster-internal suffixes. Used to enforce
    data residency (rule 3) on every configured endpoint.
    """
    host = host.strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return ip.is_private or ip.is_loopback or ip.is_link_local
    if "." not in host:
        return True  # Kubernetes short service name / compose service name
    return host.endswith(_IN_CLUSTER_SUFFIXES)


def require_in_cluster_url(url: str, *, allow_http: bool) -> str:
    parts = urlsplit(url)
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in allowed_schemes:
        raise ValueError(f"URL scheme must be one of {sorted(allowed_schemes)}")
    if parts.username or parts.password:
        raise ValueError("URL must not embed credentials")
    if not parts.hostname or not is_in_cluster_host(parts.hostname):
        raise ValueError("URL must point inside the cluster (data residency, rule 3)")
    return url


# --------------------------------------------------------------------- resilience


class ResilienceMode(StrEnum):
    BALANCED = "balanced"  # Option B
    MIRROR = "mirror"  # Option A


class ResilienceConfig(_Strict):
    """ADR 0004: each site carries 100% of real-time; batch queues or mirrors."""

    mode: ResilienceMode = ResilienceMode.BALANCED
    realtime_capacity_per_site_pct: Annotated[int, Field(ge=0, le=100)] = 100
    batch_capacity_during_outage_pct: Annotated[int, Field(ge=0, le=100)] = 0

    @model_validator(mode="after")
    def _check_mode(self) -> Self:
        if self.realtime_capacity_per_site_pct != 100:
            raise ValueError("each site must carry 100% of real-time traffic alone")
        if self.mode is ResilienceMode.MIRROR and self.batch_capacity_during_outage_pct != 100:
            raise ValueError("mirror mode (Option A) requires 100% batch capacity per site")
        if self.mode is ResilienceMode.BALANCED and self.batch_capacity_during_outage_pct == 100:
            raise ValueError("100% batch capacity per site is mirror mode; set mode: mirror")
        return self

    @property
    def queues_batch_during_outage(self) -> bool:
        return self.mode is ResilienceMode.BALANCED


# --------------------------------------------------------------------- hardware


class PoolRole(StrEnum):
    LLM = "llm"
    STT_TTS = "stt_tts"
    DEV = "dev"


class HardwareProfile(_Strict):
    """ADR 0013. One file per profile in ``config/hardware-profiles/``."""

    id: Slug
    role: PoolRole
    gpu_model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9 ._-]{1,40}$")
    gpu_memory_gb: PositiveInt | None = None
    gpus_per_server: Annotated[int, Field(ge=0, le=16)]
    mig_profile: str | None = Field(default=None, pattern=r"^[0-9]g\.[0-9]+gb$")
    cell_scale_up_threshold_pct: Annotated[int, Field(ge=1, le=100)] = 60

    @model_validator(mode="after")
    def _check_gpu(self) -> Self:
        if self.role is not PoolRole.DEV and (
            self.gpus_per_server == 0 or self.gpu_model is None or self.gpu_memory_gb is None
        ):
            raise ValueError("GPU pools need gpu_model, gpu_memory_gb and gpus_per_server > 0")
        if self.mig_profile is not None and self.role is PoolRole.LLM:
            raise ValueError("MIG slicing is only used for the STT/TTS pool")
        return self


# --------------------------------------------------------------------- tenants


class Quota(_Strict):
    """Per (tenant, brand, workload) limits. Brands never share a quota."""

    requests_per_minute: PositiveInt
    concurrent_streams: PositiveInt
    tokens_per_minute: PositiveInt | None = None
    audio_seconds_per_hour: PositiveInt | None = None
    batch_jobs_per_day: PositiveInt | None = None


class ModelSelection(_Strict):
    """Registry model IDs per profile (ADR 0010)."""

    stt: ModelId
    tts: ModelId
    llm_voice: ModelId
    llm_chat: ModelId


class BatchDeadline(_Strict):
    """ADR 0014. ``same_day`` or a number of hours."""

    kind: Literal["same_day", "hours"] = "same_day"
    hours: Annotated[int, Field(ge=1, le=24)] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.kind == "hours") != (self.hours is not None):
            raise ValueError("hours is required exactly when kind is 'hours'")
        return self


class BrandConfig(_Strict):
    id: Slug
    display_name: Annotated[str, Field(min_length=1, max_length=80)]
    workloads: frozenset[Workload]
    quotas: dict[Workload, Quota]
    guardrail_policy: Slug
    models: ModelSelection | None = None  # overrides the tenant default

    @model_validator(mode="after")
    def _check_quotas(self) -> Self:
        if not self.workloads:
            raise ValueError("a brand needs at least one workload")
        missing = self.workloads - self.quotas.keys()
        extra = self.quotas.keys() - self.workloads
        if missing:
            raise ValueError(f"quota missing for workloads: {sorted(missing)}")
        if extra:
            raise ValueError(f"quota given for workloads not enabled: {sorted(extra)}")
        return self


class TenantConfig(_Strict):
    """Everything needed to onboard a tenant with configuration only."""

    schema_version: Literal[1] = 1
    id: Slug
    display_name: Annotated[str, Field(min_length=1, max_length=80)]
    data_residency: CountryCode
    approved_origin_countries: frozenset[CountryCode]
    models: ModelSelection
    brands: Annotated[list[BrandConfig], Field(min_length=1)]
    batch_deadline: BatchDeadline = BatchDeadline()

    @field_validator("brands")
    @classmethod
    def _unique_brands(cls, brands: list[BrandConfig]) -> list[BrandConfig]:
        ids = [b.id for b in brands]
        if len(ids) != len(set(ids)):
            raise ValueError("brand ids must be unique within a tenant")
        return brands

    def brand(self, brand_id: str) -> BrandConfig | None:
        return next((b for b in self.brands if b.id == brand_id), None)

    def models_for(self, brand_id: str) -> ModelSelection:
        brand = self.brand(brand_id)
        if brand is None:
            raise KeyError(brand_id)
        return brand.models or self.models

    def quota(self, brand_id: str, workload: Workload) -> Quota | None:
        brand = self.brand(brand_id)
        return None if brand is None else brand.quotas.get(workload)


# --------------------------------------------------------------------- settings


class ServiceSettings(BaseSettings):
    """Process settings from ``TRIAXIS_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="TRIAXIS_", extra="forbid", frozen=True)

    service_name: Slug
    environment: Environment = Environment.DEV
    site: Slug | None = None
    log_strict: bool = False
    tmpdir: Path = Path("/tmp/triaxis")  # noqa: S108 - mounted as tmpfs; verified at start-up
    oidc_issuer: str
    oidc_audience: Annotated[str, Field(min_length=1, max_length=64)] = "triaxis"
    jwks_url: str
    otlp_endpoint: str | None = None
    token_max_lifetime_s: Annotated[int, Field(ge=60, le=MAX_TOKEN_LIFETIME_S)] = (
        MAX_TOKEN_LIFETIME_S
    )
    token_leeway_s: Annotated[int, Field(ge=0, le=30)] = 30
    request_timeout_s: PositiveFloat = 30.0
    tenants_dir: Path = Path("/etc/triaxis/tenants")

    @model_validator(mode="after")
    def _check_endpoints(self) -> Self:
        allow_http = not self.environment.is_production_like
        require_in_cluster_url(self.oidc_issuer, allow_http=allow_http)
        require_in_cluster_url(self.jwks_url, allow_http=allow_http)
        if self.otlp_endpoint is not None:
            require_in_cluster_url(self.otlp_endpoint, allow_http=True)
        if self.environment.is_production_like and self.site is None:
            raise ValueError("site is required in staging and prod")
        return self


# --------------------------------------------------------------------- loading


def _load_yaml(path: Path) -> Any:
    with path.open("rb") as fh:
        return yaml.safe_load(fh)


def load_tenant_config(path: Path) -> TenantConfig:
    return TenantConfig.model_validate(_load_yaml(path))


def load_tenants(directory: Path) -> dict[str, TenantConfig]:
    tenants: dict[str, TenantConfig] = {}
    for path in sorted(directory.glob("*.yaml")):
        tenant = load_tenant_config(path)
        if tenant.id in tenants:
            raise ValueError(f"duplicate tenant id in {path.name}")
        tenants[tenant.id] = tenant
    return tenants


def load_hardware_profiles(directory: Path) -> dict[str, HardwareProfile]:
    profiles: dict[str, HardwareProfile] = {}
    for path in sorted(directory.glob("*.yaml")):
        profile = HardwareProfile.model_validate(_load_yaml(path))
        if profile.id != path.stem:
            raise ValueError(f"{path.name}: id must match the file name")
        profiles[profile.id] = profile
    return profiles
