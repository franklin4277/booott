import os
from urllib.parse import quote_plus

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    insert,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from schemas.messages import BarData, TickData

metadata = MetaData()

ticks = Table(
    "market_ticks",
    metadata,
    Column("timestamp", DateTime(timezone=True), primary_key=True),
    Column("tick_id", String(36), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("source", String(64), nullable=False),
    Column("bid", Numeric(24, 10), nullable=False),
    Column("ask", Numeric(24, 10), nullable=False),
    Column("last", Numeric(24, 10)),
    Column("volume", Numeric(24, 10)),
    Column("sequence", BigInteger),
)

bars = Table(
    "market_bars",
    metadata,
    Column("timestamp", DateTime(timezone=True), primary_key=True),
    Column("symbol", String(32), primary_key=True),
    Column("timeframe", String(16), primary_key=True),
    Column("open", Numeric(24, 10), nullable=False),
    Column("high", Numeric(24, 10), nullable=False),
    Column("low", Numeric(24, 10), nullable=False),
    Column("close", Numeric(24, 10), nullable=False),
    Column("tick_volume", BigInteger, nullable=False),
    Column("spread", Integer),
    Column("real_volume", Numeric(24, 10)),
)


def database_url_from_environment() -> str:
    configured_url = os.environ.get("DATABASE_URL")
    if configured_url:
        if configured_url.startswith("postgres://"):
            return "postgresql+asyncpg://" + configured_url[len("postgres://"):]
        if configured_url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + configured_url[len("postgresql://"):]
        return configured_url

    user = quote_plus(os.environ.get("POSTGRES_USER", "trading_app"))
    password = quote_plus(os.environ.get("POSTGRES_PASSWORD", ""))
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = quote_plus(os.environ.get("POSTGRES_DB", "trading"))
    credentials = f"{user}:{password}" if password else user
    return f"postgresql+asyncpg://{credentials}@{host}:{port}/{database}"


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


class MarketDataStore:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self.engine = engine or create_async_engine(
            database_url_from_environment(),
            pool_pre_ping=True,
        )
        self._dialect = self.engine.url.get_backend_name()

    async def initialize(self) -> None:
        url = str(self.engine.url)
        async with self.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            if not _is_sqlite(url):
                await connection.execute(
                    text(
                        "SELECT create_hypertable('market_ticks', 'timestamp', "
                        "if_not_exists => TRUE, migrate_data => TRUE)"
                    )
                )
                await connection.execute(
                    text(
                        "SELECT create_hypertable('market_bars', 'timestamp', "
                        "if_not_exists => TRUE, migrate_data => TRUE)"
                    )
                )

    def _insert_stmt(self, table):
        if self._dialect == "postgresql":
            return pg_insert(table)
        if self._dialect == "sqlite":
            return sqlite_insert(table)
        return insert(table)

    async def store_tick(self, tick: TickData) -> None:
        values = {
            "timestamp": tick.timestamp,
            "tick_id": str(tick.tick_id),
            "symbol": tick.symbol,
            "source": tick.source,
            "bid": tick.bid,
            "ask": tick.ask,
            "last": tick.last,
            "volume": tick.volume,
            "sequence": tick.sequence,
        }
        statement = self._insert_stmt(ticks).values(**values).on_conflict_do_nothing(
            index_elements=["timestamp", "tick_id"]
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement)

    async def store_bar(self, bar: BarData) -> None:
        values = {
            "timestamp": bar.timestamp,
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "tick_volume": bar.tick_volume,
            "spread": bar.spread,
            "real_volume": bar.real_volume,
        }
        statement = self._insert_stmt(bars).values(**values).on_conflict_do_nothing(
            index_elements=["timestamp", "symbol", "timeframe"]
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement)

    async def aclose(self) -> None:
        await self.engine.dispose()
