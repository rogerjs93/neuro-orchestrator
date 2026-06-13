FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Debian's docker.io package in this base image does not ship the docker CLI binary.
# Install the official static CLI so pipeline subprocess calls to `docker` work.
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-29.5.2.tgz -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz

WORKDIR /workspace

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/

ENV PYTHONPATH=/workspace
ENV DATA_DIR=/data
ENV OUTPUT_DIR=/outputs
ENV FS_LICENSE=/licenses/license.txt
ENV MOCK_MODE=0

EXPOSE 8080

CMD ["python", "src/web_server.py"]
