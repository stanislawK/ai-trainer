import psycopg


def to_psycopg_dsn(dsn: str) -> str:
    """Strips any SQLAlchemy-style `+driver` suffix so psycopg can parse the DSN."""
    scheme, sep, rest = dsn.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}"


class PsycopgDatabaseHealth:
    """Implements DatabaseHealthPort with a short-lived psycopg connection."""

    def __init__(self, dsn: str, *, connect_timeout: int = 2) -> None:
        self._dsn = to_psycopg_dsn(dsn)
        self._connect_timeout = connect_timeout

    async def ping(self) -> bool:
        try:
            async with (
                await psycopg.AsyncConnection.connect(
                    self._dsn, connect_timeout=self._connect_timeout
                ) as conn,
                conn.cursor() as cur,
            ):
                await cur.execute("SELECT 1")
        except psycopg.Error:
            return False
        return True
