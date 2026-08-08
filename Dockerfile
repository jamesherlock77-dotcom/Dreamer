# Official Playwright image: Python + Chromium + every system library Chromium needs,
# already installed. This is what makes Playwright reliable on Railway without fighting
# Nixpacks/apt — no need for a separate `playwright install --with-deps` build step.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Install Python deps first so this layer is cached unless requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Discord bot is a background worker, not a web server — just run it
CMD ["python", "main.py"]
