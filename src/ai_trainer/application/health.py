from ai_trainer.application.ports.health import DatabaseHealthPort


async def check_database_health(port: DatabaseHealthPort) -> bool:
    return await port.ping()
