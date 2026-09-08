# Пульт управления PZ-сервером. Внутри — docker CLI + compose + python3.
FROM python:3.12-alpine

# docker-cli и docker-cli-compose нужны для управления соседним контейнером,
# tar — для бэкапов, tzdata — для корректного времени.
RUN apk add --no-cache docker-cli docker-cli-compose tar tzdata

WORKDIR /app
# новые модули пульта добавлять сюда же (или заменить на COPY *.py ./)
COPY config.py rcon.py dockerlib.py notify.py ops.py app.py healthcheck.py ./
COPY static ./static

EXPOSE 8080
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"
CMD ["python3", "app.py"]
