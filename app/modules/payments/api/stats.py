from dataclasses import asdict
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query

from .dependencies import ContainerDep

router = APIRouter(tags=["stats"])


@router.get("/stats/providers")
async def provider_stats(
    container: ContainerDep,
    window_minutes: Annotated[int, Query(ge=1, le=7 * 24 * 60)] = 60,
) -> list[dict[str, Any]]:
    """Per provider: attempts by outcome, approval rate, technical failure rate and the
    current circuit breaker state."""
    stats = await container.queries.provider_stats(timedelta(minutes=window_minutes))
    return [asdict(s) for s in stats]
