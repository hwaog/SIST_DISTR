# Imagen única para todos los roles (ControlNode, DataNode, cliente, PKI).
# El rol se elige con el comando:  dfsha-control | dfsha-data | dfsha | dfsha-pki
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Usuario sin privilegios; /data es el único volumen de escritura del nodo.
RUN useradd --system --uid 10001 --create-home dfsha \
 && mkdir -p /data /pki /secrets \
 && chown -R dfsha:dfsha /data
# El healthcheck del compose; se fuerza el bit de ejecución para no depender del que traiga el repositorio.
COPY docker/healthcheck.py /usr/local/bin/dfsha-healthcheck
RUN chmod 755 /usr/local/bin/dfsha-healthcheck
USER dfsha
VOLUME ["/data"]
