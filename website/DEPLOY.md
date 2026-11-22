# 官网部署说明

源码：本目录 `index.html` / `styles.css` / `main.js`  
线上：https://guidanghe.apps.grok.bot/

## 仓库对照（Mac 拉取用哪个）

| 仓库 | 作用 | 网页位置 |
| --- | --- | --- |
| GitHub `ypjn/guidanghe` | 产品主仓 | 分支 `product/desktop-finish-win` 的 `website/`（最新下载页） |
| Cursor Origin `easton-liu/guidanghe` | Apps 发布仓 | 根目录 `index.html` / `styles.css` / `main.js`（`main@1a629fd`） |

Mac 拉取产品仓：

```sh
git clone https://github.com/ypjn/guidanghe.git
cd guidanghe
git fetch origin
git checkout product/desktop-finish-win
# 网页在 website/
```

## 现状（2026-11-22）

- 产品仓 `website/` 与 Origin 根目录均已有「下载 Windows 标准包」+ `#download` + SHA。
- **线上仍是旧页**（首屏「本轮交付」/「看本轮交付」）。
- 原因：Cloud Agent 的 `publish-app` 常**不能就地更新**已占用的 slug `guidanghe`，只会提出 `guidanghe-0a36` / `guidanghe-5bf3` 等**新站**提案；缺工具的 Agent 会话也无法发布。
- Windows 副屏上的 Publish 对话框，本机 Computer Use 点不到；**建议在 Mac 上确认托盘**。

## 正确发布（更新现有站，不要新建 slug）

1. 打开 [Apps 控制台](https://cursor.com/dashboard/apps) → 点开已有 **`guidanghe`**。
2. 若 Cursor / Cloud Agent 托盘里出现 Publish 对话框：
   - 目标是 **`guidanghe.apps.grok.bot` / 更新已有 guidanghe`** → **确认 Publish**
   - 目标是 **`guidanghe-xxxx` 新 slug** → **Not now / 关掉**，不要确认
3. 优先从已绑定目录重新发：
   - Origin 仓根目录三文件，或
   - 产品仓 `website/`（小包）
4. Publish 托盘必须是 **update 现有 guidanghe**，不是新 slug。
5. 若始终只能提新 slug：联系 Cursor 把 app `guidanghe` **重新绑定**到发布源 + 静态目录。

相关会话：

- 转载官网：https://cursor.com/agents/bc-d8ce447a-ada9-5005-aed4-b28eec0e91f6  
  （已确认 `main@1a629fd` 页面正确，但该会话**没有** `publish-app` 工具）

## 校验

打开 https://guidanghe.apps.grok.bot/ 应看到：

- 主按钮「下载 Windows 标准包」
- `#download` 含 ZIP 与 SHA-256  
  `95eee7d2c5db1e649ddea46027fe9987c985a3c6a0b2efde6952491f2f737305`
