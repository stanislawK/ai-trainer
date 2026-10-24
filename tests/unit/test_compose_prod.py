"""`compose.prod.yaml` keeps ADR-0023's invariants (ticket #221): only Caddy's 443 is published,
images come by digest, `migrate` finishes before `app`, and every service is hardened."""

import ipaddress
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("db", "migrate", "app", "caddy")
LONG_RUNNING = ("db", "app", "caddy")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "compose.prod.yaml").read_text())
    return loaded


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = compose["services"]
    return found


def test_has_its_own_project_name(compose: dict[str, Any]) -> None:
    """Without it the stack shares the `ai-trainer` project with `make start` and replaces its
    containers."""
    assert compose["name"] == "ai-trainer-prod"


def test_runs_exactly_the_four_services(services: dict[str, Any]) -> None:
    assert sorted(services) == sorted(SERVICES)


def test_only_caddy_publishes_a_port(services: dict[str, Any]) -> None:
    published = {name: svc["ports"] for name, svc in services.items() if "ports" in svc}
    assert published == {"caddy": ["443:443"]}


def test_nothing_is_built_on_the_server(services: dict[str, Any]) -> None:
    assert all("build" not in svc for svc in services.values())


@pytest.mark.parametrize("name", ["db", "caddy"])
def test_third_party_images_are_pinned_by_digest(services: dict[str, Any], name: str) -> None:
    assert "@sha256:" in services[name]["image"]


@pytest.mark.parametrize("name", ["migrate", "app"])
def test_app_image_comes_from_the_environment(services: dict[str, Any], name: str) -> None:
    assert services[name]["image"].startswith("${APP_IMAGE:?")


def test_migrate_upgrades_to_head_once(services: dict[str, Any]) -> None:
    migrate = services["migrate"]
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate["restart"] == "no"
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"


def test_app_starts_only_after_migrate_succeeded(services: dict[str, Any]) -> None:
    assert services["app"]["depends_on"]["migrate"] == {
        "condition": "service_completed_successfully"
    }


@pytest.mark.parametrize("name", LONG_RUNNING)
def test_long_running_services_restart_unless_stopped(services: dict[str, Any], name: str) -> None:
    assert services[name]["restart"] == "unless-stopped"


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_is_hardened(services: dict[str, Any], name: str) -> None:
    svc = services[name]
    assert svc["deploy"]["resources"]["limits"]["memory"]
    assert svc["logging"]["driver"] == "json-file"
    assert {"max-size", "max-file"} <= set(svc["logging"]["options"])
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["cap_drop"] == ["ALL"]


def test_only_caddy_may_bind_a_privileged_port(services: dict[str, Any]) -> None:
    assert services["caddy"]["cap_add"] == ["NET_BIND_SERVICE"]
    assert "NET_BIND_SERVICE" not in services["db"].get("cap_add", [])
    assert all("cap_add" not in services[name] for name in ("migrate", "app"))


def test_database_password_comes_from_its_own_env_file(services: dict[str, Any]) -> None:
    """The password stays out of `.env`'s keys, so the `.env.example`↔`Settings` parity test
    stays strict (ADR-0023)."""
    db = services["db"]
    assert db["env_file"] == ["${CONFIG_DIR:-.}/db.env"]
    assert "POSTGRES_PASSWORD" not in db.get("environment", {})


def test_database_is_reachable_only_on_the_internal_network(compose: dict[str, Any]) -> None:
    assert compose["services"]["db"]["networks"] == ["backend"]
    assert compose["networks"]["backend"]["internal"] is True


def test_app_trusts_proxy_headers_only_from_the_edge_subnet(compose: dict[str, Any]) -> None:
    subnet = compose["networks"]["edge"]["ipam"]["config"][0]["subnet"]
    allowed = compose["services"]["app"]["environment"]["FORWARDED_ALLOW_IPS"]
    assert allowed == subnet
    assert ipaddress.ip_network(subnet).is_private
    assert sorted(compose["services"]["caddy"]["networks"]) == ["edge"]


def test_caddy_mounts_the_caddyfile_and_the_certificates(services: dict[str, Any]) -> None:
    volumes = services["caddy"]["volumes"]
    assert "./deploy/Caddyfile:/etc/caddy/Caddyfile:ro" in volumes
    assert "${CONFIG_DIR:-.}/certs:/etc/caddy/certs:ro" in volumes


def test_caddyfile_requires_the_aop_client_certificate_and_sets_hsts() -> None:
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text()
    assert "tls /etc/caddy/certs/origin.pem /etc/caddy/certs/origin-key.pem" in caddyfile
    assert "trust_pool file /etc/caddy/certs/aop-ca.pem" in caddyfile
    # The only mode line. A second one, or a weaker mode, must not slip in beside it.
    mode_lines = [
        line.strip() for line in caddyfile.splitlines() if line.strip().startswith("mode ")
    ]
    assert mode_lines == ["mode require_and_verify"]
    assert 'Strict-Transport-Security "max-age=31536000"' in caddyfile
    assert "reverse_proxy app:8000" in caddyfile
    # Only 443/tcp is published, so HTTP/3 must not be advertised.
    assert "protocols h1 h2" in caddyfile
