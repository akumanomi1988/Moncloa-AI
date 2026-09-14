#!/usr/bin/env python3
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
(PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(PROJECT_ROOT / "logs" / "moncloa.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def free_port(port: int = 8000):
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                pid = parts[-1]
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
                logger.info(f"Liberado puerto {port} (PID {pid})")
    except Exception as e:
        logger.warning(f"No se pudo liberar puerto {port}: {e}")


def init_data_dirs():
    from config import settings
    settings.SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    settings.ACTS_PATH.mkdir(parents=True, exist_ok=True)
    (settings.LOGS_PATH / "moncloa.log").parent.mkdir(parents=True, exist_ok=True)


def run_web():
    free_port(8000)
    import uvicorn
    from web.app import app
    logger.info("Starting Moncloa-AI web server on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


def run_cli():
    from config import settings
    from core.memory_store import MemoryStore
    from core.vector_store import VectorStore
    from core.api_client import NSClient
    from core.deliberation_engine import DeliberationEngine
    from services.vision import generate_initial_vision

    memory = MemoryStore(settings.SQLITE_PATH)
    vectors = VectorStore(settings.CHROMA_PATH, settings.LMSTUDIO_URL, settings.EMBEDDING_MODEL)
    api = NSClient(settings.NATION_NAME, settings.NATION_PASSWORD, settings.CONTACT_EMAIL)
    engine = DeliberationEngine(memory, vectors, api)

    if vectors.get_vision() is None:
        generate_initial_vision(vectors)

    logger.info("Starting Moncloa-AI in CLI mode (headless)")
    asyncio.run(engine.main_loop())


def main():
    init_data_dirs()

    args = sys.argv[1:]
    if "--cli" in args:
        run_cli()
    else:
        run_web()


if __name__ == "__main__":
    main()
