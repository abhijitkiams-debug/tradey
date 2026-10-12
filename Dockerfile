FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Kolkata
RUN useradd --create-home --uid 10001 trader
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[server,groww,agent]" && mkdir -p /data && chown trader /data
# relative paths in the config (db_path, cache_dir) resolve inside the /data volume
WORKDIR /data
USER trader
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
ENTRYPOINT ["claudetrade", "serve", "--host", "0.0.0.0", "--port", "8000"]
CMD ["--config", "/config/claudetrade.toml"]
