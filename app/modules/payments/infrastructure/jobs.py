class TaskiqJobQueue:
    """JobQueue port on top of Taskiq."""

    async def kick_refunds(self) -> None:
        from .tasks.tasks import execute_refunds  # lazy: tasks import the broker and settings

        await execute_refunds.kiq()
