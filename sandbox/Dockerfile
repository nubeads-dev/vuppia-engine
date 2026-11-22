# Imagen del sandbox de VUPPIA SOFÍA 3. Versiones fijadas.
# Construir SOLO por el operador:  docker build -t vuppia-sandbox:3 worker/sandbox
# Para máxima reproducibilidad, reemplazar el tag por su digest (python:3.12-alpine@sha256:...).
FROM python:3.12-alpine
RUN pip install --no-cache-dir numpy==2.1.3 && \
    rm -rf /root/.cache
USER 65534:65534
WORKDIR /tmp
