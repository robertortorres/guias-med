# Separador de guias

Recebe PDFs escaneados de guias de convênio, separa em pacotes (guia + anexos) e gera um ZIP
com um PDF por pacote, nomeado no padrão do faturamento:

| Tipo de guia | Nome do arquivo |
|---|---|
| Consulta | `NºGuia - Primeiro Último.pdf` |
| SP/SADT (pedido médico) | `NºGuia - Pedido Medico - Primeiro Último.pdf` |

Tudo roda localmente, em CPU. Nenhum dado de paciente sai do servidor.

## Como funciona

1. **Upload** de um ou mais PDFs (cada PDF é separado de forma independente).
2. **Leitura de cada página**, em paralelo:
   - orientação (guias vêm deitadas: testa 90° e 270°);
   - tipo: guia de consulta, guia SP/SADT ou anexo (pedido, laudo, formulário em branco);
   - convênio, pelo texto do cabeçalho, código de barras ou formato do número;
   - **número da guia**: código de barras primeiro (GEAP, Bradesco), OCR como reserva;
   - **nome do beneficiário**: OCR da linha abaixo do rótulo "Nome"; se estiver vazio, usa o Nome Social.
3. **Agrupamento**: cada guia abre um pacote; as páginas seguintes que não são guia entram nele.
   Isso cobre "só guia", "guia + pedido" e "guia + pedido + laudo" sem precisar escolher nada.
4. **Revisão** na tela: recorte da guia ao lado de cada campo editável, alertas, "Dividir aqui" e
   "Juntar com o anterior" para corrigir a separação.
5. **Gerar ZIP**: monta os PDFs a partir das páginas originais (qualidade do scan preservada) e
   registra os números no histórico.

### Alertas

- número ou nome não lido;
- número repetido no mesmo lote;
- **guia já exportada em outro lote** (histórico em SQLite) — evita cobrança em duplicidade;
- Registro ANS diferente do esperado para o convênio;
- Nome Social usado porque o campo Nome está vazio;
- formulário de guia sem dados (tratado como anexo).

## Subir no Proxmox (LXC ou VM com Docker)

```bash
git clone … separador-guias && cd separador-guias
cp .env.example .env            # defina usuário e senha
docker compose up -d --build
```

Acesse `http://<ip>:8000`. Recomendado: 4 vCPU, 4–8 GB de RAM, sem GPU.
Para HTTPS, coloque atrás do proxy reverso que já existe (Nginx Proxy Manager etc.).

### Variáveis

| Variável | Padrão | Para quê |
|---|---|---|
| `WORKERS` | nº de CPUs | processos de OCR em paralelo |
| `RETENTION_DAYS` | 7 | lotes (PDFs, imagens) são apagados depois disso |
| `MAX_UPLOAD_MB` | 200 | limite por envio |
| `APP_USER` / `APP_PASSWORD` | vazio | login básico; vazio = sem login (só em rede interna) |
| `CONVENIOS_YAML` | `config/convenios.yaml` | templates dos convênios |
| `TRY_ALL_ROTATIONS` | vazio | também testa 0°/180° (só se aparecer guia escaneada em pé) |

O histórico de guias exportadas fica em `data/historico.sqlite3` e **não** é apagado pela limpeza
(guarda só número, convênio, nome, lote e data).

## Convênio novo ou layout que mudou

Edite `config/convenios.yaml` e reinicie (`docker compose restart`). Cada template tem:

- `detect`: palavras do cabeçalho que identificam o convênio e o tipo de guia;
- `numero.regex`: formato do número (ex.: Bradesco `19\d{8}`, Porto consulta `\d{8}20[2-3]\d`);
- `numero.box` / `nome.box`: região da página (coordenadas 0–1 com a guia já na posição de leitura);
- `numero.barcode`: se a guia tem código de barras com o número;
- `nome.label`: rótulo do campo (ex.: `7 - Nome`, `10 - Nome`).

Para testar um template sem a tela:

```bash
docker compose exec guias python -m app.cli /data/teste.pdf --paginas 10
```

Mostra o que foi lido em cada página e os pacotes que seriam gerados.

## Convênios já mapeados

| Template | Número | Origem do número |
|---|---|---|
| Bradesco consulta | 10 dígitos (`19…`) | código de barras |
| GEAP consulta | 8 dígitos (`1…`) | código de barras |
| GEAP SP/SADT | 8 dígitos (`1…`) | código de barras |
| Porto SP/SADT | 8 dígitos | OCR |
| Porto / Porto Atender consulta | 12 dígitos terminando no ano (`…2026`) | OCR |

Observação: as guias Bradesco vêm com Registro ANS 005711 ou 421715; os dois estão cadastrados.
O número da Porto consulta termina no ano; o padrão aceita de 2020 a 2039.

## Estrutura

```
app/
  pipeline.py   leitura das páginas (orientação, classificação, extração, agrupamento)
  lotes.py      lotes em disco, processamento em segundo plano, exportação do ZIP
  historico.py  SQLite com as guias exportadas
  main.py       rotas FastAPI
  cli.py        teste de leitura pela linha de comando
  templates/    páginas HTML
  static/       CSS e JS da tela de revisão
config/convenios.yaml
```
