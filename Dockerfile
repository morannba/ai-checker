# Playwright's official image already has Chromium and its system libraries,
# which Render/Railway's plain Python runtime can't install.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY requirements.txt .
# keep the Python package on the same version as the browsers baked into the image
RUN pip install --no-cache-dir -r requirements.txt "playwright==1.48.0"
COPY . .

ENV PYTHONUNBUFFERED=1 RENDER_JS=1
EXPOSE 10000
CMD gunicorn app:app --workers 1 --threads 8 --timeout 240 --bind 0.0.0.0:${PORT:-10000}
