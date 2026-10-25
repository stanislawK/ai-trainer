from zoneinfo import available_timezones


def timezone_names() -> tuple[str, ...]:
    """The IANA zones an athlete can pick, sorted: `Region/City` names plus `UTC`. The
    bare legacy aliases `zoneinfo` also knows (`Poland`, `Factory`, `EST`) are left out."""
    return tuple(sorted(name for name in available_timezones() if "/" in name or name == "UTC"))
