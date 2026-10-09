from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from triaxis_common.config import (
    HardwareProfile,
    PoolRole,
    ResilienceConfig,
    ResilienceMode,
    ServiceSettings,
    TenantConfig,
    is_in_cluster_host,
    load_hardware_profiles,
    load_tenant_config,
    load_tenants,
    require_in_cluster_url,
)
from triaxis_common.types import Environment, Workload

REPO = Path(__file__).resolve().parents[3]
EXAMPLE_TENANT = REPO / "config/tenants/example-tenant.yaml"


def _tenant_dict() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(EXAMPLE_TENANT.read_text("utf-8"))
    return data


# --------------------------------------------------------------------- tenants


def test_example_tenant_loads() -> None:
    tenant = load_tenant_config(EXAMPLE_TENANT)
    assert tenant.id == "example"
    assert tenant.brand("brand-a") is not None
    assert tenant.brand("missing") is None
    quota = tenant.quota("brand-a", Workload.LLM)
    assert quota is not None
    assert quota.tokens_per_minute == 400000
    assert tenant.quota("brand-b", Workload.STT) is None
    assert tenant.quota("missing", Workload.STT) is None
    assert tenant.models_for("brand-b").llm_chat == "qwen3.5-27b"
    with pytest.raises(KeyError):
        tenant.models_for("missing")


def test_brand_model_override() -> None:
    data = _tenant_dict()
    data["brands"][1]["models"] = {**data["models"], "llm_chat": "allam-7b"}
    tenant = TenantConfig.model_validate(data)
    assert tenant.models_for("brand-b").llm_chat == "allam-7b"
    assert tenant.models_for("brand-a").llm_chat == "qwen3.5-27b"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(unknown_field=1),
        lambda d: d.update(id="Example Motors"),
        lambda d: d.update(data_residency="sa"),
        lambda d: d.update(brands=[]),
        lambda d: d["brands"].append(dict(d["brands"][0])),  # duplicate brand id
        lambda d: d["brands"][0]["quotas"].pop("stt"),  # quota missing for a workload
        lambda d: d["brands"][1]["quotas"].update(
            stt={"requests_per_minute": 1, "concurrent_streams": 1}
        ),  # quota for a workload that is not enabled
        lambda d: d["brands"][0].update(workloads=[]),
        lambda d: d["brands"][0].update(workloads=["admin"]),
        lambda d: d["brands"][0]["quotas"]["stt"].update(requests_per_minute=0),
        lambda d: d["models"].update(stt="Whisper Large"),
        lambda d: d.update(schema_version=2),
        lambda d: d.update(batch_deadline={"kind": "hours"}),
        lambda d: d.update(batch_deadline={"kind": "same_day", "hours": 3}),
    ],
)
def test_tenant_rejects_invalid(mutate: Any) -> None:
    data = _tenant_dict()
    mutate(data)
    with pytest.raises(ValidationError):
        TenantConfig.model_validate(data)


def test_batch_deadline_hours() -> None:
    data = _tenant_dict()
    data["batch_deadline"] = {"kind": "hours", "hours": 6}
    assert TenantConfig.model_validate(data).batch_deadline.hours == 6


def test_load_tenants_directory(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(EXAMPLE_TENANT.read_text("utf-8"), "utf-8")
    assert set(load_tenants(tmp_path)) == {"example"}
    (tmp_path / "b.yaml").write_text(EXAMPLE_TENANT.read_text("utf-8"), "utf-8")
    with pytest.raises(ValueError, match="duplicate tenant"):
        load_tenants(tmp_path)


def test_yaml_is_safe_loaded(tmp_path: Path) -> None:
    path = tmp_path / "evil.yaml"
    path.write_text("!!python/object/apply:os.system ['echo pwned']\n", "utf-8")
    with pytest.raises(yaml.constructor.ConstructorError):
        load_tenant_config(path)


# --------------------------------------------------------------------- hardware


def test_repo_hardware_profiles_load() -> None:
    profiles = load_hardware_profiles(REPO / "config/hardware-profiles")
    assert set(profiles) == {"h100-llm", "h100-mig-3g40gb-speech", "l40s-speech", "cpu-dev"}
    assert profiles["h100-mig-3g40gb-speech"].mig_profile == "3g.40gb"
    assert all(p.cell_scale_up_threshold_pct == 60 for p in profiles.values())


def test_hardware_profile_id_must_match_file(tmp_path: Path) -> None:
    (tmp_path / "other.yaml").write_text("id: cpu-dev\nrole: dev\ngpus_per_server: 0\n", "utf-8")
    with pytest.raises(ValueError, match="file name"):
        load_hardware_profiles(tmp_path)


@pytest.mark.parametrize(
    "data",
    [
        {"id": "x", "role": "llm", "gpus_per_server": 0},
        {
            "id": "x",
            "role": "llm",
            "gpu_model": "H100",
            "gpu_memory_gb": 80,
            "gpus_per_server": 8,
            "mig_profile": "3g.40gb",
        },
        {
            "id": "x",
            "role": "stt_tts",
            "gpu_model": "H100",
            "gpu_memory_gb": 80,
            "gpus_per_server": 8,
            "mig_profile": "big",
        },
        {"id": "x", "role": "dev", "gpus_per_server": 0, "cell_scale_up_threshold_pct": 0},
    ],
)
def test_hardware_profile_rejects_invalid(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        HardwareProfile.model_validate(data)


def test_hardware_profile_dev_needs_no_gpu() -> None:
    assert HardwareProfile(id="x", role=PoolRole.DEV, gpus_per_server=0).gpu_model is None


# --------------------------------------------------------------------- resilience


def test_resilience_defaults_to_option_b() -> None:
    config = ResilienceConfig()
    assert config.mode is ResilienceMode.BALANCED
    assert config.queues_batch_during_outage


def test_resilience_mirror() -> None:
    config = ResilienceConfig(mode=ResilienceMode.MIRROR, batch_capacity_during_outage_pct=100)
    assert not config.queues_batch_during_outage


@pytest.mark.parametrize(
    "kwargs",
    [
        {"realtime_capacity_per_site_pct": 80},
        {"mode": ResilienceMode.MIRROR, "batch_capacity_during_outage_pct": 50},
        {"mode": ResilienceMode.BALANCED, "batch_capacity_during_outage_pct": 100},
        {"mode": "active-passive"},
    ],
)
def test_resilience_rejects_invalid(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ResilienceConfig.model_validate(kwargs)


# --------------------------------------------------------------------- residency


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "keycloak",
        "keycloak.iam.svc",
        "keycloak.iam.svc.cluster.local",
        "otel.observability.svc.cluster.local",
        "10.0.0.5",
        "192.168.1.1",
        "127.0.0.1",
        "[::1]",
        "fd00::1",
        "bao.control.internal",
    ],
)
def test_in_cluster_hosts(host: str) -> None:
    assert is_in_cluster_host(host)


@pytest.mark.parametrize(
    "host",
    ["", "api.openai.com", "8.8.8.8", "keycloak.example.com", "svc.evil.com", "2001:4860::1"],
)
def test_out_of_cluster_hosts(host: str) -> None:
    assert not is_in_cluster_host(host)


def test_require_in_cluster_url() -> None:
    assert require_in_cluster_url("https://keycloak.iam.svc/x", allow_http=False)
    with pytest.raises(ValueError, match="scheme"):
        require_in_cluster_url("ftp://keycloak/x", allow_http=True)


# --------------------------------------------------------------------- settings


def _settings_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "TRIAXIS_SERVICE_NAME": "stt",
        "TRIAXIS_OIDC_ISSUER": "https://keycloak.iam.svc/realms/triaxis",
        "TRIAXIS_JWKS_URL": "https://keycloak.iam.svc/realms/triaxis/protocol/openid-connect/certs",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(monkeypatch, TRIAXIS_OTLP_ENDPOINT="http://otel-collector:4318/v1/traces")
    settings = ServiceSettings()
    assert settings.service_name == "stt"
    assert settings.environment is Environment.DEV
    assert settings.token_max_lifetime_s == 900


def test_settings_reject_long_token_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(monkeypatch, TRIAXIS_TOKEN_MAX_LIFETIME_S="3600")
    with pytest.raises(ValidationError):
        ServiceSettings()


def test_settings_reject_public_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(monkeypatch, TRIAXIS_OTLP_ENDPOINT="https://otel.saas-vendor.com")
    with pytest.raises(ValidationError):
        ServiceSettings()


def test_settings_prod_requires_https_and_site(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(
        monkeypatch,
        TRIAXIS_ENVIRONMENT="prod",
        TRIAXIS_OIDC_ISSUER="http://keycloak.iam.svc/realms/triaxis",
        TRIAXIS_SITE="riyadh",
    )
    with pytest.raises(ValidationError):
        ServiceSettings()
    _settings_env(monkeypatch, TRIAXIS_ENVIRONMENT="prod")
    monkeypatch.delenv("TRIAXIS_SITE")
    with pytest.raises(ValidationError):
        ServiceSettings()
    _settings_env(monkeypatch, TRIAXIS_ENVIRONMENT="prod", TRIAXIS_SITE="riyadh")
    assert ServiceSettings().site == "riyadh"


def test_settings_reject_unknown_env_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings_env(monkeypatch)
    with pytest.raises(ValidationError):
        ServiceSettings(unknown_option=True)  # type: ignore[call-arg]
