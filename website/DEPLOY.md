# 官网部署说明

源码：本目录 `index.html` / `styles.css` / `main.js`  
线上：https://guidanghe.apps.grok.bot/

## 现状（2026-11-22）

- 本仓库与远端已有带「下载 Windows 标准包」+ `#download` 的静态页。
- **线上仍是旧页**（首屏「看本轮交付」）。
- 原因：Cloud Agent 的 `publish-app` **不能就地更新**已占用的 slug `guidanghe`，只会提出 `guidanghe-0a36` / `guidanghe-5bf3` 等**新站**提案；整仓 `main` 根目录打包还超过 50MB 上限。
- Grok Bot 侧也没有 publish 工具。

## 正确发布（更新现有站，不要新建 slug）

1. 打开 [Apps 控制台](https://cursor.com/dashboard/apps) → 点开已有 **`guidanghe`**。
2. 若 [该 Cloud Agent](https://cursor.com/agents/bc-9339142c-0f6f-4eb9-860d-fd4db11128ec) 托盘里有 `guidanghe-0a36` / `guidanghe-5bf3`：**选 Not now / 关掉**，不要确认。
3. 用与**原发布绑定**一致的目录重新发：
   - 优先：`website/`（`main` 上小包），或
   - 瘦分支：`cursor/guidanghe-app-publish-28ec`（仅三份静态文件）
4. Publish 托盘必须是 **update 现有 guidanghe**，不是新 slug。
5. 若始终只能提新 slug：联系 Cursor 把 app `guidanghe` **重新绑定**到 `ypjn/guidanghe` + `website/`。

## 校验

打开 https://guidanghe.apps.grok.bot/ 应看到：

- 主按钮「下载 Windows 标准包」
- `#download` 含 ZIP 与 SHA-256  
  `95eee7d2c5db1e649ddea46027fe9987c985a3c6a0b2efde6952491f2f737305`
