from __future__ import annotations

import argparse
import logging
from pathlib import Path

from core.bot_app import TwinBotApp
from core.config import load_settings
from core.web_app import create_web_app
from modules.i18n import setup_i18n


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.info("logging_configured level=INFO")


def run_bot() -> None:
    settings = load_settings()
    setup_i18n(Path(__file__).resolve().parent, settings.default_lang)
    logging.info("starting_bot")
    app = TwinBotApp(settings)
    app.run()


def run_web() -> None:
    settings = load_settings()
    logging.info("starting_web host=0.0.0.0 port=5000")
    app = create_web_app(settings)
    app.run(host="0.0.0.0", port=5000)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Biographer runtime")
    parser.add_argument("service", choices=["bot", "web"], nargs="?", default="bot")
    args = parser.parse_args()

    configure_logging()
    if args.service == "web":
        run_web()
    else:
        run_bot()


if __name__ == "__main__":
    main()
