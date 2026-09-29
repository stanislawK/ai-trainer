from pydantic import BaseModel, ConfigDict


class SportPlugin(BaseModel):
    """What the M1 skeleton needs to know about a sport (ADR-0006): an ID, the key its label
    is looked up by in templates (G5), and its Lucide icon. Payloads, normalizers and load
    calculators join this when M1 logging lands."""

    model_config = ConfigDict(frozen=True)

    id: str
    label_key: str
    icon: str
