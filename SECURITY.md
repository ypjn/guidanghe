# Security Policy

## 密钥与凭据

- 本仓库不保存任何 AI / API key、token 或私钥。
- 本机 AI 配置请放在 `ai.json`；构建时注入的 `ai.bundled.json` 已被 `.gitignore` 忽略，不应提交。
- 如果怀疑密钥曾进入 commit、release、安装包、截图或日志，请先在服务商控制台轮换或吊销，再清理历史。仅从工作树删除文件不会让已经公开的密钥失效。

## 报告问题

请通过 GitHub 仓库的 Security 页面私密报告，或直接联系维护者。不要在公开 issue 中粘贴真实密钥、token 或个人信息。
