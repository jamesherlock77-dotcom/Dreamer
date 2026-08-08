FROM python:3.11-slim

# System deps Playwright's browsers typically need on slim images
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright + browser + OS deps it needs (fonts, libs, etc.)
RUN playwright install --with-deps chromium

# Now copy the rest of your bot's code
COPY . .

CMD ["python", "main.py"]
