import logging

from app.core.logging import configure_logging


def pytest_configure() -> None:
    configure_logging("WARNING")
    logging.getLogger("payments").setLevel(logging.WARNING)
