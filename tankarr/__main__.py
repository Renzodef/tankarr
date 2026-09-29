from __future__ import annotations

import uvicorn

from tankarr.config import get_settings
from tankarr.logs import configure_logging


def main() -> None:
    settings = get_settings()
    # uvicorn only configures its own loggers, so everything Tankarr logs -
    # which source was chosen, which file was retired, why a job failed - was
    # never reaching the container output. Configure the root logger first
    # (console for `docker logs`, a rotating file for the System page) and let
    # uvicorn use it instead of replacing it.
    configure_logging(settings)
    uvicorn.run(
        "tankarr.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
        log_config=None,
        # The interface polls every few seconds; uvicorn's five-second default
        # closed the idle connection just before each poll reopened it.
        timeout_keep_alive=30,
    )


if __name__ == "__main__":
    main()
