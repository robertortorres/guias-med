"""Separador de guias de convênio — app web."""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import lotes
from .config import RETENTION_DAYS, load_config

HERE = Path(__file__).resolve().parent
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
APP_USER = os.environ.get("APP_USER")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

security = HTTPBasic(auto_error=False)


def auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not (APP_USER and APP_PASSWORD) or request.url.path == "/saude":
        return
    ok = credentials is not None and secrets.compare_digest(credentials.username, APP_USER) \
        and secrets.compare_digest(credentials.password, APP_PASSWORD)
    if not ok:
        raise HTTPException(401, "Login necessário", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="Separador de guias", dependencies=[Depends(auth)])
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.on_event("startup")
def _startup() -> None:
    lotes.LOTES_DIR.mkdir(parents=True, exist_ok=True)
    lotes.cleanup_old()


def _get(lote_id: str) -> dict:
    try:
        return lotes.load(lote_id)
    except (FileNotFoundError, OSError):
        raise HTTPException(404, "Lote não encontrado. Ele pode ter sido apagado pela limpeza automática.")


# --------------------------------------------------------------------------- páginas

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "lotes": lotes.list_lotes(), "retencao": RETENTION_DAYS, "max_mb": MAX_UPLOAD_MB,
        "convenios": sorted({t["convenio"] for t in load_config()["templates"]}),
    })


@app.post("/lotes")
async def criar_lote(arquivos: list[UploadFile] = File(...)):
    files, total = [], 0
    for up in arquivos:
        data = await up.read()
        total += len(data)
        if total > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(413, f"Os arquivos passam de {MAX_UPLOAD_MB} MB. Envie em lotes menores.")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, f"{up.filename} não é um PDF.")
        files.append((up.filename or "arquivo.pdf", data))
    if not files:
        raise HTTPException(400, "Nenhum arquivo enviado.")
    lote_id = lotes.create(files)
    return RedirectResponse(f"/lotes/{lote_id}", status_code=303)


@app.get("/lotes/{lote_id}", response_class=HTMLResponse)
def ver_lote(request: Request, lote_id: str):
    lote = _get(lote_id)
    return templates.TemplateResponse(request, "lote.html", {"lote": lote})


# --------------------------------------------------------------------------- API usada pela tela

@app.get("/lotes/{lote_id}/dados")
def dados(lote_id: str):
    lote = _get(lote_id)
    cfg = load_config()
    return JSONResponse({**lote, "nomes_arquivo": cfg["nomes_arquivo"]})


@app.get("/lotes/{lote_id}/img/{nome}")
def imagem(lote_id: str, nome: str):
    if not re.fullmatch(r"[\w.\- ]+\.(png|jpg)", nome):
        raise HTTPException(404)
    path = lotes.lote_dir(lote_id) / "img" / nome
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


@app.put("/lotes/{lote_id}/pacotes")
async def salvar(lote_id: str, request: Request):
    lote = _get(lote_id)
    if lote["status"] not in ("revisao", "exportado"):
        raise HTTPException(409, "O lote ainda está sendo processado.")
    body = await request.json()
    try:
        lote["pacotes"] = lotes.validate_packages(lote, body.get("pacotes", []))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    lotes.save(lote)
    return {"ok": True}


@app.post("/lotes/{lote_id}/exportar")
async def exportar(lote_id: str, request: Request):
    lote = _get(lote_id)
    body = await request.json()
    try:
        lote["pacotes"] = lotes.validate_packages(lote, body.get("pacotes", []))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    faltando = [i + 1 for i, p in enumerate(lote["pacotes"]) if not p["numero"] or not p["nome"]]
    if faltando:
        raise HTTPException(400, f"Preencha número e nome dos pacotes {', '.join(map(str, faltando))}.")
    data, _ = lotes.export_zip(lote)
    lote["status"] = "exportado"
    lote["exportado"] = datetime.now().isoformat(timespec="seconds")
    lotes.save(lote)
    origem = Path(lote["arquivos"][0]["original"]).stem if lote["arquivos"] else lote_id
    fname = re.sub(r"[^\w.\-]", "_", f"guias_{origem}.zip")
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.delete("/lotes/{lote_id}")
def apagar(lote_id: str):
    _get(lote_id)
    lotes.delete(lote_id)
    return {"ok": True}


@app.get("/saude")
def saude():
    return {"ok": True}
