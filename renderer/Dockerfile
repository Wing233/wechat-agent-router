FROM rust:1.96-alpine AS build

RUN apk add --no-cache build-base
WORKDIR /src
COPY rust/app/Cargo.toml rust/app/Cargo.lock ./rust/app/
COPY rust/app/src ./rust/app/src
COPY rust/renderer/Cargo.toml ./rust/renderer/
COPY rust/renderer/src ./rust/renderer/src
RUN cargo build --release --locked --manifest-path rust/app/Cargo.toml

FROM mcr.microsoft.com/playwright:v1.49.1-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /src/rust/app/target/release/wechat-resume-render /usr/local/bin/wechat-resume-render

ENV HOME=/tmp
USER pwuser
EXPOSE 9801
CMD ["wechat-resume-render"]
