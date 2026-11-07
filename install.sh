#!/usr/bin/env bash
# Instala o serviço local do YouTube -> FLAC e prepara a extensão para o Vivaldi.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$ROOT/server/ytflac_server.py"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/ytflac.service"
OUTDIR="$HOME/Música/YouTube FLAC"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }

log "Verificando dependências"
command -v python3 >/dev/null || { warn "python3 não encontrado"; exit 1; }
if ! command -v ffmpeg >/dev/null; then
  warn "ffmpeg não encontrado. Instale com: sudo pacman -S ffmpeg"
  exit 1
fi
ok "python3 e ffmpeg presentes"

mkdir -p "$BIN_DIR"

# O yt-dlp roda numa venv propria: ela traz o `mutagen` e os extras que o
# yt-dlp usa para embutir titulo/artista/capa no FLAC.
VENV_DIR="$HOME/.local/share/ytflac/venv"
if [ ! -x "$VENV_DIR/bin/yt-dlp" ]; then
  log "Criando a venv do yt-dlp em $VENV_DIR"
  python3 -m venv "$VENV_DIR" >/dev/null 2>&1 || true
  if [ -x "$VENV_DIR/bin/pip" ]; then
    "$VENV_DIR/bin/pip" install -q -U "yt-dlp[default]" || warn "pip falhou; tento o binário avulso"
  fi
fi

if [ -x "$VENV_DIR/bin/yt-dlp" ]; then
  ok "yt-dlp (venv): $("$VENV_DIR/bin/yt-dlp" --version)"
else
  warn "venv indisponível; usando binário avulso (sem capa/metadados embutidos)"
  if ! command -v yt-dlp >/dev/null && [ ! -x "$BIN_DIR/yt-dlp" ]; then
    log "Baixando yt-dlp para $BIN_DIR"
    curl -fsSL --max-time 120 \
      -o "$BIN_DIR/yt-dlp" \
      "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
    chmod +x "$BIN_DIR/yt-dlp"
  fi
  ok "yt-dlp: $("$BIN_DIR/yt-dlp" --version 2>/dev/null || yt-dlp --version)"
fi

mkdir -p "$OUTDIR"
ok "pasta de destino: $OUTDIR"

log "Instalando o serviço (systemd --user)"
mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=YouTube -> FLAC (servico local)
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 "$SERVER"
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
WorkingDirectory=$ROOT

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now ytflac.service
sleep 1
if systemctl --user is-active --quiet ytflac.service; then
  ok "serviço ativo em http://127.0.0.1:8765"
else
  warn "o serviço não subiu. Veja: journalctl --user -u ytflac -n 50"
fi

if command -v curl >/dev/null; then
  curl -fsS --max-time 3 http://127.0.0.1:8765/api/health >/dev/null \
    && ok "API respondendo" \
    || warn "API não respondeu (veja o journal)"
fi

cat <<'EOF'

──────────────────────────────────────────────────────────────
 Carregar a extensão no Vivaldi
──────────────────────────────────────────────────────────────
 1. Abra:   vivaldi://extensions
 2. Ligue "Modo do desenvolvedor" (canto superior direito)
 3. Clique em "Carregar sem compactação"
 4. Escolha a pasta:
EOF
printf '      %s\n' "$ROOT/extension"
cat <<'EOF'

 Pronto: abra um vídeo do YouTube e clique no ícone da extensão.
──────────────────────────────────────────────────────────────
EOF
