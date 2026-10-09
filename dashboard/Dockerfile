# PZ Console: Python, Docker CLI, and Compose for the sibling game container.
ARG PYTHON_BASE_IMAGE=python:3.12-alpine
FROM ${PYTHON_BASE_IMAGE}

# tar creates world backups; tzdata supplies container time; Lua checks syntax.
RUN apk add --no-cache docker-cli docker-cli-compose tar tzdata lua5.1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY schemas ./schemas
COPY static ./static

EXPOSE 8080
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python3 healthcheck.py
CMD ["python3", "app.py"]
