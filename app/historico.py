"""Histórico de guias exportadas (SQLite) para alertar duplicidade entre lotes."""
import sqlite3
from contextlib import closing
from datetime import datetime

from .config import DATA_DIR

DB = DATA_DIR / "historico.sqlite3"


def _conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS guias (
        numero TEXT NOT NULL, convenio TEXT, nome TEXT, lote TEXT, exportado_em TEXT,
        PRIMARY KEY (numero, convenio))""")
    return c


def lookup(numeros: list[str]) -> dict[str, dict]:
    numeros = [n for n in numeros if n]
    if not numeros:
        return {}
    with closing(_conn()) as c:
        q = "SELECT numero, convenio, nome, lote, exportado_em FROM guias WHERE numero IN (%s)" % ",".join("?" * len(numeros))
        return {r[0]: {"convenio": r[1], "nome": r[2], "lote": r[3], "exportado_em": r[4]} for r in c.execute(q, numeros)}


def record(items: list[dict], lote: str) -> None:
    now = datetime.now().isoformat(timespec="minutes")
    with closing(_conn()) as c, c:
        c.executemany(
            "INSERT OR IGNORE INTO guias (numero, convenio, nome, lote, exportado_em) VALUES (?,?,?,?,?)",
            [(i["numero"], i.get("convenio") or "", i.get("nome", ""), lote, now) for i in items if i.get("numero")],
        )
