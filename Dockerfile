FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY . .
RUN pip install --no-cache-dir -e ".[dev]"
RUN mkdir -p /app/data
CMD ["python", "-m", "app.main"]
