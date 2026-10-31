FROM lyph-api:0.1.0
ENV PYTHONDONTWRITEBYTECODE=1
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
USER root
WORKDIR /app
COPY requirements.txt /app/
RUN uv pip install --python /opt/venv/bin/python --no-cache --index-url "$PIP_INDEX_URL" -r /app/requirements.txt
COPY *.py /app/
COPY vendor /app/vendor
COPY plugins /app/plugins
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9900", "--no-access-log"]
