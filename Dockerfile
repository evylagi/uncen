FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone zen-proxy (single file, no npm install)
RUN git clone https://github.com/12errh/zen-proxy.git /opt/zen-proxy

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Start proxy in background, then bot
CMD sh -c "node /opt/zen-proxy/zen-proxy.mjs & sleep 3 && python bot.py"
