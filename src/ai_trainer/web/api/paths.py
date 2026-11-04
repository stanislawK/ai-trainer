"""Where the token routes live (ADR-0036): the session gate and the CSRF check stand aside here,
and the rate limit counts by token, because these routes authenticate by bearer token alone."""

KNOWLEDGE_API_PREFIX = "/api/admin/knowledge"


def is_knowledge_api(path: str) -> bool:
    """The prefix itself or a path under it; `/api/admin/knowledgex` is not."""
    return path == KNOWLEDGE_API_PREFIX or path.startswith(f"{KNOWLEDGE_API_PREFIX}/")
