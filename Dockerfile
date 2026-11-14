FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1
COPY . .
CMD ["python", "manage.py", "run"]