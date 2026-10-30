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
    # `metrics` is shared with Alloy only, so Docker's DNS can't hand Caddy the app's address on
    # another network, from which Uvicorn would not trust X-Forwarded-* (#273).
    assert sorted(compose["services"]["caddy"]["networks"]) == ["edge", "metrics"]


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
    assert sorted(alloy["networks"]) == ["backend", "egress", "metrics", "telemetry"]
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


def test_the_app_reports_production_and_the_release_tag(services: dict[str, Any]) -> None:
    """#293: `deployment.environment` and `service.version` on every span (ADR-0030). Set here,
    not in `.env`, so `.env.example`'s development values can't leak into production. The tag
    comes from RELEASE_TAG, not APP_VERSION: Compose interpolates from the project directory's
    `.env`, which is the app's own and may hold `APP_VERSION=dev`."""
    environment = services["app"]["environment"]
    assert environment["DEPLOYMENT_ENVIRONMENT"] == "production"
    assert environment["APP_VERSION"] == "${RELEASE_TAG:-unknown}"


def test_the_release_tag_is_never_a_settings_key() -> None:
    """Were it one, the app's `.env` could supply the tag through Compose's interpolation."""
    assert "RELEASE_TAG" not in (ROOT / ".env.example").read_text()


@pytest.mark.parametrize(
    "key",
    [
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
    ],
)
@pytest.mark.parametrize("name", ["migrate", "app"])
def test_no_otlp_header_from_env_reaches_the_app(
    services: dict[str, Any], name: str, key: str
) -> None:
    """ADR-0030 invariant 1: `environment` overrides `env_file`, so a Grafana credential left in
    `.env` never reaches either process that reads it. The OTel SDK reads the per-signal
    variables itself, whatever headers the app passes."""
    assert services[name]["environment"][key] == ""


def test_nothing_waits_for_alloy(services: dict[str, Any]) -> None:
    """An Alloy that fails to start never holds up the database, a migration or the app."""
    for svc in services.values():
        assert "alloy" not in svc.get("depends_on", {})


# ADR-0030 (#273): Caddy's metrics only on the `metrics` network, never through 443.


def test_only_caddy_and_alloy_share_the_internal_metrics_network(compose: dict[str, Any]) -> None:
    assert compose["networks"]["metrics"]["internal"] is True
    members = sorted(
        name for name, svc in compose["services"].items() if "metrics" in svc["networks"]
    )
    assert members == ["alloy", "caddy"]
    subnet = ipaddress.ip_network(compose["networks"]["metrics"]["ipam"]["config"][0]["subnet"])
    edge = ipaddress.ip_network(compose["networks"]["edge"]["ipam"]["config"][0]["subnet"])
    assert subnet.is_private
    assert not subnet.overlaps(edge)
    for name in ("caddy", "alloy"):
        address = compose["services"][name]["networks"]["metrics"]["ipv4_address"]
        assert ipaddress.ip_address(address) in subnet
        # Not the bridge's gateway, which routed traffic from other networks arrives from.
        assert ipaddress.ip_address(address) != subnet.network_address + 1


def _caddy_sites() -> dict[str, str]:
    """Each top-level site block of the Caddyfile, by its address."""
    text = (ROOT / "deploy" / "Caddyfile").read_text()
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    sites: dict[str, str] = {}
    address: str | None = None
    body: list[str] = []
    for line in lines:
        if address is None and line.endswith("{") and not line.startswith(("\t", " ")):
            address, body = line[:-1].strip(), []
        elif address is not None and line == "}":
            sites[address] = "\n".join(body)
            address = None
        elif address is not None:
            body.append(line)
    return sites


def test_metrics_are_served_only_on_caddys_metrics_address(compose: dict[str, Any]) -> None:
    caddy = compose["services"]["caddy"]["networks"]["metrics"]["ipv4_address"]
    sites = _caddy_sites()
    metrics_site = f"http://{caddy}:9180"
    assert sorted(sites) == sorted(["", "https://{$SITE_HOST}", metrics_site])
    alloy = compose["services"]["alloy"]["networks"]["metrics"]["ipv4_address"]
    # Alloy's address alone gets the metrics; anyone else a host routes in gets 403.
    assert sites[metrics_site].split() == [
        "bind",
        caddy,
        "@alloy",
        "remote_ip",
        alloy,
        "handle",
        "@alloy",
        "{",
        "metrics",
        "}",
        "handle",
        "{",
        "respond",
        "403",
        "}",
    ]
    assert "metrics" not in sites["https://{$SITE_HOST}"]
    # Global options: metrics collection on. The admin API stays on its default, localhost.
    global_options = sites[""]
    assert "\n\tmetrics" in global_options
    assert "admin" not in global_options
    # Fixed server names, so the `server` label never depends on the order of the sites.
    assert re.search(r"servers :443 \{\s*name public\b", global_options)
    assert re.search(rf"servers {re.escape(caddy)}:9180 \{{\s*name metrics\b", global_options)
    assert global_options.count("protocols h1 h2") == 2


def test_caddys_metrics_port_is_not_published(services: dict[str, Any]) -> None:
    assert all("9180" not in port for port in services["caddy"]["ports"])
    assert "expose" not in services["caddy"]


def test_alloy_mounts_the_host_read_only_for_its_metrics(services: dict[str, Any]) -> None:
    volumes = services["alloy"]["volumes"]
    host = [v for v in volumes if v.split(":")[0] in ("/proc", "/sys", "/")]
    assert len(host) == 3
    for volume in host:
        assert volume.split(":")[2].split(",")[0] == "ro", volume
