from __future__ import annotations

import logging
import sys

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    settings = Settings()
    if not settings.effective_llm_api_key:
        logging.warning(
            "No LLM API key is configured. Set LLM_API_KEY in .env or the "
            "provider-specific environment variable before sending a request."
        )
    if settings.onebot_token.get_secret_value() in {
        "",
        "replace-with-a-long-random-token",
    }:
        logging.warning(
            "ONEBOT_TOKEN is empty or still a placeholder. "
            "Set the same strong token in .env and NapCat."
        )
    uvicorn.run(
        create_app(settings=settings),
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

