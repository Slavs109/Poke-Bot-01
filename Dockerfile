FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY check_stock.py queue_monitor.py run_forever.py config.yaml ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "run_forever.py"]
