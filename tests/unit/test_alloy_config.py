"""`deploy/alloy/config.alloy` keeps ADR-0030's relay rules (ticket #272): the app's OTLP and
this stack's container logs go to Grafana Cloud, labelled by compose service only, with
email-shaped values masked, and every credential read from `alloy.env`.

`make alloy-check` runs Alloy's own `fmt` and `validate` on the file; these tests pin what the
validator can't know."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = (ROOT / "deploy/alloy/config.alloy").read_text()
ENV_EXAMPLE = ROOT / "deploy/alloy.env.example"


def _alloy_string(name: str, text: str = CONFIG) -> list[str]:
    """Every `<name> = "<value>"` in the config, decoded like an Alloy (Go) string literal."""
    return [
        json.loads(literal) for literal in re.findall(rf'\b{name}\s*=\s*("(?:[^"\\]|\\.)*")', text)
    ]


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "compose.prod.yaml").read_text())
    return loaded


def test_reads_only_this_stacks_containers(compose: dict[str, Any]) -> None:
    """Anything else on the host, the `make start` stack on a laptop included, stays out."""
    assert f'"com.docker.compose.project={compose["name"]}"' in CONFIG


def test_logs_are_labelled_by_compose_service_and_nothing_else() -> None:
    """ADR-0030 invariant 4: bounded labels only; request and user IDs stay in the line."""
    assert _alloy_string("target_label") == ["service"]
    (source,) = re.findall(r"source_labels\s*=\s*\[([^\]]*)\]", CONFIG)
    assert json.loads(f"[{source}]") == ["__meta_docker_container_label_com_docker_compose_service"]
    for unbounded in ("request_id", "user_id", "trace_id", "span_id"):
        assert unbounded not in CONFIG


def test_the_otlp_receiver_listens_where_the_app_sends(compose: dict[str, Any]) -> None:
    endpoint = compose["services"]["app"]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert endpoint.endswith(":4318")
    assert _alloy_string("endpoint", CONFIG.split("otelcol.receiver.otlp", 1)[1])[0] == (
        "0.0.0.0:4318"
    )


def test_every_credential_comes_from_alloy_env() -> None:
    used = set(re.findall(r'sys\.env\("([A-Z0-9_]+)"\)', CONFIG))
    listed = {
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert used == listed
    assert used >= {"GRAFANA_CLOUD_TOKEN", "GRAFANA_CLOUD_OTLP_URL", "GRAFANA_CLOUD_LOGS_URL"}
    # No URL, user or token is written into the file itself.
    assert "grafana.net" not in CONFIG
    assert not re.search(r"(username|password)\s*=\s*\"", CONFIG)


def _masked(line: str) -> str:
    """`stage.replace` replaces each capture group of its expression with `replace`.

    RE2's Unicode classes are spelled out for Python's `re`, which has none: `\\pL\\pN` (a letter
    or digit, inside a class that already holds `_`) is `\\w`, and `\\pL` alone `[^\\W\\d_]`.
    `make alloy-check` compiles the real expression with RE2.
    """
    (expression,) = _alloy_string("expression")
    (replace,) = _alloy_string("replace")
    python = expression.replace(r"\pL\pN", r"\w").replace(r"\pL", r"[^\W\d_]")
    return re.sub(python, lambda m: m.group(0).replace(m.group(1), replace), line)


@pytest.mark.parametrize(
    ("line", "masked"),
    [
        (
            "DETAIL:  Key (email)=(ada.lovelace+climb@example.co.uk) already exists.",
            "DETAIL:  Key (email)=(<email>) already exists.",
        ),
        ("sign-in for Ada@Example.COM refused", "sign-in for <email> refused"),
        ("a@b.io and c_d@e-f.org", "<email> and <email>"),
        # Europe first (PRD 0008 G15): names and domains outside ASCII.
        ('Key (email)=("józef.nowak@example.pl")', 'Key (email)=("<email>")'),
        ("user@bücher.de and Łucja@poczta.pl", "<email> and <email>"),
        ("GET /sign-in?email=carol%40example.net", "GET /sign-in?email=<email>"),
        ('{"logger": "ai_trainer.access", "route": "/sign-in"}', None),
        ("postgres@db:5432 ready to accept connections", None),
        ("2026-10-25T18:00:00Z GET /messages/{message_id}/reply 200 12.0ms", None),
    ],
)
def test_email_shaped_values_are_masked_before_shipping(line: str, masked: str | None) -> None:
    assert _masked(line) == (line if masked is None else masked)


def test_alloy_check_uses_the_image_compose_runs(compose: dict[str, Any]) -> None:
    makefile = (ROOT / "Makefile").read_text()
    target = makefile.split("\nalloy-check:", 1)[1].split("\n\n", 1)[0]
    image = compose["services"]["alloy"]["image"]
    assert "@sha256:" in image
    assert f"\nALLOY_IMAGE = {image}\n" in makefile
    assert target.count("$(ALLOY_IMAGE)") == 3
    assert "fmt --test" in target
    assert "validate" in target
