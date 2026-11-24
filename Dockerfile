FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY openp6 ./openp6
COPY web ./web
COPY sample.xer ./sample.xer
COPY sample_multi.xer ./sample_multi.xer

# Render/Fly/Cloud Run inject PORT; default 8080 locally
ENV OPENP6_PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz')" || exit 1

CMD ["python", "web/server.py"]
