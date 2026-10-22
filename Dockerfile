FROM python:3.11-slim

WORKDIR /app

# Prevent Python from buffering stdout/stderr in Railway logs
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "src.main_loop"]