"""Leitura das páginas escaneadas: orientação, classificação, extração e agrupamento.

Tudo roda em CPU. Cada página é processada de forma independente (paralelizável);
o agrupamento em pacotes acontece depois, em ordem.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pymupdf
import pytesseract
from PIL import Image, ImageOps
from pyzbar.pyzbar import decode as decode_barcodes

from .config import load_config

DPI = 200
OCR_LANG = "por+eng"
HEADER_KEYWORDS = ("GUIA", "CONSULTA", "SADT", "SERVI", "REGISTRO", "ANS", "NOME", "PRESTADOR", "BENEFICI")


def _ocr_lang() -> str:
    try:
        langs = pytesseract.get_languages()
    except Exception:
        return "eng"
    return OCR_LANG if "por" in langs else "eng"


@dataclass
class PageResult:
    arquivo: str            # nome do PDF de origem
    pagina: int             # índice 0-based dentro do PDF
    rotacao: int | None     # graus (PIL, anti-horário) para ficar legível; None = não detectada
    tipo: str               # consulta | sadt | anexo
    template: str | None
    convenio: str | None
    numero: str = ""
    numero_fonte: str = ""  # barcode | ocr | ""
    nome: str = ""
    nome_fonte: str = ""    # nome | nome_social | ""
    ans_confere: bool | None = None
    alertas: list[str] = field(default_factory=list)
    thumb: str = ""
    view: str = ""
    crop_numero: str = ""
    crop_nome: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- utilitários

def render_page(pdf_path: Path, index: int, dpi: int = DPI) -> Image.Image:
    with pymupdf.open(pdf_path) as doc:
        pix = doc[index].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def crop_rel(img: Image.Image, box) -> Image.Image:
    w, h = img.size
    x0, y0, x1, y1 = box
    return img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def upscale(img: Image.Image, factor: float = 2.0) -> Image.Image:
    return img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)


def trim_right(img: Image.Image, pad: int = 24) -> Image.Image:
    """Corta o espaço vazio à direita do texto (deixa o recorte legível na tela)."""
    a = np.array(img) < 140
    a[a.mean(axis=1) > 0.4] = False          # remove linhas horizontais do formulário
    a[:, a.mean(axis=0) > 0.6] = False       # e bordas verticais
    cols = np.flatnonzero(a.mean(axis=0) > 0.10)   # colunas com traço de letra (linhas tortas ficam abaixo disso)
    # ignora linhas de borda isoladas no fim
    if cols.size == 0:
        return img
    gaps = np.flatnonzero(np.diff(cols) > img.height)  # buraco grande = fim do texto
    end = cols[gaps[0]] if gaps.size else cols[-1]
    return img.crop((0, 0, min(img.width, end + pad), img.height))


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def clean_name(raw: str) -> str:
    s = strip_accents(raw).upper()
    s = re.sub(r"[^A-Z ]", " ", s)
    words = [w for w in s.split() if len(w) > 1 or w in ("E",)]
    return " ".join(words)


def short_name(full: str) -> str:
    """Primeiro + último nome, em Title Case (regra usada nos lotes)."""
    parts = clean_name(full).split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].title()
    return f"{parts[0].title()} {parts[-1].title()}"


# --------------------------------------------------------------------------- orientação

def _header_text(img: Image.Image, lang: str) -> str:
    band = crop_rel(img, (0, 0, 1, 0.45))
    if band.width > 1400:
        band = band.resize((1400, int(band.height * 1400 / band.width)))
    return pytesseract.image_to_string(band, lang=lang, config="--psm 6")


def _score(text: str) -> int:
    up = strip_accents(text).upper()
    return sum(1 for k in HEADER_KEYWORDS if k in up)


def detect_orientation(img: Image.Image, lang: str, hint: int | None = None) -> tuple[int | None, str]:
    """Devolve (rotação, texto do cabeçalho). Guias vêm deitadas (90/270).
    `hint` = rotação da 1ª guia do mesmo PDF; testada primeiro para economizar OCR."""
    best = (None, "", 0)
    # guias são formulários em paisagem escaneados em A4 retrato: sempre 90 ou 270.
    angles = [90, 270, 0, 180] if os.environ.get("TRY_ALL_ROTATIONS") else [90, 270]
    if hint in angles:
        angles.remove(hint)
        angles.insert(0, hint)
    for angle in angles:
        rotated = img.rotate(angle, expand=True)
        text = _header_text(rotated, lang)
        score = _score(text)
        if score > best[2]:
            best = (angle, text, score)
        if score >= 4:
            break
    if best[2] < 2:
        return None, best[1]
    return best[0], best[1]


# --------------------------------------------------------------------------- classificação

def classify(header_text: str, templates: list[dict]) -> dict | None:
    up = strip_accents(header_text).upper()
    for t in templates:
        det = t.get("detect", {})
        if not all(k.upper() in up for k in det.get("all", [])):
            continue
        anys = det.get("any", [])
        if anys and not any(k.upper() in up for k in anys):
            continue
        return t
    return None


def classify_by_number(header_text: str, templates: list[dict]) -> dict | None:
    """Plano C: o tipo de guia foi lido, o convênio não. Usa o formato do número
    (ex.: Porto consulta = 8 dígitos + 2026) para decidir."""
    up = strip_accents(header_text).upper()
    tokens = re.findall(r"\d{6,14}", re.sub(r"(?<=\d) (?=\d)", "", up))
    for t in templates:
        if not all(k.upper() in up for k in t.get("detect", {}).get("all", [])):
            continue
        if t["numero"].get("strict_regex") and any(re.fullmatch(t["numero"]["regex"], tok) for tok in tokens):
            return t
    return None


def classify_by_barcode(img: Image.Image, header_text: str, templates: list[dict]) -> dict | None:
    """Plano B quando o OCR do cabeçalho erra o nome do convênio: o código de barras
    casa com o padrão de número de um template do mesmo tipo de guia."""
    up = strip_accents(header_text).upper()
    try:
        codes = [c.data.decode(errors="ignore") for c in decode_barcodes(img)]
    except Exception:
        return None
    for t in templates:
        if not t["numero"].get("barcode"):
            continue
        if not all(k.upper() in up for k in t.get("detect", {}).get("all", [])):
            continue
        if any(re.fullmatch(t["numero"]["regex"], c) for c in codes):
            return t
    return None


# --------------------------------------------------------------------------- extração

def read_barcode(img: Image.Image, regex: str) -> str:
    try:
        codes = decode_barcodes(img)
    except Exception:
        return ""
    for c in codes:
        data = c.data.decode(errors="ignore")
        m = re.fullmatch(regex, data) or re.search(regex, data)
        if m:
            return m.group(0)
    return ""


def read_number_ocr(region: Image.Image, regex: str, header_text: str, scale: float = 2.0) -> str:
    regex = rf"(?<!\d)(?:{regex})(?!\d)"
    big = upscale(ImageOps.autocontrast(region), scale)
    for cfg in ("--psm 6 -c tessedit_char_whitelist=0123456789", "--psm 6"):
        text = pytesseract.image_to_string(big, config=cfg)
        digits = re.sub(r"[^\d\n]", "", text)
        for line in digits.splitlines():
            m = re.search(regex, line)
            if m:
                return m.group(0)
    m = re.search(regex, re.sub(r"(?<=\d) (?=\d)", "", header_text))
    return m.group(0) if m else ""


def remove_vertical_lines(img: Image.Image, frac: float = 0.7) -> Image.Image:
    """Apaga bordas verticais dos campos do formulário, que o OCR confunde com 'I' ou '['."""
    a = np.array(img)
    dark = a < 150
    h = dark.shape[0]
    out = a.copy()
    for x in range(dark.shape[1]):
        col = dark[:, x]
        if col.sum() < h * frac:
            continue
        # maior sequência contínua de pixels escuros
        runs = np.diff(np.flatnonzero(np.diff(np.r_[0, col.astype(np.int8), 0])))[::2]
        if runs.size and runs.max() >= h * frac:
            out[:, x] = 255
    return Image.fromarray(out)


def _name_from_line(text: str) -> str:
    for line in text.splitlines():
        words = clean_name(line).split()
        while words and len(words[0]) <= 2:      # lixo de borda no começo ("I", "WI", "[")
            words.pop(0)
        while words and (len(words[-1]) <= 2 or len(set(words[-1])) == 1):  # lixo no fim ("EE", "EEE")
            words.pop()
        name = " ".join(words)
        if len(words) >= 2 and not re.search(r"DADOS|CONTRATAD|SOLICITANTE|SOCIAL|CARTEIRA|NOME|LTDA|CLINICA", name):
            return name
    return ""


def locate_number(region: Image.Image, numero: str, scale: float = 2.0) -> Image.Image | None:
    """Recorte justo em volta do número impresso, para conferência na tela."""
    if not numero:
        return None
    big = upscale(region, scale)
    d = pytesseract.image_to_data(big, config="--psm 11", output_type=pytesseract.Output.DICT)
    for i, w in enumerate(d["text"]):
        digits = re.sub(r"\D", "", w)
        if len(digits) >= 6 and (digits in numero or numero in digits or numero[-6:] in digits):
            h = d["height"][i]
            x0, y0 = d["left"][i], d["top"][i]
            x1, y1 = x0 + d["width"][i], y0 + h
            # inclui o rótulo à esquerda/acima, se houver
            return big.crop((max(0, x0 - 10 * h), max(0, y0 - int(1.5 * h)), min(big.width, x1 + 2 * h), min(big.height, y1 + int(1.5 * h))))
    return None


GENERIC_NAME_LABEL = r"(?:^|\s)\d{0,2}\s*-?\s*Nome\s*$"   # linha que termina em "Nome"


def read_name_anchored(region: Image.Image, label: str, lang: str, scale: float = 1.5) -> tuple[str, Image.Image | None]:
    """Acha o rótulo do campo (ex.: '7 - Nome') e lê só a linha logo abaixo dele."""
    big = upscale(region, scale)
    d = pytesseract.image_to_data(big, lang=lang, config="--psm 6", output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list[int]] = {}
    for i, w in enumerate(d["text"]):
        if w.strip():
            lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(i)
    ordered = list(lines.values())
    texts = [" ".join(d["text"][i] for i in idxs) for idxs in ordered]
    hits = [k for k, t in enumerate(texts) if re.search(label, t, re.I)]
    if not hits and "Social" not in label:
        hits = [k for k, t in enumerate(texts) if re.search(GENERIC_NAME_LABEL, t, re.I)]
    for k in hits:
        idxs = ordered[k]
        nome_idx = [i for i in idxs if re.fullmatch(r"\W*nome\W*", d["text"][i], re.I)]
        ref = nome_idx[0] if nome_idx else idxs[0]
        x0 = min(d["left"][i] for i in idxs)
        h = d["height"][ref]
        y_top = d["top"][ref] + h
        box = (max(0, x0 - int(0.3 * h)), y_top, min(big.width, x0 + int(big.width * 0.8)), min(big.height, y_top + int(h * 3.4)))
        raw = big.crop(box)
        line_img = remove_vertical_lines(raw, frac=0.6)
        for psm in ("6", "7"):
            name = _name_from_line(pytesseract.image_to_string(line_img, lang=lang, config=f"--psm {psm}"))
            if name:
                # recorte para a tela: rótulo + linha do nome
                top = max(0, d["top"][ref] - int(h * 0.6))
                return name, big.crop((box[0], top, box[2], box[3]))
    return "", None


def read_name(region: Image.Image, label: str, lang: str, social_label: str | None, scale: float = 1.5) -> tuple[str, str, Image.Image | None]:
    text = pytesseract.image_to_string(remove_vertical_lines(upscale(region, scale)), lang=lang, config="--psm 6")
    lines = [ln.strip() for ln in text.splitlines()]

    def after(label_rx: str) -> str:
        for i, ln in enumerate(lines):
            if re.search(label_rx, ln, re.I):
                rest = re.sub(label_rx, "", ln, flags=re.I).strip(" :-|")
                cand = [rest] + lines[i + 1:i + 3]
                for c in cand:
                    name = clean_name(c)
                    if len(name.split()) >= 2 and not re.search(r"DADOS|CONTRATAD|SOLICITANTE|SOCIAL|CARTEIRA", name):
                        return name
                return ""
        return ""

    name, crop = read_name_anchored(region, label, lang, scale)
    if not name:
        name = after(label)
    if name:
        return name, "nome", crop
    if social_label:
        name, crop = read_name_anchored(region, social_label, lang, scale)
        if not name:
            name = after(social_label)
        if name:
            return name, "nome_social", crop
    return "", "", None


def check_ans(header: str, ans) -> tuple[bool | None, str]:
    """(True, ans) = ANS esperado encontrado; (False, lido) = outro ANS; (None, "") = não deu para ler."""
    if not ans:
        return None, ""
    expected = [ans] if isinstance(ans, str) else list(ans)
    for a in expected:
        if a in header:
            return True, a
    lines = strip_accents(header).upper().splitlines()
    for i, ln in enumerate(lines):
        if "REGISTRO ANS" in ln:
            for nxt in lines[i:i + 3]:
                found = re.findall(r"\b\d{6}\b", nxt)
                if found:
                    return False, found[0]
    return None, ""


# --------------------------------------------------------------------------- página

def process_page(pdf_path: str, index: int, out_dir: str, hint: int | None = None, config_path: str | None = None) -> dict:
    cfg = load_config(config_path)
    lang = _ocr_lang()
    pdf = Path(pdf_path)
    out = Path(out_dir)
    stem = f"{pdf.stem[:40]}_{index:03d}"

    img = render_page(pdf, index)
    angle, header = detect_orientation(img, lang, hint)
    upright = img.rotate(angle, expand=True) if angle is not None else img

    view = upright.copy()
    view.thumbnail((1400, 1400))
    view.convert("L").save(out / f"{stem}_view.jpg", quality=80)
    thumb = upright.copy()
    thumb.thumbnail((360, 360))
    thumb_name = f"{stem}_thumb.png"
    thumb.save(out / thumb_name)

    tpl = classify(header, cfg["templates"]) if angle is not None else None
    if angle is not None and tpl is None:
        tpl = classify_by_barcode(img, header, cfg["templates"]) or classify_by_number(header, cfg["templates"])
    res = PageResult(
        arquivo=pdf.name, pagina=index, rotacao=angle,
        tipo=tpl["tipo"] if tpl else "anexo",
        template=tpl["id"] if tpl else None,
        convenio=tpl["convenio"] if tpl else None,
        thumb=thumb_name,
        view=f"{stem}_view.jpg",
    )
    if not tpl:
        return res.to_dict()

    ncfg, mcfg = tpl["numero"], tpl["nome"]

    # número: código de barras primeiro, OCR depois
    if ncfg.get("barcode"):
        res.numero = read_barcode(img, ncfg["regex"])
        if res.numero:
            res.numero_fonte = "barcode"
    num_region = crop_rel(upright, ncfg["box"])
    if not res.numero and ncfg.get("barcode"):
        # segunda tentativa: só a região do número, ampliada
        res.numero = read_barcode(upscale(num_region, 2), ncfg["regex"])
        res.numero_fonte = "barcode" if res.numero else ""
    if not res.numero:
        res.numero = read_number_ocr(num_region, ncfg["regex"], header, ncfg.get("scale", 2.0))
        res.numero_fonte = "ocr" if res.numero else ""

    name_region = crop_rel(upright, mcfg["box"])
    res.nome, res.nome_fonte, name_tight = read_name(name_region, mcfg["label"], lang, mcfg.get("social_label"), mcfg.get("scale", 1.5))
    if res.nome_fonte == "nome_social":
        res.alertas.append("Campo Nome vazio na guia; foi usado o Nome Social.")

    num_crop = f"{stem}_num.png"
    name_crop = f"{stem}_nome.png"
    (locate_number(num_region, res.numero, ncfg.get("scale", 2.0)) or num_region).save(out / num_crop)
    (trim_right(name_tight) if name_tight else name_region).save(out / name_crop)
    res.crop_numero, res.crop_nome = num_crop, name_crop

    res.ans_confere, ans_lido = check_ans(header, tpl.get("ans"))
    if res.ans_confere is False:
        res.alertas.append(f"Registro ANS lido ({ans_lido}) diferente do esperado para {tpl['convenio']}. Confira se a guia é desse convênio.")

    # guia em branco (ex.: formulário SP/SADT usado só para escrever o pedido)
    if not res.numero and not res.nome:
        res.alertas.append("Parece um formulário de guia sem dados; tratado como anexo.")
        res.tipo = "anexo_formulario"
    return res.to_dict()


# --------------------------------------------------------------------------- agrupamento

def group_pages(pages: list[dict]) -> list[dict]:
    """Cada guia abre um pacote; páginas seguintes que não são guia entram nele.
    O agrupamento recomeça a cada PDF de origem."""
    packages: list[dict] = []
    current = None
    last_file = None
    for i, p in enumerate(pages):
        is_guia = p["tipo"] in ("consulta", "sadt")
        if p["arquivo"] != last_file:
            current = None
            last_file = p["arquivo"]
        if is_guia or current is None:
            current = {
                "paginas": [i],
                "tipo": p["tipo"] if is_guia else "consulta",
                "convenio": p.get("convenio"),
                "numero": p.get("numero", ""),
                "nome": p.get("nome", ""),
                "sem_guia": not is_guia,
            }
            packages.append(current)
        else:
            current["paginas"].append(i)
    return packages


def file_name(pkg: dict, cfg: dict) -> str:
    pattern = cfg["nomes_arquivo"].get(pkg["tipo"], "{numero} - {nome}")
    name = pattern.format(numero=pkg.get("numero", "").strip(), nome=short_name(pkg.get("nome", "")))
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return name or "sem_nome"
