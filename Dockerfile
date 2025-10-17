# Use a lightweight Python base
FROM python:3.11-slim

WORKDIR /app

# Copy dependency file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your app files
COPY . .

# Create and switch to a non-root user
RUN useradd -m botuser
USER botuser
ENV HOME=/home/botuser

CMD ["python", "countdown_bot_supabase.py"]
