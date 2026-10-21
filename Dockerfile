FROM python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY radar ./radar
COPY scripts/check_sources.py ./scripts/check_sources.py
COPY config/assessment.schema.json ./config/assessment.schema.json
RUN useradd --uid 10001 --no-create-home radar
USER 10001
CMD ["python", "-m", "radar.main"]
