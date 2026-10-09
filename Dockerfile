FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt constraints.txt ./
# Constraints pin safe versions of indirect dependencies; setuptools in the
# base image is upgraded too (Trivy, CVE-2025-47273).
RUN pip install --no-cache-dir --upgrade "setuptools>=78.1.1" && pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY wakeel wakeel
COPY corpus corpus
COPY web web
RUN useradd --uid 10001 --create-home wakeel && mkdir -p .cache .data && chown wakeel .cache .data
ENV WAKEEL_DB=/app/.data/wakeel.db
VOLUME /app/.data
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"
CMD ["sh", "-c", "uvicorn wakeel.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
