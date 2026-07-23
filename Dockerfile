FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application
COPY . .
RUN pip install --no-cache-dir -e .

# NB: run as root. The durable token volume Railway mounts at /data is owned by
# root, and a non-root process cannot write to it — which silently broke token
# persistence. For a personal single-tenant instance running as root is
# acceptable; keeping the token store writable matters more here.

# Railway provides PORT; the server binds 0.0.0.0:$PORT via env.
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT', os.environ.get('MCP_PORT','8000'))+'/health').read()" || exit 1

CMD ["python", "-m", "ticktick_mcp.cli", "run"]
