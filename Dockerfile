# Secrets stay outside the image. Pass SMTP_* and ETL_ENV at runtime.
# Do not COPY .env, key files, logs, or local databases into this image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp \
    ETL_CONFIG=/app/config.yaml \
    ETL_ENV=production

WORKDIR /app

RUN groupadd --gid 1000 etl \
    && useradd --uid 1000 --gid etl --home-dir /tmp --shell /usr/sbin/nologin --no-create-home etl

COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt \
    && rm -rf /root/.cache

COPY --chown=etl:etl etl ./etl
COPY --chown=etl:etl config.yaml ./config.yaml
COPY --chown=etl:etl data/samples ./data/samples

# Application files are readable and not writable. Runtime directories are
# writable by the etl user only. Bind mounts replace data, logs, and output.
RUN mkdir -p data/inbox data/archive logs output/reports \
    && chown -R etl:etl /app \
    && chmod -R a-w,a+rX /app \
    && chmod -R u+w data logs output

USER etl

CMD ["python", "-m", "etl.scheduler"]
