#!/bin/bash
# PDV Easy Auditoria — Instalador de estação PDV
# Uso: bash <(curl -sSk https://SERVIDOR/install.sh) [opções]
#
# Opções obrigatórias:
#   --token TOKEN         Token API da loja (gerado no painel)
#   --api-url URL         URL do servidor central (ex: https://201.182.184.80:8099)
#   --dvr-host IP         IP do DVR iMHDX
#   --dvr-pass SENHA      Senha do DVR
#
# Opções com defaults:
#   --dvr-user admin      Usuário do DVR
#   --dvr-channel 1       Canal do DVR
#   --station 001         Número da estação PDV
#   --base-dir DIR        Diretório dos spy files (default: /home/rpdv/frente)
#
# Opções opcionais:
#   --gemini-key KEY      Chave API Gemini (para auditoria IA)
#   --groq-key KEY        Chave API Groq (alternativa ao Gemini)
#   --telegram-token TK   Token do bot Telegram
#   --telegram-chat ID    Chat ID do Telegram

set -euo pipefail

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'; B='\033[1m'

# ── Defaults ─────────────────────────────────────────────────────────────────
API_TOKEN=""; API_URL=""; DVR_HOST=""; DVR_PASS=""
DVR_USER="admin"; DVR_CHANNEL="1"; DVR_UDP_PORT="5001"
PDV_STATION="001"; PDV_BASE_DIR="/home/rpdv/frente"
GEMINI_KEY=""; GROQ_KEY=""; TG_TOKEN=""; TG_CHAT=""
INSTALL_DIR="/opt/pdv-visual-auditor"
PYTHON="python3.8"
STEPS=6

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)          API_TOKEN="$2";    shift 2;;
    --api-url)        API_URL="$2";      shift 2;;
    --dvr-host)       DVR_HOST="$2";     shift 2;;
    --dvr-user)       DVR_USER="$2";     shift 2;;
    --dvr-pass)       DVR_PASS="$2";     shift 2;;
    --dvr-channel)    DVR_CHANNEL="$2";   shift 2;;
    --dvr-udp-port)   DVR_UDP_PORT="$2"; shift 2;;
    --station)        PDV_STATION="$2";  shift 2;;
    --base-dir)       PDV_BASE_DIR="$2"; shift 2;;
    --gemini-key)     GEMINI_KEY="$2";   shift 2;;
    --groq-key)       GROQ_KEY="$2";     shift 2;;
    --telegram-token) TG_TOKEN="$2";     shift 2;;
    --telegram-chat)  TG_CHAT="$2";      shift 2;;
    *) echo -e "${RED}Argumento desconhecido: $1${NC}"; exit 1;;
  esac
done

# ── Validação ─────────────────────────────────────────────────────────────────
_req() { [[ -z "${!1}" ]] && { echo -e "${RED}✗ --${2:-$1} é obrigatório${NC}"; exit 1; }; }
_req API_TOKEN token; _req API_URL api-url; _req DVR_HOST dvr-host; _req DVR_PASS dvr-pass

[[ $EUID -ne 0 ]] && { echo -e "${RED}✗ Execute como root (sudo bash ...)${NC}"; exit 1; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "${BLU}${B}"
echo "  ┌─────────────────────────────────────────┐"
echo "  │   PDV Easy Auditoria — Instalador PDV   │"
echo "  └─────────────────────────────────────────┘"
echo -e "${NC}"
printf "  Servidor : %s\n"  "$API_URL"
printf "  DVR      : %s (usuário: %s, canal: %s, UDP overlay: %s)\n" "$DVR_HOST" "$DVR_USER" "$DVR_CHANNEL" "$DVR_UDP_PORT"
printf "  Estação  : PDV %s\n" "$PDV_STATION"
printf "  Dir spy  : %s\n" "$PDV_BASE_DIR"
echo ""

_step() { echo -e "${BLU}[${1}/${STEPS}]${NC} ${B}${2}${NC}"; }
_ok()   { echo -e "    ${GRN}✓ ${1}${NC}"; }
_warn() { echo -e "    ${YLW}⚠ ${1}${NC}"; }
_fail() { echo -e "    ${RED}✗ ${1}${NC}"; exit 1; }

# ── [1] Dependências do sistema ───────────────────────────────────────────────
_step 1 "Verificando dependências do sistema"

command -v "$PYTHON" >/dev/null 2>&1 || _fail "python3.8 não encontrado — instale: apt install python3.8"
command -v curl      >/dev/null 2>&1 || _fail "curl não encontrado — instale: apt install curl"
command -v ffmpeg    >/dev/null 2>&1 || _fail "ffmpeg não encontrado — instale: apt install ffmpeg"

_ok "python3.8 ($($PYTHON --version 2>&1 | awk '{print $2}'))"
_ok "curl     ($(curl --version 2>&1 | head -1 | awk '{print $2}'))"
_ok "ffmpeg   OK"

$PYTHON -m pip install --quiet requests Pillow 2>/dev/null \
  || $PYTHON -m pip install --quiet requests Pillow --break-system-packages 2>/dev/null \
  || _fail "Falha ao instalar requests/Pillow via pip"
_ok "requests + Pillow instalados"

# ── [2] Diretórios ────────────────────────────────────────────────────────────
_step 2 "Criando diretórios"
mkdir -p "$INSTALL_DIR" /var/lib/pdv-visual-auditor /var/log/pdv-visual-auditor
_ok "$INSTALL_DIR"
_ok "/var/lib/pdv-visual-auditor  (dados de auditoria)"

# ── [3] Download dos arquivos ─────────────────────────────────────────────────
_step 3 "Baixando arquivos do servidor"

_dl() {
  local fname="$1"
  curl -sSk --fail \
    -H "Authorization: Bearer ${API_TOKEN}" \
    "${API_URL}/api/install/files/${fname}" \
    -o "${INSTALL_DIR}/${fname}" \
    || _fail "Falha ao baixar ${fname} — verifique token e conectividade"
  chmod 755 "${INSTALL_DIR}/${fname}"
  _ok "$fname"
}

_dl video_streamer.py
_dl pdv_visual_alert_worker.py
_dl pdv_intelbras_bridge.py
_dl pdv_config_loader.py
[[ -n "$TG_TOKEN" ]] && _dl pdv_telegram_assistant.py

# ── [4] Arquivos de configuração ──────────────────────────────────────────────
_step 4 "Criando arquivos de configuração"

# Env principal — lido por todos os serviços
cat > /etc/pdv-telegram-assistant.env <<EOF
AUDITORIA_API_TOKEN=${API_TOKEN}
AUDITORIA_API_URL=${API_URL}
PDV_STATION=${PDV_STATION}
PDV_BASE_DIR=${PDV_BASE_DIR}
GEMINI_API_KEY=${GEMINI_KEY}
GROQ_API_KEY=${GROQ_KEY}
TELEGRAM_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}
EOF
chmod 600 /etc/pdv-telegram-assistant.env
_ok "/etc/pdv-telegram-assistant.env"

# Credenciais do DVR separadas
cat > /etc/pdv-video-streamer.env <<EOF
IMHDX_PASS=${DVR_PASS}
EOF
chmod 600 /etc/pdv-video-streamer.env
_ok "/etc/pdv-video-streamer.env"

# Config da bridge UDP (overlay de texto no DVR)
cat > /etc/pdv-intelbras-bridge.env <<EOF
PDV_STATION=${PDV_STATION}
PDV_SRC_PORT=5000
IMHDX_IP=${DVR_HOST}
IMHDX_PORT=${DVR_UDP_PORT}
EOF
chmod 600 /etc/pdv-intelbras-bridge.env
_ok "/etc/pdv-intelbras-bridge.env"

# ── [5] Serviços systemd ──────────────────────────────────────────────────────
_step 5 "Instalando serviços systemd"

cat > /etc/systemd/system/pdv-video-streamer.service <<EOF
[Unit]
Description=PDV Video Streamer iMHDX
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=5
EnvironmentFile=/etc/pdv-telegram-assistant.env
EnvironmentFile=-/etc/pdv-video-streamer.env
Environment=IMHDX_HOST=${DVR_HOST}
Environment=IMHDX_USER=${DVR_USER}
Environment=IMHDX_CHANNEL=${DVR_CHANNEL}
Environment=VIDEO_STREAMER_PORT=8765
ExecStart=/usr/bin/python3.8 ${INSTALL_DIR}/video_streamer.py
WorkingDirectory=${INSTALL_DIR}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pdv-intelbras-bridge.service <<EOF
[Unit]
Description=PDV Intelbras Bridge (overlay UDP no DVR)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=2
EnvironmentFile=/etc/pdv-intelbras-bridge.env
ExecStart=/usr/bin/python3.8 ${INSTALL_DIR}/pdv_intelbras_bridge.py \
  --station \${PDV_STATION} \
  --src-port \${PDV_SRC_PORT} \
  --dest-ip \${IMHDX_IP} \
  --dest-port \${IMHDX_PORT}
WorkingDirectory=${PDV_BASE_DIR}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pdv-visual-alert-worker.service <<EOF
[Unit]
Description=PDV Visual Alert Worker (auditoria IA)
After=network-online.target pdv-video-streamer.service
Requires=pdv-video-streamer.service

[Service]
Type=simple
Restart=always
RestartSec=5
EnvironmentFile=/etc/pdv-telegram-assistant.env
Environment=VISUAL_ALERT_POLL_SECONDS=1
Environment=VISUAL_ALERT_MIN_VALUE=20
Environment=VISUAL_ALERT_DELAY_SECONDS=6
ExecStart=/usr/bin/python3.8 ${INSTALL_DIR}/pdv_visual_alert_worker.py
WorkingDirectory=${INSTALL_DIR}
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

if [[ -n "$TG_TOKEN" ]]; then
  cat > /etc/systemd/system/pdv-telegram-assistant.service <<EOF
[Unit]
Description=PDV Telegram Assistant Bot
After=network-online.target pdv-video-streamer.service

[Service]
Type=simple
Restart=always
RestartSec=10
EnvironmentFile=/etc/pdv-telegram-assistant.env
ExecStart=/usr/bin/python3.8 ${INSTALL_DIR}/pdv_telegram_assistant.py
WorkingDirectory=${INSTALL_DIR}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload

SVCS=(pdv-video-streamer pdv-intelbras-bridge pdv-visual-alert-worker)
[[ -n "$TG_TOKEN" ]] && SVCS+=(pdv-telegram-assistant)

for svc in "${SVCS[@]}"; do
  systemctl enable --quiet "$svc"
  systemctl restart "$svc"
  sleep 1
  if systemctl is-active --quiet "$svc"; then
    _ok "$svc"
  else
    _warn "$svc iniciou com problema — verifique: journalctl -u $svc -n 30"
  fi
done

# ── [6] Testes de conectividade ───────────────────────────────────────────────
_step 6 "Testando conectividade"

DVR_RESP=$(curl -s --digest -u "${DVR_USER}:${DVR_PASS}" \
  "http://${DVR_HOST}/cgi-bin/global.cgi?action=getCurrentTime" \
  --connect-timeout 5 2>/dev/null || echo "")
if echo "$DVR_RESP" | grep -q "result="; then
  DVR_TIME=$(echo "$DVR_RESP" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]+')
  _ok "DVR iMHDX acessível — hora atual: $DVR_TIME"
else
  _warn "DVR iMHDX não respondeu (${DVR_HOST}) — verifique IP e credenciais"
fi

sleep 3  # aguarda video_streamer subir
API_RESP=$(curl -sSk --connect-timeout 5 \
  -H "Authorization: Bearer ${API_TOKEN}" \
  "${API_URL}/api/v1/health" 2>/dev/null | head -c 20 || echo "")
if [[ -n "$API_RESP" ]] && [[ "$API_RESP" != *"401"* ]]; then
  _ok "Servidor central acessível — ${API_URL}"
else
  _warn "Servidor central não respondeu — verifique conectividade para ${API_URL}"
fi

# ── Resumo ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GRN}${B}"
echo "  ┌──────────────────────────────────────────┐"
echo "  │         Instalação concluída ✓           │"
echo "  └──────────────────────────────────────────┘"
echo -e "${NC}"
echo "  Serviços ativos:"
echo "    • pdv-video-streamer      streaming, snapshots, Gemini IA"
echo "    • pdv-intelbras-bridge    overlay de texto no DVR"
echo "    • pdv-visual-alert-worker auditoria visual automática"
[[ -n "$TG_TOKEN" ]] && echo "    • pdv-telegram-assistant  bot de alertas"
echo ""
echo "  Dashboard: ${API_URL}"
echo ""
echo "  Verificar logs:"
echo "    journalctl -u pdv-video-streamer -f"
echo ""
if [[ -z "$TG_TOKEN" ]]; then
  echo -e "  ${YLW}Telegram não configurado. Ative em: Dashboard > Configurações > Notificações${NC}"
  echo ""
fi
if [[ -z "$GEMINI_KEY" ]] && [[ -z "$GROQ_KEY" ]]; then
  echo -e "  ${YLW}Sem chave IA. Configure em: Dashboard > Configurações > Auditoria IA${NC}"
  echo ""
fi
