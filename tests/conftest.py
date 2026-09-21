from pydantic_ai import models

# No test may reach a real model (ADR-0013 invariant 1).
models.ALLOW_MODEL_REQUESTS = False
