# 网易云音乐桥接

`wechat-agent-music-bridge` 是一个可选的 Rust HTTP 服务。它借助官方 `ncm-cli` 读取网易云音乐数据，支持歌曲搜索、歌词、歌单、最近播放、每日推荐和账号资料等只读命令。Docker 镜像使用 Node.js 运行固定版本 `@music163/ncm-cli@0.1.7`，桥接进程无需 Python。

## 在 Docker 中运行

该服务加入 Router 的 Docker 网络。按仓库默认项目名启动 Router 后，在仓库根目录设置一个独立的 ASCII 随机 token 并启动桥接。若 Router 使用自定义 Compose 项目名，把 `docker-compose.music.yml` 中的外部网络名改为实际网络名。

```bash
export NCM_BRIDGE_TOKEN='<替换为随机 token>'
docker compose -f docker-compose.music.yml up -d --build music-bridge
docker compose -f docker-compose.music.yml exec music-bridge ncm-cli configure
docker compose -f docker-compose.music.yml exec music-bridge ncm-cli login
docker compose -f docker-compose.music.yml exec music-bridge wget -qO- http://127.0.0.1:18888/health
```

`configure` 使用网易云开放平台提供的 `appId` 和 `privateKey`。登录配置存放在 `./data/music-bridge`，容器重建时继续使用此目录。

## HTTP 接口

同一 Docker 网络中的客户端可通过 `http://music-bridge:18888` 访问服务。`GET /health` 返回健康状态和只读命令数。查询使用 `POST /run`，请求头带 `Authorization: Bearer <NCM_BRIDGE_TOKEN>` 和 `Content-Type: application/json`，请求体传入参数数组。

```json
{"args":["search","song","--keyword","晴天","--limit","3"]}
```

查询成功时返回 `{"ok": true, "data": ...}`。鉴权失败返回 401，白名单校验失败返回 403。调用端需自行接入这两个 HTTP 接口。桥接服务支持 43 个命令，具体命令可查阅[只读白名单](../rust/app/src/readonly.rs)。

也可在安装并登录 `ncm-cli` 的宿主机上构建运行。服务默认绑定 `127.0.0.1:18888`，`NCM_CLI_BIN` 或 `--bin` 可指定 CLI 路径。

```bash
export NCM_BRIDGE_TOKEN='<替换为随机 token>'
cargo build --locked --release --manifest-path rust/app/Cargo.toml
./rust/app/target/release/wechat-agent-music-bridge
```

Windows 可在 PowerShell 用 `$env:NCM_BRIDGE_TOKEN` 设置 token，运行 `rust/app/target/release/wechat-agent-music-bridge.exe`。

## 验证

```bash
cargo test --locked --manifest-path rust/app/Cargo.toml
python rust/verification/verify.py --unit-gate
```

差分验证需要 Rust、Python 及 `rust-anything`。脚本用本地替身测试 CLI 调用，记录 Python 基准后回放 Rust 服务。测试基准位于 `rust/verification/reference/`，回放产物保存在 `rust/.verification/`。应用回放覆盖鉴权、只读命令、请求大小、Unicode、超时和错误映射，补充测试逐字节对照 HTTP 响应并单独归一化 `Date` 头。验证环境使用 Windows 的 Python 3.13.17 和 Linux 的 Python 3.14.10。

Windows 的官方 npm 启动器有一项行为差异。Rust 直接将查询参数传给 Node，含空格的词组会作为一项完整参数。Python 基准经批处理启动器时会拆开这类词组，测试单独断言了两种结果。
