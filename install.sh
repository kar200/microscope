#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"

mkdir -p "${BIN_DIR}" "${APP_DIR}"

if [[ ! -d "${REPO_DIR}/.deps" ]]; then
    echo "Installing Python dependencies into ${REPO_DIR}/.deps ..."
    if command -v uv >/dev/null 2>&1; then
        uv pip install --target "${REPO_DIR}/.deps" opencv-python
    else
        python3 -m pip install --target "${REPO_DIR}/.deps" opencv-python
    fi
fi

cat > "${BIN_DIR}/bresun-microscope" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${REPO_DIR}/.deps:${REPO_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}"
export QT_LOGGING_RULES="qt.qpa.wayland.textinput=false"
exec python3 -m microscope_app "\$@"
EOF
chmod +x "${BIN_DIR}/bresun-microscope"

cat > "${APP_DIR}/microscope.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Microscope
Comment=Live 4K view, focus control & measurement — Bresun HH-4K MAX
Exec=${BIN_DIR}/bresun-microscope
Path=${REPO_DIR}
Icon=camera-video
Terminal=false
Categories=AudioVideo;Video;Science;
EOF

echo "Installed launcher: ${BIN_DIR}/bresun-microscope"
echo "Installed desktop entry: ${APP_DIR}/microscope.desktop"
