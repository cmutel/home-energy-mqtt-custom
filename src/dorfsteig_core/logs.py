import logging

from dorfsteig_core.settings import settings


def setup_logging() -> None:
    """Log to stderr; under systemd this ends up in journald."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
