from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.modules.payments.api import router as payments_router
from app.modules.payments.bootstrap import Container, build_container


def create_app(container: Container | None = None) -> FastAPI:
    """With a ready container (tests) the lifespan does not build or close anything."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if container is not None:
            yield
            return
        settings = get_settings()
        configure_logging(settings.log_level)
        from app.modules.payments.infrastructure.tasks.broker import broker

        await broker.startup()  # needed to send tasks (.kiq) from the API process
        app.state.container = await build_container(settings)
        try:
            yield
        finally:
            await app.state.container.close()
            await broker.shutdown()

    app = FastAPI(
        title="Payment Cascade",
        version="0.1.0",
        description="Cascading card payments across several providers.",
        lifespan=lifespan,
    )
    if container is not None:
        app.state.container = container
    app.include_router(payments_router)

    @app.get("/health", tags=["service"])
    async def health(request: Request) -> JSONResponse:
        c: Container = request.app.state.container
        checks: dict[str, str] = {}
        try:
            async with c.engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:
            checks["postgres"] = f"error: {type(exc).__name__}"
        try:
            await c.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {type(exc).__name__}"
        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {
                "status": "ok" if healthy else "degraded",
                "checks": checks,
                "providers": c.providers.codes(),
            },
            status_code=200 if healthy else 503,
        )

    return app


app = create_app()
