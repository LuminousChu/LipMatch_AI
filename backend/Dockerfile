FROM python:3.12.8-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY migrations ./migrations
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
# 啟動前先套用遷移（已套用的會略過）；帳號另以 python -m app.cli create-user 預建
CMD ["sh", "-c", "python -m app.cli migrate && uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000"]
