"""`compose.prod.yaml` keeps ADR-0023's invariants (ticket #221): only Caddy's 443 is published,
images come by digest, `migrate` finishes before `app`, and every service is hardened."""

import ipaddress
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("db", "migrate", "app", "caddy", "alloy")
LONG_RUNNING = ("db", "app", "caddy", "alloy")


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


def test_runs_exactly_the_five_services(services: dict[str, Any]) -> None:
    assert sorted(services) == sorted(SERVICES)


def test_only_caddy_publishes_a_port(services: dict[str, Any]) -> None:
    published = {name: svc["ports"] for name, svc in services.items() if "ports" in svc}
    assert published == {"caddy": ["443:443"]}


def test_nothing_is_built_on_the_server(services: dict[str, Any]) -> None:
    assert all("build" not in svc for svc in services.values())


@pytest.mark.parametrize("name", ["db", "caddy", "alloy"])
def test_third_party_images_are_pinned_by_digest(services: dict[str, Any], name: str) -> None:
    image = services[name]["image"]
    assert re.fullmatch(r"[a-z0-9./-]+:[\w.-]+@sha256:[0-9a-f]{64}", image), image
    # A literal: no variable in an env file can swap it.
    assert "$" not in image


def test_every_service_and_network_is_written_out_here(compose: dict[str, Any]) -> None:
    """These tests read this file only, so nothing may add a service, a port or a network to
    it from elsewhere: `include` and `extends` pull in other files, and a network's `name`
    or `external` can make `egress` the real `edge` (ADR-0030 invariant 2)."""
    assert "include" not in compose
    for name, svc in compose["services"].items():
        assert "extends" not in svc, name
    for name, network in compose["networks"].items():
        assert not {"name", "external"} & set(network or {}), name


@pytest.mark.parametrize("name", SERVICES)
def test_no_service_shares_the_hosts_namespaces_or_runs_privileged(
    services: dict[str, Any], name: str
) -> None:
    svc = services[name]
    assert not svc.get("privileged", False)
    for key in ("network_mode", "pid", "ipc", "userns_mode"):
        assert key not in svc, key


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
    assert all("cap_add" not in services[name] for name in ("migrate", "app", "alloy"))


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
    assert "max_size 256KiB" in caddyfile
    # Only 443/tcp is published, so HTTP/3 must not be advertised.
    assert "protocols h1 h2" in caddyfile


def test_alloy_is_never_on_edge_and_publishes_nothing(compose: dict[str, Any]) -> None:
    """ADR-0030 invariant 2: the edge subnet is trusted for X-Forwarded-*, because Caddy is its
    only other member, and Alloy listens for the app only."""
    alloy = compose["services"]["alloy"]
    assert sorted(alloy["networks"]) == ["egress", "telemetry"]
    assert "ports" not in alloy
    assert "expose" not in alloy
    assert "network_mode" not in alloy


def test_only_the_app_and_alloy_share_the_internal_telemetry_network(
    compose: dict[str, Any],
) -> None:
    assert compose["networks"]["telemetry"]["internal"] is True
    members = sorted(
        name for name, svc in compose["services"].items() if "telemetry" in svc["networks"]
    )
    assert members == ["alloy", "app"]
    assert "internal" not in compose["networks"]["egress"]
    assert [n for n, s in compose["services"].items() if "egress" in s["networks"]] == ["alloy"]


def test_alloy_reads_its_config_and_the_docker_socket_read_only(services: dict[str, Any]) -> None:
    volumes = services["alloy"]["volumes"]
    assert "./deploy/alloy/config.alloy:/etc/alloy/config.alloy:ro" in volumes
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in volumes
    sockets = [v for v in volumes if "docker.sock" in v]
    assert sockets == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    assert services["alloy"]["command"][:2] == ["run", "/etc/alloy/config.alloy"]


def test_alloy_stores_its_positions_where_root_may_write(services: dict[str, Any]) -> None:
    """With every capability dropped, root can't enter the image's /var/lib/alloy (its `alloy`
    user's, mode 770), and Alloy would crash-loop on start."""
    alloy = services["alloy"]
    (storage,) = [a for a in alloy["command"] if a.startswith("--storage.path=")]
    path = storage.split("=", 1)[1]
    assert not path.startswith("/var/lib/alloy")
    assert f"alloy_data:{path}" in alloy["volumes"]


def test_alloy_alone_reads_the_grafana_cloud_env_file_and_may_start_without_it(
    services: dict[str, Any],
) -> None:
    """ADR-0030 invariants 1 and 5: only Alloy holds the credentials, and a missing file leaves
    telemetry off rather than failing a deploy."""
    assert services["alloy"]["env_file"] == [
        {"path": "${CONFIG_DIR:-.}/alloy.env", "required": False}
    ]
    for name, svc in services.items():
        if name != "alloy":
            assert "alloy.env" not in str(svc.get("env_file", []))


def test_the_app_exports_its_telemetry_to_alloy(services: dict[str, Any]) -> None:
    environment = services["app"]["environment"]
    assert environment["OTEL_ENABLED"] == "true"
    assert environment["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://alloy:4318"


def test_nothing_waits_for_alloy(services: dict[str, Any]) -> None:
    """An Alloy that fails to start never holds up the database, a migration or the app."""
    for svc in services.values():
        assert "alloy" not in svc.get("depends_on", {})
