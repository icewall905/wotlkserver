FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends default-mysql-client \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py agents.py ./
COPY templates templates
EXPOSE 8080
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "8", "--timeout", "1900", "app:app"]
