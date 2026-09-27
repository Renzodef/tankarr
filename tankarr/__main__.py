from __future__ import annotations

import logging

import uvicorn

from tankarr.config import get_settings


def main() -> None:
    settings = get_settings()
    # uvicorn only configures its own loggers, so everything Tankarr logs -
    # which source was chosen, which file was retired, why a job failed - was
    # never reaching the container output. Configure the root logger first and
    # let uvicorn use it instead of replacing it.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    uvicorn.run(
        "tankarr.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
