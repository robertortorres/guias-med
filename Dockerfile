FROM python:3.12-slim

# OCR em português + leitura de código de barras
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por libzbar0 tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

# OMP_THREAD_LIMIT=1: cada processo do pool usa 1 thread do Tesseract (paralelismo vem do pool)
ENV DATA_DIR=/data \
    OMP_THREAD_LIMIT=1 \
    PYTHONUNBUFFERED=1
    TZ=America/Sao_Paulo

RUN useradd -r -u 1000 guias && mkdir -p /data && chown guias /data
COPY docker-entrypoint.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/saude')"

# 1 worker do uvicorn: os lotes rodam numa thread + pool de processos dentro dele
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
