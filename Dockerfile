# Use a lightweight Python base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install build dependencies for asyncpg (Postgres C libs)
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

# Copy dependency file first to leverage caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your app files
COPY . .

# Create and switch to a non-root user
RUN useradd -m botuser
USER botuser
ENV HOME=/home/botuser

# Run the bot
CMD ["python", "countdown_bot_supabase.py"]
