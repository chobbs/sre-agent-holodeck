FROM python:3.12-slim
WORKDIR /app

# curl needed for Docker healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Build context is repo root so requirements.txt is shared across all mocks
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only this mock's source + fixtures
COPY grafana-mock/ .

EXPOSE 3000
CMD ["python", "app.py"]
