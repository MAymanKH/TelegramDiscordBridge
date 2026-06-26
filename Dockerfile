# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
#  - tzdata: lets the bridge resolve names like "Europe/Berlin" so the
#    `timezone` setting in settings.yaml renders digest timestamps in
#    your local time instead of the container default (UTC).
RUN apt-get update && apt-get install -y \
    gcc \
    libmagic1 \
    ffmpeg \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY bridge/ bridge/
COPY example.settings.yaml .

# Optional build-time stamp — pass via --build-arg GIT_SHA=$(git rev-parse --short HEAD)
ARG GIT_SHA=""
ENV BRIDGER_GIT_SHA=$GIT_SHA

# Create directories for persistent data
RUN mkdir -p /app/messages/telegram /app/messages/discord

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

# Run the application
CMD ["python", "main.py"]