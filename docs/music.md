# 网易云音乐只读插件（music）

通过官方 `ncm-cli` 读取网易云音乐数据，接入 Agent Router。插件只暴露只读工具，不包含播放控制、红心、歌单增删、评论/笔记发布、云盘上传或 AI 写歌生成。

## 架构

```text
Agent Router (Docker/Linux)
  plugins/music
        │  HTTP + Bearer token
        ▼
scripts/ncm_bridge.py  (Windows，和 ncm-cli 同机)
        │  argv allowlist + subprocess
        ▼
ncm-cli（已登录的官方 CLI）
```

Router 与 ncm-cli 不在同一台机器时使用 bridge；Router 本机开发时可以直接用 CLI 模式。

## 前置条件

在运行 bridge 的机器上（Python 3.10+；也可以使用 Router 的 Python 3.12 容器）：

```powershell
npm install -g @music163/ncm-cli
ncm-cli configure
ncm-cli login
ncm-cli login --check
```

`ncm-cli configure` 需要网易云开放平台的 `appId` 和 `privateKey`。只读工具不需要 mpv；只有播放才需要。

## 启动 bridge

在 `agent-router` 目录：

```powershell
$env:NCM_BRIDGE_TOKEN = "换成一个长随机串"
python scripts/ncm_bridge.py --host 0.0.0.0 --port 18888
```

只允许本机访问时用默认的 `--host 127.0.0.1`。Docker 容器访问 Windows 宿主时需要 `0.0.0.0` 或 Docker 网关地址，并且必须：

- 使用 `NCM_BRIDGE_TOKEN` / `--token`
- 在 Windows 防火墙中只放行受信任网络
- 不要把端口暴露到公网

### Router 在远程服务器时

bridge 必须运行在 Router 能访问到的位置。当前验证的是“Windows 本机 + 本机 Router”或“Windows 本机 bridge + 同机 Docker”。如果 Router 在远程服务器：

- 推荐在服务器 host 或同网络容器里安装 `ncm-cli`、完成登录，再运行 bridge；
- 不建议把 Windows 的 18888 直接暴露公网；
- 如果必须跨机，使用 SSH/VPN 等受控通道，不要只依赖端口转发。

健康检查：

```bash
curl http://127.0.0.1:18888/health
```

## Router 配置

`config.json`：

```json
{
  "plugins": {
    "music": {
      "transport": "http",
      "bridge_url": "http://host.docker.internal:18888",
      "bridge_token": "与 NCM_BRIDGE_TOKEN 一致",
      "timeout": 30,
      "max_items": 20
    }
  }
}
```

Router 本机直接运行 CLI 时：

```json
{
  "plugins": {
    "music": {
      "transport": "cli",
      "bin": "ncm-cli",
      "timeout": 30,
      "max_items": 20
    }
  }
}
```

`bridge_url` 使用其他主机时，需要把该主机加入 `plugins/music/manifest.json` 的 `permissions`（`net:<host>`）。

## 定时同步

配置 `sync_enabled=true` 后，插件注册 Router 任务 `music_sync`，每小时运行一次：

- **每小时**：读取最近播放 10 首，替换 lyph 爱好里 `ncm-history-*` 条目；每条同时写入 `cover`（专辑封面，http→https）、`artists` 和 `album`，供爱好页音乐分区使用
- **每 7 天**：重新抓取完整红心歌单，上传新的 `netease-liked-songs.md`，等新来源 `ready` 后再删除旧来源
- 同步状态保存在 Router 的 `music:last_liked_sync`
- 写入 lyph 走后台 SSO + Admin API，不直接写 SQLite
- 红心同步失败时保留旧知识来源；最近播放为空时不会清空旧爱好

相关配置：

```json
{
  "sync_enabled": true,
  "liked_interval_days": 7,
  "recent_limit": 10,
  "knowledge_filename": "netease-liked-songs.md",
  "knowledge_visibility": "public",
  "knowledge_rag": true,
  "lyph_api_url": "http://api-internal:8000",
  "lyph_origin": "https://123.57.153.49"
}
```

手动触发：

```bash
curl -X POST http://127.0.0.1:9900/internal/jobs/music_sync/run \
  -H "X-Service-Token: <service_token>"
```

## 工具

| 工具 | 作用 |
| --- | --- |
| `music__search` | 搜索歌曲、专辑、歌单、MV、播客、声音或综合结果 |
| `music__user_profile` | 当前账号昵称、签名、用户 ID、VIP 状态 |
| `music__favorite_playlist` | 红心歌单「我喜欢的音乐」 |
| `music__playlists` | 我创建 / 收藏的歌单 |
| `music__playlist_detail` | 歌单详情 |
| `music__playlist_tracks` | 歌单歌曲列表 |
| `music__daily_recommend` | 每日推荐 |
| `music__lyric` | 歌词（截断到 4000 字） |
| `music__recent_history` | 最近播放 |
| `music__listen_ranking` | 听歌排行（总榜 / 周榜） |
| `music__cloud_songs` | 音乐云盘歌曲 |
| `music__album_tracks` | 专辑歌曲 |
| `music__artist_songs` | 艺人歌曲 |
| `music__podcast_categories` | 播客一级分类 |
| `music__note_topics` | 笔记话题搜索 |
| `music__hot_comments` | 热门评论 |
| `music__ai_song_balance` | AI 写歌积分余额 |

所有工具默认只对 owner 可见：没有加入 `app.py` 的 `GUEST_TOOLS`。如果以后要让访客使用公开搜索，只把 `music__search` 加入 `GUEST_TOOLS`，不要加入个人资料、歌单、云盘类工具。

## 只读边界

`scripts/ncm_bridge.py` 和插件共用 `plugins/music/readonly.py` 的 allowlist。以下命令不会被接受：

- `play` / `pause` / `resume` / `stop` / `next` / `prev` / `seek` / `volume` / `queue`
- `playlist create/add/remove/reorder/update*`
- `song like` / `song dislike`
- `comment post` / `comment reply`
- `note publish` / `note delete`
- `podcast subscribe` / `unsubscribe` / `voiceUpload` / `voicelistCreate` / `voicelistEdit` / `voiceUpdate`
- `cloudupload` / `cloud downloadFile` / `cloud updateSort`
- `aisong generate` / `generateLyric` / `dailyLogin`
- `login` / `logout` / `configure` / `config` / `upgrade` / `diag` / `tui`

## 已知限制

- 搜索结果里 `visible=false` 的歌曲不可播放；工具会同时返回 `visible` 和 `playable`，只读工具不提供播放。
- `ncm-cli 0.1.7` 的 `playlist add/remove` 会把 `songIdList` 序列化成字符串，服务端返回 `songIdList参数非法`；插件没有暴露这两个命令。
- `podcast collected` 当前返回 400，插件没有暴露。
- `album collected` 需要同时传 `--offset`；插件内部已固定传 `offset=0`。
- `search mv` 需要 `--offset`；插件内部已固定传 `offset=0`。
- 网易云开放平台有每日调用次数上限，超限时工具返回 `quota_exceeded`。
- 只读工具不需要 mpv；播放控制不在本插件范围内。

## 验证

```powershell
# bridge 白名单
python -m pytest tests/test_music_plugin.py -q

# 启动 bridge 后
curl -H "Authorization: Bearer $env:NCM_BRIDGE_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{"args":["search","song","--keyword","晴天","--limit","3"]}' `
  http://127.0.0.1:18888/run
```

Router 端工具调用会返回：

```json
{
  "ok": true,
  "data": {"query": "晴天", "type": "song", "items": []},
  "source": "ncm-cli",
  "fetched_at": "..."
}
```
