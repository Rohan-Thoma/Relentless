FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium
ENV PYTHONUNBUFFERED=1
COPY . .
CMD ["python", "manage.py", "run"]