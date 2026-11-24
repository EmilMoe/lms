# Mock af Blackboard Learn (REST-API) og Anthology Developer Portal (LTI 1.3). Se app.py.
FROM python:3.12-slim
RUN pip install --no-cache-dir "cryptography==43.0.3"
WORKDIR /app
COPY app.py .
RUN useradd --system --uid 10001 bb && mkdir /data && chown bb /data
USER bb
ENV PYTHONUNBUFFERED=1 BB_DATA_DIR=/data BB_SEED_DIR=/seed
EXPOSE 8080
CMD ["python", "app.py"]
