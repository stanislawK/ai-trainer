"""The container's health probe (the Dockerfile's HEALTHCHECK, ticket #216).

It calls `/health` on the local port but sends the Host of `PUBLIC_BASE_URL`, because
`TrustedHostMiddleware` admits only that host (ADR-0023) and a `localhost` Host would get a 400.
Exits 0 when the app answers 2xx, 1 otherwise.
"""

import os
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

HEALTH_URL = "http://localhost:8000/health"
# Above the DB ping's connect timeout inside /health (2s default), so a slow-to-fail database
# is reported by the route rather than timing out the probe.
TIMEOUT_SECONDS = 5


def probe_request(public_base_url: str) -> Request:
    return Request(HEALTH_URL, headers={"Host": urlsplit(public_base_url).netloc})


def main() -> int:
    # A container probe, not app code: the container's environment is `PUBLIC_BASE_URL`'s source.
    try:
        urlopen(probe_request(os.environ["PUBLIC_BASE_URL"]), timeout=TIMEOUT_SECONDS)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
