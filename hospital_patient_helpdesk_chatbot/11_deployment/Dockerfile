FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "07_backend.12_api_main:app", "--host", "0.0.0.0", "--port", "8000"]
