# easy-pdv-auditoria — Contexto do Sistema

## O que é este projeto

Sistema de auditoria visual de cupons para supermercado. Quando o caixa escaneia
um produto, captura frame da câmera DVR naquele momento e usa Gemini Vision para
identificar o item. Compara com o PDV — divergências geram alertas.

Objetivo real: identificar erros humanos (item extra, erro de estoque, duplo
escaneamento) e suspeitas de fraude. A maioria é erro humano, não roubo.

---

## Arquitetura

```
[PDV1 — caixa registradora]          [Servidor central 10.10.12.7]
  video_streamer.py (porta 8765)  ->  app.py (FastAPI, porta 8099)
  pdv_intelbras_bridge.py             PostgreSQL 17 (Docker)
         |                                    |
  DVR iMHDX (192.168.24.227)          dashboard/ (nginx, porta 80)
  Gemini Vision API (Google)
```

Fluxo: PDV escaneia item -> spy file atualiza -> video_streamer detecta bip ->
captura frame do DVR -> envia para Gemini -> compara com PDV -> salva na API
central -> exibe no dashboard.

---

## Componentes e localizações

| Componente         | Arquivo neste repo                    | Onde roda              |
|--------------------|---------------------------------------|------------------------|
| API central        | app.py                                | Docker em 10.10.12.7   |
| Dashboard web      | dashboard/                            | Docker em 10.10.12.7   |
| Servidor PDV       | pdv_files/video_streamer.py           | PDV1 138.99.28.216:2289|
| Bridge UDP->DVR    | pdv_files/pdv_intelbras_bridge.py     | PDV1                   |
| Bot Telegram       | pdv_files/pdv_telegram_assistant.py   | PDV1                   |

Credenciais de acesso: consultar a memória do Claude em
~/.claude/projects/c--PROJETOS-Mikrotik/memory/

---

## Deploy

### PDV1 — video_streamer.py
```powershell
& "C:\Program Files\PuTTY\pscp" -pw "<SENHA_PDV1>" -P 2289 `
  pdv_files\video_streamer.py `
  rpdv@138.99.28.216:/opt/pdv-visual-auditor/video_streamer.py

& "C:\Program Files\PuTTY\plink" -batch -ssh -pw "<SENHA_PDV1>" -P 2289 `
  rpdv@138.99.28.216 "ps aux | grep video_streamer | grep -v grep"
```

ATENCAO: processo roda como root. rpdv nao tem sudo sem senha.
Para reiniciar precisa de acesso root direto ou reboot.

### Servidor central (10.10.12.7)
```bash
ssh central@10.10.12.7
cd /opt/easy-auditoria-api
docker compose up -d --build
```

---

## Variáveis de ambiente (PDV1)

Em /opt/pdv-visual-auditor/.env no PDV1. Nunca commitar valores reais.

```
IMHDX_HOST=192.168.24.227
IMHDX_USER=admin
IMHDX_PASS=****
IMHDX_CHANNEL=1
GEMINI_API_KEY=****
AUDITORIA_API_URL=https://201.182.184.80:8099
AUDITORIA_API_TOKEN=****
PDV_STATION=001
VIDEO_STREAMER_PORT=8765
PDV_BASE_DIR=/home/rpdv/frente
```

---

## Problemas ativos / decisoes tecnicas

### DVR timing drift
- DVR tem relogio independente, oscila +-2s em relacao ao PDV
- FIX: _calibrate_dvr_offset() chamado no inicio de cada _gemini_analyze_cupom
  (mede offset fresh antes de analisar, nao usa cache de 3 min)
- Janela de captura: (-2, -1, 0, 1, 2) — 5 frames cobrindo drift residual
- Re-analise historica: offset atual nao reflete o passado, resultados imprecisos

### Falsos positivos
- Gemini as vezes ve item anterior ainda na esteira
- ROI ja foi ajustado (nao mexer sem necessidade)
- Taxa esperada com timing correto: ~20% suspeitos

### ON CONFLICT na API central
- Era DO NOTHING -> re-analise nao atualizava resultados
- FIX: DO UPDATE SET em app.py na funcao criar_evento

### /audit-range-status endpoint
- Estava em do_POST mas dashboard consulta via GET -> retornava 400
- FIX: movido para do_GET no video_streamer.py

---

## Spy file (log do PDV)

Arquivo diario: /home/rpdv/frente/EspiaoYYMMDD.001
Software PDV: Sysmo Veraz v4.14.47
Eventos: FECHACUPOM (aciona analise), CANCELA (ignorar)
video_streamer.py le continuamente (tail) para detectar novos bips.

---

## Stack

- Python 3.8 (PDV1 — versao fixada pelo SO)
- FastAPI + PostgreSQL 17 (servidor central)
- Gemini 2.0 Flash (analise de imagem via GEMINI_API_KEY)
- ffmpeg (extracao de frames do DVR)
- Docker + nginx (servidor central)
- JavaScript vanilla (dashboard — sem framework)
- Telegram Bot API (notificacoes)

---

## IPs de referencia

| Host             | IP                    |
|------------------|-----------------------|
| PDV1             | 138.99.28.216:2289    |
| DVR iMHDX        | 192.168.24.227        |
| Servidor central | 10.10.12.7            |
| API central      | 201.182.184.80:8099   |

---

## Notas importantes

- dashboard/config.js tem tokens reais — no .gitignore, nunca commitar
- pdv_files/video_streamer.py e a versao canonica (a de pdv1-integration/ no
  repo antigo easytecnologias/easy-auditoria-api e obsoleta)
- PDV8 (outro supermercado, rede 192.168.4.x) — integracao futura planejada
