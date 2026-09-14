import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

NATION_NAME = os.getenv("NATION_NAME", "")
NATION_PASSWORD = os.getenv("NATION_PASSWORD", "")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")

LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "http://localhost:1234")
LLM_MODEL = os.getenv("LLM_MODEL", "")  # LM Studio ignora el nombre (usa el modelo cargado)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL", "30"))

PERSONALITIES_PATH = PROJECT_ROOT / "config" / "personalities.yaml"
SQLITE_PATH = PROJECT_ROOT / "data" / "moncloa.db"
CHROMA_PATH = PROJECT_ROOT / "data" / "chroma_db"
LOGS_PATH = PROJECT_ROOT / "logs"
ACTS_PATH = LOGS_PATH / "consejo_ministros"
