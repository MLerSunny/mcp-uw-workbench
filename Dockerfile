FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY data/ ./data/

RUN pip install --no-cache-dir -e .

# docker-compose selects the server per container via `command:`.
CMD ["underwriting-mcp"]
