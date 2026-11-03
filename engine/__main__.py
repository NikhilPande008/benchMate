"""Run the engine: uv run python -m engine"""
import uvicorn

from engine.config import Settings
from engine.main import create_app

if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
