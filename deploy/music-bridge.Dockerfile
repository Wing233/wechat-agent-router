FROM rust:1-alpine AS bridge-build

RUN apk add --no-cache musl-dev
WORKDIR /build
COPY rust/app/Cargo.toml rust/app/Cargo.lock ./
COPY rust/app/src ./src
ARG NCM_BRIDGE_PYTHON_VERSION=3.14.10
RUN NCM_BRIDGE_PYTHON_VERSION="$NCM_BRIDGE_PYTHON_VERSION" cargo build --locked --release

FROM node:22-alpine

RUN apk add --no-cache ca-certificates
RUN npm install -g @music163/ncm-cli@0.1.7

WORKDIR /app
COPY --from=bridge-build /build/target/release/wechat-agent-music-bridge /usr/local/bin/wechat-agent-music-bridge

ENV HOME=/data \
    NO_COLOR=1

EXPOSE 18888
CMD ["wechat-agent-music-bridge", "--host", "0.0.0.0", "--port", "18888"]
