import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("ticketsbot.app:create_app", factory=True, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
