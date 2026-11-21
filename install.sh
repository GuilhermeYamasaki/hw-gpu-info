#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP_FILE="$APPS_DIR/hw-gpu-monitor.desktop"

ENABLE_AUTOSTART=false
for arg in "$@"; do
    case "$arg" in
        --autostart)
            ENABLE_AUTOSTART=true
            ;;
        -h|--help)
            echo "Uso: ./install.sh [--autostart]"
            echo "  --autostart  Tambem configura o app para iniciar com o sistema"
            exit 0
            ;;
        *)
            echo "Opcao desconhecida: $arg" >&2
            echo "Uso: ./install.sh [--autostart]" >&2
            exit 1
            ;;
    esac
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "Erro: python3 nao encontrado no sistema." >&2
    exit 1
fi

echo "==> Preparando ambiente virtual em $VENV_DIR..."
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi

echo "==> Instalando dependencias (PySide6)..."
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

chmod +x "$SCRIPT_DIR/run.sh"

echo "==> Criando atalho no menu do sistema..."
mkdir -p "$APPS_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=HW GPU Monitor
Comment=Monitor de temperatura, clock e fan da GPU AMD
Exec=$SCRIPT_DIR/run.sh
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
EOF

if [[ "$ENABLE_AUTOSTART" == true ]]; then
    echo "==> Ativando inicializacao automatica..."
    mkdir -p "$AUTOSTART_DIR"
    cp "$DESKTOP_FILE" "$AUTOSTART_DIR/hw-gpu-monitor.desktop"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

echo ""
echo "Instalacao concluida!"
echo "Voce ja pode abrir o 'HW GPU Monitor' pelo menu do sistema ou rodar: $SCRIPT_DIR/run.sh"
