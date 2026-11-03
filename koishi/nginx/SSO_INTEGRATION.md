# 统一 Authenticator（TOTP）登录接入说明

目标：一个 TOTP secret、一次登录、一个会话，覆盖 Koishi 控制台、lyph 管理后台，
以及未来挂在同一 Nginx 下的 Web 后台。不引入 Authelia / Authentik / OAuth2 Proxy 等独立服务，
Router 直接充当 IdP。

## 1. 范围与结论清单

| 入口 | 现状 | 接入方式 | 结论 |
| --- | --- | --- | --- |
| Koishi 控制台 `/koishi/` | Nginx Basic Auth + `koishi_totp` cookie，Router `/koishi-auth/check` | 新增 `/sso/*`，`/koishi/` 改用 `auth_request /_sso`；`/koishi-auth/*` 原样保留 | 可统一 |
| lyph 管理后台 `/admin`、`/api/v1/admin` | owner 密码 + `lyph_owner` cookie + CSRF | Nginx 校验 TOTP 后注入 `X-SSO-Verified` HMAC，lyph 信任该头并派生 CSRF | 可统一，密码登录保留为回退 |
| 未来同 Nginx 的 Web 后台 | 各自登录 | 纯页面可直接用 `auth_request`；有自有会话的应用需实现同样的受信任代理头 | 网关层可统一；否则只能双重登录 |

不在本方案范围：不经过这套 Nginx 的独立服务、纯 API、桌面端。它们没有共享的浏览器会话，
无法用一个 HttpOnly cookie 覆盖；继续使用各自凭证并通过网络边界限制访问。

建议：新后台优先做成“无自有登录的页面/API”，由 Nginx 统一门禁；必须保留自有登录的，
按 lyph 的受信任代理头模式接入，或者明确接受“先 SSO 再应用登录”的双重登录。

## 2. 架构

```
浏览器（同一主机，HTTPS）
  │
  ▼
Nginx
  ├─ /sso/*             → wechat-agent-router:9900   （TOTP 登录/登出/检查，IdP）
  ├─ /_sso   (internal) → router /sso/check → 204 + X-SSO-Verified
  ├─ /koishi/           → auth_request → 172.19.0.1:5140
  ├─ /admin, /admin/*   → auth_request → web:3000
  ├─ /api/v1/admin/*    → auth_request + X-SSO-Verified → api-internal:8000
  └─ /api/*（其余）      → api-internal:8000（不经过 SSO，行为不变）
```

单主机是“一个 cookie”的前提。Koishi 与 lyph 已经在同一个 Nginx 下，因此 cookie 用
host-only + `Path=/` 即可同时覆盖 `/koishi/` 和 `/admin`。

## 3. 关键设计

### 3.1 一个 secret

沿用 `config.json -> koishi_auth`：

- `totp_secret`：唯一 TOTP secret，`/sso/setup` 与 `/koishi-auth/setup` 绑定的是同一个。
- `enrolled`：是否已完成绑定。
- `session_secret`：SSO/旧 Koishi 会话的 HMAC 签名密钥。
- `session_ttl`：默认 43200 秒（12 小时）。

新增可选配置：

```json
"sso": {
  "cookie_name": "lyph_sso",
  "cookie_domain": "",
  "edge_secret": "REPLACE_WITH_32+_RANDOM_CHARS",
  "extra_cookies": ["koishi_totp", "lyph_owner"]
}
```

`edge_secret` 为空时回退到 `session_secret`。生产建议单独设置，便于独立轮换。

### 3.2 Cookie

- 名称 `lyph_sso`，`Path=/`，默认不设 `Domain`（host-only）。
- `HttpOnly`、`Secure`、`SameSite=Lax`。
- 选择 Lax 而不是 Strict：从外部链接点回 `/admin` 时，Strict 不发送 cookie，会先跳到登录页；
  Lax 对顶级导航友好，同时阻止跨站 POST。
- 不使用 `SameSite=None`：那会把 cookie 暴露给跨站请求，且必须配合 `Secure`。
- 只有同一主机才能共享。若未来后台在兄弟子域，才考虑 `cookie_domain=.example.com`；
  代价是任一子域都能收到该 cookie，不建议默认开启。

### 3.3 会话与受信任代理头

会话值复用 `koishi_auth.issue_session/verify_session`：`issued.HMAC-SHA256(session_secret)`。

Nginx 的 `/_sso` 子请求命中 Router `/sso/check`，校验 cookie 后返回：

```
X-SSO-Verified: v1.<unix_ts>.<hex_hmac>
```

签名内容为 `v1|<cookie_value>|<ts>`，TTL 90 秒。Nginx 用
`auth_request_set $sso_verified $upstream_http_x_sso_verified;` 取出并注入受保护 location。

密钥的实际持有方是 Router（签名）与 lyph（校验）；Nginx 只转发，不计算 HMAC。
“只信任 Nginx”不足以替代 HMAC：同网段容器可以伪造普通请求头，HMAC 才能证明会话来自 IdP。

lyph 校验时必须同时满足：`lyph_sso` cookie 存在、HMAC 正确、时间戳在 90 秒内。
只有头没有 cookie，或只有 cookie 没有头，都不能登录。

### 3.4 CSRF

SSO 会话是无状态的，没有数据库里的 `csrf_token`。lyph 用同一把 edge secret 派生：

```
csrf = HMAC-SHA256(edge_secret, "csrf|" + sso_cookie_value)[:32]
```

`GET /api/v1/auth/session` 在 SSO 模式下返回该 token，前端现有的 `web/lib/api.ts` 会自动
取用并放到 `X-CSRF-Token`；`require_owner` 的 Origin 校验保持不变。

不推荐“Nginx 注入固定 token”：固定值可被重放，且无法按会话区分。

### 3.5 `next` 防开放重定向

`/sso/login?next=...` 只接受同站路径，且必须以 `/koishi`、`/admin`、`/api/v1/admin` 开头；
拒绝协议/主机、`//`、`..`、反斜杠、控制字符、`#`，长度上限 2048，否则回退到 `/`。
Nginx 用 `$uri` 而不是 `$request_uri` 拼 `next`，避免查询串里的 `&` 被解析成额外参数。

### 3.6 登出

- `GET /sso/logout`：清 `lyph_sso`、`koishi_totp`、`lyph_owner`，跳回 `/sso/login`。
- `POST /api/v1/auth/logout`：清 `lyph_owner` 与 `lyph_sso`；有会话时仍校验 Origin + CSRF。
- 会话是无状态的，删除 cookie 即登出；被复制的 cookie 在 TTL 内仍有效。需要“立刻踢掉所有会话”时轮换
  `session_secret`。
- 登出 CSRF 的最坏结果是“被强制退出”，不产生数据变更。

### 3.7 TOTP 重放

- `verify_totp_once` 校验 RFC 6238，窗口 ±1 步（约 90 秒）。
- 额外维护内存重放缓存（key = secret + 命中步数 + code，TTL 180 秒），同一验证码不能用第二次。
- 登录失败限流 12 次 / 5 分钟 / IP。
- Router 单进程部署，内存缓存有效；进程重启后缓存清空，但 90 秒窗口仍然限制重放。

### 3.8 绑定页

`/sso/setup` 会显示 TOTP secret，必须保护。模板对这一个精确 location 保留 Basic Auth
（`koishi.htpasswd`）作为 break-glass；`/koishi/` 日常访问不再需要 Basic Auth。
重置 TOTP 后如已删除 htpasswd，需要临时恢复该 location 再绑定。

## 4. 改动文件

Router：

- `sso_auth.py`（新增）：cookie、会话、`next` 白名单、HMAC 头、TOTP 重放。
- `sso_routes.py`（新增）：`/sso/check`、`/sso/login`、`/sso/setup`、`/sso/logout`。
- `app.py`：顶部 `import sso_routes`，末尾一行 `sso_routes.register(app, CFG, SETTINGS, _koishi_page)`。
- `config.example.json`：新增可选的 `sso` 配置块（cookie 名与 edge secret 占位符）。
- `koishi/nginx/sso.conf`（新增）：通用 `/_sso` + 三个受保护示例。
- `koishi/nginx/koishi.conf`：只保留 `/koishi-auth/*` 兼容回退，删除旧的 `/koishi/` 块（避免 location 重复）。

lyph：

- `api/lyph/auth.py`：`_sso_verified` / `_sso_session` / `_owner_session`，`require_owner` 接入，
  `/auth/session` 返回 `via` 与 `sso` 状态，logout 清理两个 cookie。
- `api/lyph/config.py`：`LYPH_SSO_ENABLED`、`LYPH_SSO_SHADOW`、`LYPH_SSO_EDGE_SECRET`、
  `LYPH_SSO_COOKIE`、`LYPH_SSO_HEADER_TTL`。
- `deploy/nginx.conf`、`deploy/nginx-tls.conf`：`http` 块补 `map $http_upgrade $connection_upgrade`；
  确认有 `include /etc/nginx/apps/*.conf;`（TLS 是生产入口，两处都要）。

`api/lyph/main.py` 不需要改：`auth` 已经在 `create_app()` 的 router 列表里。

## 5. lyph Nginx 集成 diff

```diff
 http {
     resolver 127.0.0.11 valid=5s ipv6=off;
+    map $http_upgrade $connection_upgrade { default upgrade; '' close; }
     upstream api_backend { zone api_backend 64k; server api-internal:8000 resolve; }
@@
     server {
         listen 8080;
         server_tokens off;
         add_header X-Content-Type-Options nosniff always;
         add_header Referrer-Policy strict-origin-when-cross-origin always;
         add_header X-Frame-Options SAMEORIGIN always;
+        include /etc/nginx/apps/*.conf;
```

说明：

- 生产若使用 `compose.tls.yaml`，实际加载的是 `deploy/nginx-tls.conf`：它已有 `include`，
  只需补同样的 `map`（`/koishi/` 的 WebSocket 需要）。仓库里 `nginx.conf` 与 `nginx-tls.conf`
  都已补上；线上未改。
- 当前工作区 `deploy/nginx.conf` 已有 `include` 和 `map`；线上若没有 `include` 必须补，
  否则 `apps/*.conf` 不会生效。线上若已定义同名 `map`，不要重复添加。
- `deploy/apps/sso.conf` 从 `agent-router/koishi/nginx/sso.conf` 复制，替换 `__GATE_TOKEN__`。
- `koishi.conf` 必须替换为 legacy-only 版本，否则 `/koishi/` 会与 `sso.conf` 重复定义，
  `nginx -t` 直接失败。
- `/api/v1/admin` 使用 `^~`，优先于现有 `location /api/`；其余 `/api/` 完全不变，SSE 不受影响。
- `/admin` 是 Next.js 页面，保留 `proxy_buffering off`；`proxy_read_timeout 300s` 覆盖长请求。

## 6. lyph 环境变量

`.env`（`compose.yaml` 已通过 `env_file` 注入，无需改 compose）：

```
LYPH_SSO_ENABLED=false
LYPH_SSO_SHADOW=true
LYPH_SSO_EDGE_SECRET=REPLACE_WITH_SAME_VALUE_AS_ROUTER
LYPH_SSO_COOKIE=lyph_sso
LYPH_SSO_HEADER_TTL=90
```

- `LYPH_SSO_EDGE_SECRET` 必须与 Router 的 `sso.edge_secret` 完全一致，至少 32 字符。
- `LYPH_SSO_ENABLED=true` 但 secret 不足 32 字符时，`settings.validate()` 会启动失败（显式失败，
  避免静默退回密码登录）。
- 密码登录始终保留：`POST /api/v1/auth/login` 未加 SSO 依赖。

## 7. 迁移与回滚

前置：备份 `deploy/nginx.conf`、`deploy/apps/koishi.conf`、`.env`、Router 的 `config.json`；
每步先 `nginx -t` 再 reload。以下命令由运维执行，本次交付不部署、不重启。

### 阶段 0：准备

1. 生成 48 字符随机串，同时写入 Router `config.json -> sso.edge_secret` 和 lyph `.env`。
2. 确认 Router 容器与 lyph nginx 在同一 Docker 网络，`wechat-agent-router:9900` 可解析。
3. 确认站点是 HTTPS；`Secure` cookie 在纯 HTTP 下不会保存。

### 阶段 1：shadow（只注入头，不改 require_owner）

- Router：部署新代码（`/sso/*` 此时只是新增路由，不影响旧入口）。
- Nginx：只加入 `/_sso`、`/sso/*`、`/sso/setup`、`/api/v1/auth/session` 和
  `@lyph_auth_session`；`/admin`、`/api/v1/admin`、`/koishi/` 先不加。
- lyph：`LYPH_SSO_SHADOW=true`、`LYPH_SSO_ENABLED=false`，重启 API。
- 手动访问 `/sso/login` 完成一次 TOTP 登录，然后：

```bash
curl -sS -b /tmp/sso.jar https://HOST/api/v1/auth/session
# 期望：{"authenticated":...,"sso":{"enabled":false,"verified":true}}
# authenticated 取决于是否还有密码会话；关键是 sso.verified=true。
```

`verified:true` 说明 Nginx 注入、Router 签名、lyph 校验三段链路都通了，但 lyph 仍不接受它登录。

回滚：删除 `/api/v1/auth/session` 的 auth_request 与 `/sso/*` 代理，恢复环境变量，reload。
用户侧无感。

### 阶段 2：启用 lyph SSO

- Nginx：加入 `/admin`、`/admin/`、`/api/v1/admin`、`/api/v1/auth/logout` 四块。
- lyph：`LYPH_SSO_ENABLED=true`、`LYPH_SSO_SHADOW=false`，重启 API。
  想更稳可以先只改这一项、不加 Nginx 门禁：此时 API 已能接受 SSO 头，但 `/admin` 仍可走密码，
  确认无误后再加四个 location。
- 验证：访问 `/admin` → 302 `/sso/login?next=/admin`；登录后进入工作台；修改类请求带
  `X-CSRF-Token`；`/api/v1/admin/*` 返回正常；退出后 `/admin` 再次 302。
- 此时 `/admin/login` 也会被 Nginx 拦到 SSO，密码页不可达，但 `POST /api/v1/auth/login`
  仍可用——这是 lyph 层的回退，不是绕过 Nginx 的方式。

回滚：`LYPH_SSO_ENABLED=false` + 重启 API，然后从 Nginx 删除四个 location 并 reload。
密码登录立即恢复。只改环境变量而不删 location，页面仍会被 SSO 拦截。

### 阶段 3：Koishi 切换，最后去掉 Basic Auth

1. 用 legacy-only 的 `koishi.conf` 替换线上文件，同时加入 `sso.conf` 的 `/koishi/` 块
   （块内暂时保留两行 `auth_basic`）。reload。
2. 验证 `/koishi/`：先过 Basic Auth，再跳 SSO，登录后可正常使用 Koishi（WebSocket 正常）。
3. 删除 `/koishi/` 块里的两行 `auth_basic`，reload。日常只剩 TOTP。
4. `/sso/setup` 的两行 Basic Auth 保留，作为重置 TOTP 后的绑定入口。

回滚：

- 只回滚第 3 步：把两行 `auth_basic` 加回 `/koishi/`，reload。
- 完整回滚：恢复旧 `koishi.conf`（含旧 `/koishi/` 块），从 `sso.conf` 删除 `/koishi/`，reload。
  SSO 路由与 lyph 可继续保留，互不影响。

## 8. 验证命令

```bash
# 未登录：页面 302，API 401 JSON
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' https://HOST/admin
curl -sS -o /dev/null -w '%{http_code}\n' https://HOST/api/v1/admin/settings

# 登录页可用
curl -sS -o /dev/null -w '%{http_code}\n' https://HOST/sso/login

# TOTP 登录（$CODE 为当前 6 位码）
csrf=$(curl -sS -c /tmp/sso.jar https://HOST/sso/login \
  | sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p')
curl -sS -b /tmp/sso.jar -c /tmp/sso.jar -o /dev/null \
  -w '%{http_code} %{redirect_url}\n' \
  --data-urlencode "csrf=$csrf" --data-urlencode "code=$CODE" \
  'https://HOST/sso/login?next=/admin/'

# 会话与 CSRF
curl -sS -b /tmp/sso.jar https://HOST/api/v1/auth/session

# 同一验证码立即重放：应重新返回登录页，且不新增 Set-Cookie
curl -sS -b /tmp/sso.jar -D- -o /dev/null \
  --data-urlencode "csrf=$csrf" --data-urlencode "code=$CODE" \
  'https://HOST/sso/login?next=/admin/' | grep -i '^set-cookie' || echo 'no new cookie (expected)'

# next 开放重定向：登录后应回到 /，而不是外部站点
curl -sS -o /dev/null -w '%{redirect_url}\n' \
  --data-urlencode "csrf=$csrf" --data-urlencode "code=$CODE" \
  'https://HOST/sso/login?next=https://evil.example/'

# 登出（需要 session 返回的 csrf_token）
CSRF=$(curl -sS -b /tmp/sso.jar https://HOST/api/v1/auth/session \
  | python -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])')
curl -sS -b /tmp/sso.jar -c /tmp/sso.jar -X POST \
  -H 'Origin: https://HOST' -H "X-CSRF-Token: $CSRF" \
  https://HOST/api/v1/auth/logout
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' https://HOST/admin

# 伪造头直连 API（容器内）应失败：没有 cookie 或 HMAC 不对都返回未认证
docker compose exec api python -c \
  "import urllib.request as u; r=u.Request('http://127.0.0.1:8000/api/v1/auth/session',headers={'X-SSO-Verified':'v1.0.'+'0'*64}); print(u.urlopen(r).read().decode())"
```

## 9. 安全注意

- cookie 不跨站：host-only + Lax + HttpOnly + Secure；不要把 `Domain` 放大到整个父域，除非明确接受子域风险。
- 统一使用一个规范主机名（带不带 `www` 视为两个站点），否则浏览器不会共享 cookie。
- 可选强化：cookie 名改为 `__Host-lyph_sso`（Router `sso.cookie_name` 与 lyph `LYPH_SSO_COOKIE` 同步），
  浏览器会强制 `Secure`、`Path=/`、禁止 `Domain`，比普通 host-only 更难配错。
- `next` 白名单是唯一入口；不要改成“以 `/` 开头就放行”，`//evil` 和 `/%2f%2fevil` 都是开放重定向。
- HMAC 密钥轮换：同时替换 Router `sso.edge_secret` 与 lyph `LYPH_SSO_EDGE_SECRET`，逐请求重新签名，
  已登录用户不需要重新登录（旧签名立即失效，新请求立即用新密钥）。
- 会话密钥轮换：替换 `koishi_auth.session_secret` 会让所有 SSO 会话失效，需要重新登录。
- TOTP 轮换：`reset_koishi_auth` 会同时换 `totp_secret` 和 `session_secret`，所有人重新绑定/登录。
- 登出只清 cookie；无状态会话的硬吊销依赖 `session_secret` 轮换。
- TOTP 重放窗口固定 ±1 步；不要为了“容错”调大。重放缓存是进程内，重启后仍有 90 秒上限。
- Nginx 必须在受保护 location 用 `proxy_set_header X-SSO-Verified $sso_verified;` 覆盖客户端输入；
  `@lyph_auth_session` / `@lyph_auth_logout` 必须显式置空。
- XSS 仍然可以带着 cookie 调 API；CSRF token 对 JS 可读，这是所有同源后台的共同边界，不能靠本方案消除。
- 未完成/未验证：未部署、未 reload、未跑 lyph 全量测试；只做了 Python 语法检查与 SSO 签名/白名单的隔离验证。

## 10. 未来 Web 后台扩展

只读页面（无自有登录）：

```nginx
location ^~ /new-admin/ {
    auth_request /_sso;
    error_page 401 = @sso_login;
    proxy_pass http://new_backend;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

有自己的 API：参考 `/api/v1/admin` 的两段 location，API 失败时返回 401 JSON，不要 302；
应用若需要识别用户/CSRF，按 `auth.py` 的 `_sso_verified` 逻辑校验同一个
`X-SSO-Verified` + `lyph_sso` cookie，并从 edge secret 派生自己的 CSRF。

同时把新前缀加入 `sso_auth.ALLOWED_NEXT_PREFIXES`，否则登录后的 `next` 会被回退到 `/`。
仍然要求与 Koishi/lyph 同主机，否则浏览器不会共享这个 cookie。
