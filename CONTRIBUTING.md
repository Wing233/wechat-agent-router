# Contributing

感谢你愿意改进微桥。

## 开发环境

- Python 3.11+
- Docker（可选，用于跑 Router）
- Windows（仅桌面端 / 本机 Agent 需要）

```bash
git clone https://github.com/Wing233/wechat-agent-router.git
cd wechat-agent-router
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

## 本地跑 Router

Router 的默认路径是 Linux 容器路径（`/app`、`/data`、`/workspace`）。  
推荐直接用 Docker：

```bash
cp config.example.json config.json
# 填好配置
docker compose up -d --build
curl http://127.0.0.1:9900/health
```

如果要在本机直接跑，需要自己准备对应的目录，或用 bind mount / symlink。

## 代码风格

- Python：4 空格缩进，类型注解优先，异常信息面向用户可读
- 前端：原生 HTML/CSS/JS，不引入构建链
- 提交信息：`feat:` / `fix:` / `docs:` / `chore:` / `security:`
- 不提交 `config.json`、`data/`、`workspace/`、日志、截图中的真实地址

## 新增插件

1. 新建 `plugins/<id>/manifest.json`
2. 新建 `plugins/<id>/plugin.py`，实现 `apply(ctx)`
3. 只申请真正需要的权限
4. 给工具写清楚 description 和 JSON Schema
5. 加最小测试或至少本地跑通
6. 在 README 的插件表里补充一行

示例：

```python
def apply(ctx) -> None:
    ctx.tools.register(
        "ping",
        "返回 pong",
        {"type": "object", "properties": {}},
        lambda _: {"pong": True},
    )
```

## 修改安全边界

涉及以下内容时，请在 PR 里写清威胁模型和回滚方式：

- 认证 / 授权
- 文件路径
- 命令执行
- 网络请求（SSRF）
- 密钥存储
- 审批流程

## 提交 PR

- 一个 PR 只做一件事
- 说明“改了什么、为什么、怎么验证”
- 截图/日志请先脱敏
- CI 必须通过

## 行为准则

请保持友善、就事论事。参见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
