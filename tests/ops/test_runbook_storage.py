"""The owner's steps for the `uploads` bucket and its token (ADR-0033, ticket #420)."""

from pathlib import Path

ROOT = Path(__file__).parents[2]


def _section() -> str:
    runbook = (ROOT / "deploy" / "RUNBOOK.md").read_text()
    start = runbook.index("## 14. Knowledge file storage")
    return runbook[start : runbook.index("## Provisioning record")]


def test_runbook_covers_bucket_and_token() -> None:
    section = _section()

    assert "wrangler r2 bucket create" in section
    assert "Object Read & Write" in section
    assert "Apply to specific buckets only" in section
    assert "storage-check" in section
    assert "keep the originals" in section.lower()


def test_runbook_names_every_storage_setting() -> None:
    section = _section()
    env_example = (ROOT / ".env.example").read_text()

    keys = [
        line.split("=", 1)[0]
        for line in env_example.splitlines()
        if line.startswith("OBJECT_STORAGE_")
    ]

    assert keys
    for key in keys:
        assert key in section, key
