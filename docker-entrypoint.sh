#!/bin/sh
# Ajusta a permissão da pasta de dados (volume) e roda o app como usuário sem privilégio.
set -e
mkdir -p /data
chown -R guias:guias /data 2>/dev/null || echo "aviso: não consegui ajustar a permissão de /data"
exec setpriv --reuid=guias --regid=guias --init-groups "$@"
