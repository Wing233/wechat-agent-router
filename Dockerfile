FROM lyph-api:0.1.0
ENV PYTHONDONTWRITEBYTECODE=1
USER root
WORKDIR /app
COPY *.py /app/
COPY vendor /app/vendor
COPY plugins /app/plugins
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9900", "--no-access-log"]
