#!/usr/bin/env bash
# Entrypoint del worker en el VPS.
# 1) Construye la imagen del sandbox en el daemon del HOST (via socket montado).
# 2) Fija VUPPIA_SANDBOX_DIGEST al id real de esa imagen (obligatorio: sin digest
#    valido el codigo no confiable NO se ejecuta).
# 3) Arranca el loop autonomo 24/7 con freno por archivo VUPPIA_STOP.
set -euo pipefail

export DOCKER_HOST="unix:///var/run/docker.sock"
echo "[vuppia] esperando acceso al Docker del host..."
ok=0
for i in $(seq 1 30); do
  if docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then ok=1; break; fi
  echo "[vuppia]   intento $i/30: Docker del host aun no disponible, reintento en 3s..."
  sleep 3
done
if [ "$ok" != "1" ]; then
  echo "[vuppia] ERROR: no hay acceso al daemon Docker del host (socket) tras 30 intentos."; exit 1; fi
echo "[vuppia] Docker del host OK."

echo "[vuppia] construyendo imagen del sandbox aislado..."
docker build -q -t vuppia-sandbox:3 /app/sandbox

DIGEST="$(docker image inspect vuppia-sandbox:3 --format '{{.Id}}')"
export VUPPIA_SANDBOX_DIGEST="$DIGEST"
echo "[vuppia] sandbox fijado en digest: $DIGEST"

# Freno de emergencia: si existe /app/data/VUPPIA_STOP el loop se detiene solo.
INTERVAL="${VUPPIA_INTERVAL:-900}"
echo "[vuppia] arrancando runner autonomo (intervalo ${INTERVAL}s)..."
exec python cli.py autonomous \
  --db /app/data/vuppia.sqlite3 \
  --interval "${INTERVAL}" \
  --stop-file /app/data/VUPPIA_STOP
