#!/usr/bin/env bash
# Instala o serviço local do YouTube/Spotify -> MP3 e prepara a extensão para o Vivaldi.
#
# Uso:
#   bash install.sh                  # terminal (padrão)
#   bash install.sh --gui            # tenta usar zenity/kdialog/dialog se disponíveis
#
# Se uma ferramenta gráfica (zenity, kdialog ou dialog) estiver instalada, o
# script usa-a para mostrar progresso; caso contrário, tudo é feito no terminal.
# Nenhuma dessas ferramentas é obrigatória.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$ROOT/server/ytflac_server.py"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/ytflac.service"
OUTDIR="$HOME/Música/YouTube FLAC"
GUI_MODE=0
[ "${1:-}" = "--gui" ] && GUI_MODE=1

# ----------------------------------------------------------------------- GUI helpers
# Detecta uma ferramenta gráfica disponível (opcional).
GUI_TOOL=""
for t in zenity kdialog dialog; do
  if command -v "$t" >/dev/null 2>&1; then GUI_TOOL="$t"; break; fi
done

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }

# Mensagem (terminal + GUI se disponível)
say() {
  log "$*"
  if [ "$GUI_MODE" = 1 ] && [ -n "$GUI_TOOL" ]; then
    case "$GUI_TOOL" in
      zenity)  zenity --info --title="BetterMusicDownloader" --text="$*" --width=420 2>/dev/null || true ;;
      kdialog) kdialog --msgbox "$*" 2>/dev/null || true ;;
      dialog)  dialog --msgbox "$*" 10 60 2>/dev/null || true ;;
    esac
  fi
}

# Progresso (apenas GUI, silencioso no terminal)
progress() {
  if [ "$GUI_MODE" = 1 ] && [ -n "$GUI_TOOL" ]; then
    case "$GUI_TOOL" in
      zenity)  zenity --progress --title="BetterMusicDownloader" --text="$1" --pulsate --auto-close --width=420 2>/dev/null || true ;;
    esac
  fi
}

# ----------------------------------------------------------------------- dependencias
log "Verificando dependências"
command -v python3 >/dev/null || { warn "python3 não encontrado"; exit 1; }

# ffmpeg: tenta instalar automaticamente se faltar
if ! command -v ffmpeg >/dev/null; then
  warn "ffmpeg não encontrado. Tentando instalar automaticamente..."
  if command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm ffmpeg || { warn "falhou: sudo pacman -S ffmpeg"; exit 1; }
  elif command -v apt-get >/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y ffmpeg || { warn "falhou: sudo apt install ffmpeg"; exit 1; }
  elif command -v dnf >/dev/null; then
    sudo dnf install -y ffmpeg || { warn "falhou: sudo dnf install ffmpeg"; exit 1; }
  else
    warn "Não detectei gerenciador de pacotes. Instale o ffmpeg manualmente."
    exit 1
  fi
fi
ok "python3 e ffmpeg presentes"

# ----------------------------------------------------------------------- yt-dlp venv
mkdir -p "$BIN_DIR"

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

# ----------------------------------------------------------------------- systemd
log "Instalando o serviço (systemd --user)"
mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=YouTube -> MP3 (servico local)
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

# ----------------------------------------------------------------------- final
say "Instalação do serviço concluída!"

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

# Tenta abrir o Vivaldi na página de extensões automaticamente
if command -v vivaldi >/dev/null 2>&1; then
  log "Abrindo o Vivaldi em vivaldi://extensions ..."
  vivaldi "vivaldi://extensions" >/dev/null 2>&1 &
elif command -v vivaldi-stable >/dev/null 2>&1; then
  vivaldi-stable "vivaldi://extensions" >/dev/null 2>&1 &
fi
EOF
chmod +x install.sh
echo "install.sh atualizado"