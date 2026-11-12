"""Testa a leitura de um PDF sem passar pela tela (útil para ajustar templates).

    python -m app.cli lote.pdf                 # mostra o que foi lido em cada página
    python -m app.cli lote.pdf --zip saida.zip # gera o ZIP direto, sem revisão
"""
import argparse
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pymupdf

from .config import WORKERS, load_config
from .pipeline import file_name, group_pages, process_page


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--zip", type=Path, help="gera o ZIP sem revisão")
    ap.add_argument("--paginas", type=int, help="processa só as N primeiras páginas")
    args = ap.parse_args()

    with pymupdf.open(args.pdf) as doc:
        n = doc.page_count if not args.paginas else min(args.paginas, doc.page_count)
    with tempfile.TemporaryDirectory() as tmp, ProcessPoolExecutor(WORKERS) as ex:
        first = process_page(str(args.pdf), 0, tmp)
        rest = list(ex.map(process_page, [str(args.pdf)] * (n - 1), range(1, n), [tmp] * (n - 1), [first["rotacao"]] * (n - 1)))
    pages = [first] + rest

    for p in pages:
        print(f"p{p['pagina'] + 1:>3}  {p['tipo']:<16} {p['template'] or '-':<18} {p['numero'] or '-':<14} "
              f"{p['numero_fonte'] or '':<8} {p['nome'] or '-'}  {' | '.join(p['alertas'])}")
    cfg = load_config()
    pacotes = group_pages(pages)
    print(f"\n{len(pacotes)} pacotes:")
    for p in pacotes:
        print(f"  {len(p['paginas'])} pág.  {file_name(p, cfg)}.pdf")

    if args.zip:
        import zipfile
        with pymupdf.open(args.pdf) as src, zipfile.ZipFile(args.zip, "w", zipfile.ZIP_DEFLATED) as z:
            for p in pacotes:
                out = pymupdf.open()
                for i in p["paginas"]:
                    out.insert_pdf(src, from_page=pages[i]["pagina"], to_page=pages[i]["pagina"])
                z.writestr(f"{file_name(p, cfg)}.pdf", out.tobytes(garbage=3, deflate=True))
        print(f"\nZIP salvo em {args.zip}")


if __name__ == "__main__":
    main()
