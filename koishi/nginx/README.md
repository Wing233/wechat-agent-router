# Nginx 反向代理（HTTPS + Basic Auth + Authenticator）

当前生产环境使用 lyph 的 Nginx（`/opt/lyph/deploy`）。

## 1. 在 http 层增加 WebSocket map

`nginx-tls.conf` 的 `http {` 后需要：

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

## 2. Basic Auth 文件

```bash
printf 'koishi:%s\n' "$(openssl passwd -apr1 '你的密码')" \
  > /opt/lyph/deploy/apps/koishi.htpasswd
chmod 644 /opt/lyph/deploy/apps/koishi.htpasswd
```

密码也可以随机生成：

```bash
PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
```

## 3. Koishi 侧

```yaml
group:console:
  console:
    open: false
    uiPath: /koishi
    apiPath: /koishi/status
```

`server.host` 绑定 Docker 网关地址（当前 `172.19.0.1`），不要绑定 `0.0.0.0`。

## 4. 反代配置

复制 `koishi.conf` 到 `/opt/lyph/deploy/apps/koishi.conf`，把 `__GATE_TOKEN__` 替换为：

```bash
python3 -c "import json;print(json.load(open('/opt/wechat-agent-router/config.json'))['koishi_auth']['gate_token'])"
```

然后：

```bash
chmod 600 /opt/lyph/deploy/apps/koishi.conf
docker exec lyph-nginx-1 nginx -t
docker exec lyph-nginx-1 nginx -s reload
```

## 5. 为什么不能直接用 `/koishi/` 子路径代理

Koishi 控制台的静态资源默认是根路径 `/index.js`、`/status`。必须同时设置：

- `uiPath: /koishi`
- `apiPath: /koishi/status`

否则静态资源和 WebSocket 会打到 lyph 主页。

## 6. Authenticator 逻辑

- `/koishi-auth/setup`：首次绑定，显示 TOTP 密钥
- `/koishi-auth/login`：输入 6 位动态码
- `/_koishi_totp`：Nginx `auth_request` 子请求，校验 `koishi_totp` Cookie
- Router 的 `/koishi-auth/*` 还要求 Nginx 注入 `X-Koishi-Auth-Gate`，直接访问 9900 会 404
