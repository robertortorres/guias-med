"""Lotes: armazenamento em disco, processamento em segundo plano e exportação do ZIP."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pymupdf

from . import historico
from .config import DATA_DIR, RETENTION_DAYS, WORKERS, load_config
from .pipeline import file_name, group_pages, process_page

LOTES_DIR = DATA_DIR / "lotes"
_pool: ProcessPoolExecutor | None = None
_lock = threading.Lock()
ID_RE = re.compile(r"^[a-f0-9]{12}$")


def pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=WORKERS)
    return _pool


# --------------------------------------------------------------------------- disco

def lote_dir(lote_id: str) -> Path:
    if not ID_RE.match(lote_id):
        raise FileNotFoundError(lote_id)
    return LOTES_DIR / lote_id


def load(lote_id: str) -> dict:
    with open(lote_dir(lote_id) / "lote.json", encoding="utf-8") as f:
        return json.load(f)


def save(lote: dict) -> None:
    d = lote_dir(lote["id"])
    tmp = d / "lote.json.tmp"
    with _lock:
        tmp.write_text(json.dumps(lote, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / "lote.json")


def list_lotes() -> list[dict]:
    out = []
    if not LOTES_DIR.exists():
        return out
    for d in LOTES_DIR.iterdir():
        try:
            lote = load(d.name)
        except Exception:
            continue
        out.append({k: lote.get(k) for k in ("id", "criado", "status", "arquivos", "total", "feitos", "exportado")}
                   | {"pacotes": len(lote.get("pacotes") or [])})
    return sorted(out, key=lambda x: x["criado"], reverse=True)


def cleanup_old() -> int:
    """Apaga lotes mais antigos que RETENTION_DAYS (dados de pacientes não ficam guardados)."""
    if not LOTES_DIR.exists():
        return 0
    limit = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for d in LOTES_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < limit:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def delete(lote_id: str) -> None:
    shutil.rmtree(lote_dir(lote_id), ignore_errors=True)


# --------------------------------------------------------------------------- criação / processamento

def create(files: list[tuple[str, bytes]]) -> str:
    cleanup_old()
    lote_id = uuid.uuid4().hex[:12]
    d = LOTES_DIR / lote_id
    (d / "pdf").mkdir(parents=True)
    (d / "img").mkdir()
    arquivos = []
    for i, (name, data) in enumerate(files):
        safe = re.sub(r"[^\w.\- ]", "_", Path(name).name) or f"arquivo_{i}.pdf"
        safe = f"{i:02d}_{safe}"
        (d / "pdf" / safe).write_bytes(data)
        with pymupdf.open(d / "pdf" / safe) as doc:
            arquivos.append({"arquivo": safe, "original": name, "paginas": doc.page_count})
    lote = {
        "id": lote_id,
        "criado": datetime.now().isoformat(timespec="seconds"),
        "status": "processando",
        "arquivos": arquivos,
        "total": sum(a["paginas"] for a in arquivos),
        "feitos": 0,
        "paginas": [],
        "pacotes": [],
        "erro": None,
        "exportado": None,
    }
    save(lote)
    threading.Thread(target=_run, args=(lote_id,), daemon=True).start()
    return lote_id


def _run(lote_id: str) -> None:
    lote = load(lote_id)
    d = lote_dir(lote_id)
    try:
        results: dict[tuple[str, int], dict] = {}
        ex = pool()
        hints: dict[str, int | None] = {}

        def done(fut, key):
            results[key] = fut.result()
            lote["feitos"] = len(results)
            save(lote)

        # 1ª página de cada PDF primeiro: a rotação dela vira dica para as demais
        firsts = {ex.submit(process_page, str(d / "pdf" / a["arquivo"]), 0, str(d / "img")): a["arquivo"]
                  for a in lote["arquivos"] if a["paginas"]}
        for fut in as_completed(firsts):
            arq = firsts[fut]
            done(fut, (arq, 0))
            hints[arq] = results[(arq, 0)]["rotacao"]

        futs = {}
        for a in lote["arquivos"]:
            for i in range(1, a["paginas"]):
                f = ex.submit(process_page, str(d / "pdf" / a["arquivo"]), i, str(d / "img"), hints.get(a["arquivo"]))
                futs[f] = (a["arquivo"], i)
        for fut in as_completed(futs):
            done(fut, futs[fut])

        paginas = [results[(a["arquivo"], i)] for a in lote["arquivos"] for i in range(a["paginas"])]
        lote["paginas"] = paginas
        lote["pacotes"] = _build_packages(paginas)
        lote["status"] = "revisao"
    except Exception as e:  # erro aparece na tela, não derruba o servidor
        lote["status"] = "erro"
        lote["erro"] = f"{type(e).__name__}: {e}"
    save(lote)


def _build_packages(paginas: list[dict]) -> list[dict]:
    pacotes = group_pages(paginas)
    hist = historico.lookup([p["numero"] for p in pacotes])
    for i, p in enumerate(pacotes):
        g = paginas[p["paginas"][0]]
        p["id"] = i
        p["numero_fonte"] = g.get("numero_fonte", "")
        p["nome_fonte"] = g.get("nome_fonte", "")
        p["crop_numero"] = g.get("crop_numero", "")
        p["crop_nome"] = g.get("crop_nome", "")
        p["template"] = g.get("template")
        alertas = list(g.get("alertas", []))
        if p.pop("sem_guia", False):
            alertas.append("Nenhuma guia identificada no começo deste PDF; confira se essas páginas são de outro pacote.")
        for pg in p["paginas"][1:]:
            alertas.extend(paginas[pg].get("alertas", []))
        h = hist.get(p["numero"])
        if h:
            dia = datetime.fromisoformat(h["exportado_em"]).strftime("%d/%m/%Y")
            alertas.append(f"Guia já exportada em {dia} (lote {h['lote']}). Confira se não é cobrança em duplicidade.")
        p["alertas"] = alertas
    return pacotes


# --------------------------------------------------------------------------- revisão / exportação

def validate_packages(lote: dict, pacotes: list[dict]) -> list[dict]:
    """Confere que cada página aparece exatamente uma vez e normaliza os campos editáveis."""
    n = len(lote["paginas"])
    seen: list[int] = []
    clean = []
    for p in pacotes:
        pages = [int(x) for x in p.get("paginas", [])]
        if not pages:
            continue
        seen.extend(pages)
        clean.append({
            **{k: p.get(k) for k in ("alertas", "crop_numero", "crop_nome", "numero_fonte", "nome_fonte", "template", "convenio")},
            "id": len(clean),
            "paginas": pages,
            "tipo": p.get("tipo") if p.get("tipo") in ("consulta", "sadt") else "consulta",
            "numero": re.sub(r"\D", "", str(p.get("numero", ""))),
            "nome": re.sub(r"\s+", " ", str(p.get("nome", ""))).strip().upper(),
        })
    if sorted(seen) != list(range(n)):
        raise ValueError("Cada página precisa estar em exatamente um pacote.")
    return clean


def export_zip(lote: dict) -> tuple[bytes, list[str]]:
    cfg = load_config()
    d = lote_dir(lote["id"])
    paginas = lote["paginas"]
    docs: dict[str, pymupdf.Document] = {}
    used: dict[str, int] = {}
    names = []
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in lote["pacotes"]:
                base = file_name(p, cfg)
                used[base] = used.get(base, 0) + 1
                fname = base if used[base] == 1 else f"{base} ({used[base]})"
                out = pymupdf.open()
                for idx in p["paginas"]:
                    pg = paginas[idx]
                    src = docs.get(pg["arquivo"])
                    if src is None:
                        src = docs[pg["arquivo"]] = pymupdf.open(d / "pdf" / pg["arquivo"])
                    out.insert_pdf(src, from_page=pg["pagina"], to_page=pg["pagina"])
                z.writestr(f"{fname}.pdf", out.tobytes(garbage=3, deflate=True))
                out.close()
                names.append(fname)
    finally:
        for doc in docs.values():
            doc.close()
    historico.record(lote["pacotes"], lote["id"])
    return buf.getvalue(), names
