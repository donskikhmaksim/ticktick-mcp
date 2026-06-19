FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application
COPY . .
RUN pip install --no-cache-dir -e .

# Railway provides PORT; the server binds 0.0.0.0:$PORT via env.
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0

CMD ["python", "-m", "ticktick_mcp.cli", "run"]
