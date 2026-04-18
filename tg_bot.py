from pathlib import Path

from core.bot_app import TwinBotApp
from core.config import load_settings
from modules.i18n import setup_i18n


def main() -> None:
    settings = load_settings()
    setup_i18n(Path(__file__).resolve().parent, settings.default_lang)
    app = TwinBotApp(settings)
    print("🤖 AI Twin bot started")
    app.run()


if __name__ == "__main__":
    main()
