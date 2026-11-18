# Mock af Canvas Data 2 (DAP Query API) til test-Canvas. Se app.py.
FROM python:3.12-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.2.3"
WORKDIR /app
COPY app.py .
RUN useradd --system --uid 10001 dap && mkdir /data && chown dap /data
USER dap
ENV PYTHONUNBUFFERED=1 DAP_DATA_DIR=/data
EXPOSE 8080
CMD ["python", "app.py"]
