FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
# Optional build secret `buildca`: a PEM bundle for networks that re-sign TLS (see docker-compose.sandbox.yml).
RUN --mount=type=secret,id=buildca,required=false \
    if [ -f /run/secrets/buildca ]; then export PIP_CERT=/run/secrets/buildca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY *.py ./
RUN useradd --system --no-create-home gateway
USER gateway
EXPOSE 8088
CMD ["python", "main.py"]
