FROM python:3.9-slim

WORKDIR /app

# Copy source files
COPY server.py /app/server.py
COPY server.cfg /app/server.cfg

# Install required OS packages
RUN apt-get update && apt-get install -y \
    lynx \
    openssl \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip install --no-cache-dir \
    telnetlib3==2.0.0 \
    wikipedia==1.4.0 \
    requests==2.31.0 \
    websockets==10.3 \
    beautifulsoup4==4.12.2 \
    configparser

# Expose default Telnet port
EXPOSE 23

# Run the server
CMD ["python", "/app/server.py"]
