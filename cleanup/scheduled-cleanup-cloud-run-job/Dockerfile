# Cloud Run Job image that runs the detectors on a schedule and publishes
# the report. Live collection uses Application Default Credentials from the
# job's dedicated service account; no keys are baked into the image.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY detectors ./detectors
COPY cleanup/scheduled-cleanup-cloud-run-job/main.py ./main.py

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin finops \
    && mkdir -p /tmp/out && chown -R finops:finops /tmp/out
USER 10001

ENTRYPOINT ["python", "main.py"]
