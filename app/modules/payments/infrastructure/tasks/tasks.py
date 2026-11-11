"""Periodic and on-demand background tasks. Each one is safe to run concurrently."""

from typing import Annotated

from taskiq import Context, TaskiqDepends

from .broker import broker


def _container(context: Context):  # type: ignore[no-untyped-def]
    return context.state.container


@broker.task(task_name="payments.reconcile_unknown", schedule=[{"interval": 15}])
async def reconcile_unknown(context: Annotated[Context, TaskiqDepends()]) -> int:
    """Ask providers about attempts with an unknown outcome (payments in requires_review)."""
    return await _container(context).reconcile_unknown()


@broker.task(task_name="payments.execute_refunds", schedule=[{"interval": 10}])
async def execute_refunds(context: Annotated[Context, TaskiqDepends()]) -> int:
    """Refund extra charges. Also kicked right after an extra charge is detected."""
    return await _container(context).execute_refunds()


@broker.task(task_name="payments.recover_stale", schedule=[{"interval": 30}])
async def recover_stale(context: Annotated[Context, TaskiqDepends()]) -> int:
    """Pick up payments left in created/processing by a process that died."""
    return await _container(context).recover_stale()
