FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    FLOWITH_API_HOST=0.0.0.0 \
    FLOWITH_API_PORT=8787

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy/ ./proxy/

RUN adduser --disabled-password --gecos "" --home /home/proxy proxy \
    && chown -R proxy:proxy /app
USER proxy

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://127.0.0.1:8787/health').raise_for_status()"

CMD ["python", "-m", "proxy"]
