# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 1.x | ✅ |

## Reporting a Vulnerability

请**不要**在公开 Issue 里贴出密钥、Cookie、服务器地址或可复现的攻击细节。

优先使用 GitHub 的私密漏洞报告：

1. 打开 <https://github.com/Wing233/wechat-agent-router/security/advisories>
2. 点击 **Report a vulnerability**
3. 描述影响范围、复现步骤和建议修复

如果无法使用 Security Advisories，可以发邮件到仓库所有者 GitHub 资料里的公开邮箱，并在标题里写 `[SECURITY]`。

## 响应时间

- 48 小时内确认收到
- 7 天内给出初步判断
- 修复后会在 Release Notes / Security Advisory 中致谢（如果你愿意）

## 范围

以下问题属于安全范围：

- 认证/授权绕过（`service_token`、Agent Key、审批码）
- 路径穿越、任意文件读写、命令注入
- SSRF、Hub 媒体下载越界
- 密钥/Token/Cookie 泄露
- 插件权限绕过
- 远程代码执行
- 拒绝服务（可稳定复现的资源耗尽）

以下问题通常不属于安全范围：

- 需要先拿到服务器 root / 容器权限的攻击
- 用户自己把 `9900` 暴露到公网且未加认证
- 第三方平台（微信、OpeniLink Hub、DeepSeek、SiliconFlow、LeetCode）自身的问题
- 模型幻觉、生成内容不准确
- 社会工程

## 部署者检查清单

- [ ] `config.json` 权限 600，未提交到 git
- [ ] `service_token`、`login_token_secret`、`credential_key` 都是随机值
- [ ] 每个 Agent 使用独立 `api_key`
- [ ] 不把 9900 直接暴露公网；反向代理加 TLS 和访问控制
- [ ] `plugins/`、`workspace/` 只挂载必要目录
- [ ] 定期备份 `data/router.db` 和 `config.json`
- [ ] 定期轮换 Hub Channel Key、Agent Key、LLM Key
- [ ] 启用 `leetcode` 前确认平台 ToS
- [ ] 启用 `evolution` 前确认你信任模型生成的代码
