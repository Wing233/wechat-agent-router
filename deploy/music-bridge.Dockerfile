FROM node:22-alpine

RUN apk add --no-cache python3 ca-certificates

RUN npm install -g @music163/ncm-cli@0.1.7

WORKDIR /app
COPY plugins/music ./plugins/music
COPY scripts/ncm_bridge.py ./scripts/ncm_bridge.py

ENV HOME=/data \
    PYTHONUNBUFFERED=1 \
    NO_COLOR=1

EXPOSE 18888
CMD ["python3", "scripts/ncm_bridge.py", "--host", "0.0.0.0", "--port", "18888"]
