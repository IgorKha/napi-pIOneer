"""python -m pioneer — запуск core (uvicorn)."""
import uvicorn

from pioneer.api.app import create_app
from pioneer.config import load_config


def main() -> None:
    cfg = load_config()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
