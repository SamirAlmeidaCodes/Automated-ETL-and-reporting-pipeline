FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    ETL_CONFIG=/app/config.yaml

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY etl ./etl
COPY config.yaml ./config.yaml
COPY data/samples ./data/samples

RUN mkdir -p data/inbox data/archive logs output/reports \
    && chmod 0777 data data/inbox data/archive data/samples logs output output/reports

CMD ["python", "-m", "etl.scheduler"]
