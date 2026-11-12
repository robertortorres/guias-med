import os
from functools import lru_cache
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path(os.environ.get("CONVENIOS_YAML", BASE / "config" / "convenios.yaml"))
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
WORKERS = int(os.environ.get("WORKERS", str(max(1, (os.cpu_count() or 2)))))


@lru_cache(maxsize=4)
def load_config(path: str | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)
