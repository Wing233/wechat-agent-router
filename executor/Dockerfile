FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0
WORKDIR /app
COPY executor/__init__.py /app/executor/__init__.py
COPY executor/server.py /app/executor/server.py
RUN mkdir -p /workspace && chown 10001:10001 /workspace
USER 10001:10001
EXPOSE 9800
CMD ["python", "-m", "uvicorn", "executor.server:app", "--host", "0.0.0.0", "--port", "9800", "--no-access-log"]
