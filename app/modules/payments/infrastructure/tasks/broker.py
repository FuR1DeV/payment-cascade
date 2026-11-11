"""Taskiq broker and scheduler.

Worker:    taskiq worker app.modules.payments.infrastructure.tasks.broker:broker \
               app.modules.payments.infrastructure.tasks.tasks
Scheduler: taskiq scheduler app.modules.payments.infrastructure.tasks.broker:scheduler \
               app.modules.payments.infrastructure.tasks.tasks

RedisStreamBroker uses Redis Streams with consumer groups: a message is acknowledged
after the task finishes, so a task taken by a worker that died is delivered again.
Our tasks are idempotent anyway (they claim work from the database).
"""

from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisStreamBroker

from app.core.config import get_settings
from app.core.logging import configure_logging

_settings = get_settings()

broker = RedisStreamBroker(url=_settings.redis_url, queue_name="payments")
scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    from app.modules.payments.bootstrap import build_container  # avoid an import cycle

    configure_logging(_settings.log_level)
    state.container = await build_container(_settings)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:
    container = getattr(state, "container", None)
    if container is not None:
        await container.close()
