FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY interlock ./interlock
COPY web ./web
COPY scripts ./scripts
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "interlock.api:app", "--host", "0.0.0.0", "--port", "8000"]
