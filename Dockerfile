FROM python:3.11-slim
WORKDIR /app
COPY src/scheduler.py .
RUN pip install --no-cache-dir kubernetes
CMD ["python", "scheduler.py"]
