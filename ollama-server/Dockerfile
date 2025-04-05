FROM debian:bookworm-slim

WORKDIR /app

# --------------------------
# Install Ollama and Python
# --------------------------
RUN apt-get update && \
    apt-get install -y curl gnupg openssl lynx python3 python3-pip bash && \
    rm -rf /var/lib/apt/lists/*

# Install Ollama (official CLI install)
RUN curl -fsSL https://ollama.com/install.sh | bash

# Add ollama to PATH (in case needed explicitly)
ENV PATH="/root/.ollama/bin:$PATH"

# --------------------------
# Copy server and config
# --------------------------
COPY ollama_ai_server.py /app/ollama_ai_server.py
COPY server.cfg /app/server.cfg
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

# --------------------------
# Install Python packages
# --------------------------
RUN pip3 install --no-cache-dir \
    requests==2.31.0 \
    websockets==10.3 \
    beautifulsoup4==4.12.2 \
    configparser

EXPOSE 50000

ENTRYPOINT ["/app/entrypoint.sh"]

