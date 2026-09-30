from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from services.market_data.app.storage import database_url_from_environment


def create_database_engine() -> AsyncEngine:
    return create_async_engine(
        database_url_from_environment(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
