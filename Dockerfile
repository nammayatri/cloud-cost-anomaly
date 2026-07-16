FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY providers/ ./providers/

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
