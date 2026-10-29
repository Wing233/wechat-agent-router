# 部署

## 推荐拓扑

```text
公网
  │ HTTPS
  ▼
Nginx / Caddy  ──→  Router :9900
                      │
                      ├── OpeniLink Hub :9800
                      ├── data/ (SQLite)
                      └── workspace/
```

Router 和 Hub 可以在同一台机器，也可以分开；Router 只需要能访问 Hub。

## Docker Compose

```bash
cp config.example.json config.json
mkdir -p data workspace sandbox
docker compose up -d --build
```

`docker-compose.yml` 默认：

- 端口 `9900:9900`
- `host.docker.internal:9800` 访问宿主机 Hub
- 挂载 `config.json`、`data/`、`workspace/`、`plugins/`、`sandbox/`

如果 Hub 在 Docker 里，给 Router 加同一个 network：

```yaml
networks:
  hub:
    external: true
    name: openilink-hub_default
services:
  router:
    networks:
      - default
      - hub
```

然后把 `hub.base_url` 改成 `http://openilink-hub:9800`。

## 反向代理

### Caddy

```caddyfile
router.example.com {
    reverse_proxy 127.0.0.1:9900
}
```

### Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name router.example.com;

    ssl_certificate     /etc/letsencrypt/live/router.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/router.example.com/privkey.pem;

    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:9900;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

建议再加 IP allowlist / Basic Auth / Cloudflare Access，至少不要让 `/internal/*` 裸奔。

## 权限

```bash
chmod 600 config.json
chmod 700 data workspace
```

容器默认以 root 运行，因为 `plugins/` 需要可写（`evolution` 安装插件）。  
如果要降权，把 `plugins/` 和 `workspace/` 的宿主权限交给容器用户，并去掉 `evolution`。

## 备份

```bash
mkdir -p backup
sqlite3 data/router.db ".backup 'backup/router-$(date +%F).db'"
tar czf backup/media-$(date +%F).tar.gz workspace/
cp config.json backup/config-$(date +%F).json
```

备份里含密钥，注意加密和访问控制。

## 升级

```bash
git pull
docker compose up -d --build
```

升级前备份。数据库只做 `CREATE TABLE IF NOT EXISTS`，不会自动删列。

## 多实例

Router 是单进程 SQLite，**不要**同时跑多个实例写同一个 `router.db`。  
需要横向扩展时，先把 SQLite 换成 Postgres（当前未内置）。

## 桌面端部署

Windows 电脑：

```powershell
python -m pip install -r desktop\requirements.txt
python desktop\main.py
```

或打包：

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
```

把 `WeChatAgentDesktop.exe` 放到目标机器，填写 Router 地址和 Agent Key。

## 系统服务

Router 交给 Docker Compose 的 `restart: unless-stopped` 即可。  
如果不用 Docker，可以用 systemd：

```ini
[Unit]
Description=WeChat Agent Router
After=network.target

[Service]
WorkingDirectory=/opt/wechat-agent-router
Environment=ROUTER_CONFIG=/opt/wechat-agent-router/config.json
ExecStart=/opt/wechat-agent-router/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 9900
Restart=always
User=router

[Install]
WantedBy=multi-user.target
```

注意：`app.py` 默认使用 `/app`、`/data`、`/workspace` 路径；非容器部署需要自己准备这些目录或用 bind mount。

## 监控

- `GET /health`：进程、插件、任务、Agent 在线状态
- Docker healthcheck 已内置
- 日志：`docker logs -f wechat-agent-router`
- 桌面端“服务器 → 日志”查看最近日志

## 升级检查清单

- [ ] 备份 `data/router.db` 和 `config.json`
- [ ] 看 CHANGELOG 是否有 breaking change
- [ ] 确认 `plugins/` 没有被覆盖
- [ ] `docker compose up -d --build`
- [ ] `curl /health`
- [ ] 微信发一条测试消息
