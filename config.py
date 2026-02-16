import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    olx_url: str
    poll_seconds: int
    user_agent: str


def load_config() -> Config:
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    olx_url = os.getenv("OLX_URL", "https://www.olx.ua/uk/otdam-darom/")
    poll_seconds = int(os.getenv("POLL_SECONDS", "300"))
    user_agent = os.getenv(
        "UA",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    )
    return Config(
        bot_token=bot_token,
        olx_url=olx_url,
        poll_seconds=poll_seconds,
        user_agent=user_agent,
    )
