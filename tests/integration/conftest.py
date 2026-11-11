"""Integration tests run against real PostgreSQL and Redis (docker compose or CI services)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app
from app.modules.payments.bootstrap import Container, build_container
from tests.fakes import FakeProvider, RecordingJobs

from .helpers import DATABASE_URL, REDIS_URL, make_settings


@pytest.fixture(scope="session", autouse=True)
def _migrate() -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
async def _clean() -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE payments, payment_attempts, webhook_events, payment_alerts CASCADE")
        )
    await engine.dispose()
    redis = Redis.from_url(REDIS_URL)
    await redis.flushdb()
    await redis.aclose()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def alpha() -> FakeProvider:
    return FakeProvider("mock_alpha")


@pytest.fixture
def beta() -> FakeProvider:
    return FakeProvider("mock_beta")


@pytest.fixture
def gamma() -> FakeProvider:
    return FakeProvider("mock_gamma")


@pytest.fixture
def jobs() -> RecordingJobs:
    return RecordingJobs()


@pytest.fixture
async def container(
    settings: Settings,
    alpha: FakeProvider,
    beta: FakeProvider,
    gamma: FakeProvider,
    jobs: RecordingJobs,
) -> AsyncIterator[Container]:
    # Routing comes from the seeded rules: USD + DE -> mock_alpha, mock_beta, mock_gamma.
    c = await build_container(settings, providers=[alpha, beta, gamma], jobs=jobs)
    yield c
    await c.close()


@pytest.fixture
async def client(container: Container) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
