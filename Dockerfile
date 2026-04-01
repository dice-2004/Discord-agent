FROM python:3.11-slim

ARG INSTALL_GEMINI_CLI=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN if [ "$INSTALL_GEMINI_CLI" = "true" ]; then \
            apt-get update && \
            apt-get install -y --no-install-recommends nodejs npm ca-certificates && \
            npm install -g @google/gemini-cli && \
            npm cache clean --force && \
            apt-get clean && \
            rm -rf /var/lib/apt/lists/*; \
        fi

COPY src /app/src
COPY data /app/data

CMD ["python", "-m", "main_agent.main"]
