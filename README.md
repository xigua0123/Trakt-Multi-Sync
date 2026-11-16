# Trakt Multi Sync

带 Web 管理界面的 Trakt 多账号双向同步工具，Docker 一键部署。

以一个中心账号 **A** 为枢纽，与 **B / C / D** 双向同步观看历史、评分、Watchlist。
所有 Trakt 账号通过 **PIN 码授权**（Device Flow）添加，无需公网、无需回调地址、无需每个账号单独建 App。

## 特性

- **PIN 码授权**：Web 页面只填备注名，自动弹出 PIN + 激活链接，用户在 `trakt.tv/activate` 输入 PIN 即可
- **全局一个 Client ID**：在 docker-compose.yml 里配一次，所有 Trakt 账号共用
- **双向同步**：观看历史合并去重，评分按 `rated_at` 较新者覆盖（防循环）
- **自动 token 刷新**：access_token 过期自动续期
- **定时任务**：APScheduler 后台按配置间隔跑，Web 改配置即时生效
- **多架构镜像**：linux/amd64 + linux/arm64，支持群晖 / NUC / Raspberry Pi

## 快速开始

### 1. 创建 Trakt App（一次性）

打开 https://trakt.tv/oauth/applications/new ：

- Name: 随便填（如 `Trakt Multi Sync`）
- Redirect uri: 填 `urn:ietf:wg:oauth:2.0:oob`
- Save 后复制 **Client ID** 和 **Client Secret**

### 2. 配置 docker-compose.yml

```yaml
services:
  trakt-sync:
    image: ghcr.io/<你的GitHub用户名>/trakt-multi-sync:latest
    container_name: trakt-sync
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      - TZ=Asia/Shanghai
```

### 3. 启动

```bash
mkdir trakt-multi-sync && cd trakt-multi-sync
mkdir data
# 把上面的 docker-compose.yml 保存到当前目录
docker compose up -d
```

### 4. 浏览器打开 Web 管理台

访问 `http://你的服务器IP:8080`：

1. **系统设置** Tab：确认顶部显示当前 Client ID（从环境变量读入，只读），可选填管理台密码
2. **账号管理** Tab：
   - 填备注名（如 `main`），点【生成 PIN 并打开 Trakt 激活页 ↗】
   - 弹窗显示 PIN 码，自动打开 `https://trakt.tv/activate`
   - 登录你要加的 Trakt 账号，输入 PIN，点 Allow
   - 页面自动检测授权成功，账号列表刷新
   - 重复此步骤添加 B、C、D
3. **同步配置** Tab：
   - 主账号填 `A`
   - 从账号填 `backup1,backup2`（逗号分隔）
   - 保存
4. **同步与日志** Tab：点【立即跑一轮】，观察日志

之后容器会按你设的间隔自动跑。

## 目录结构

```
trakt-sync/
├── main.py            # FastAPI 入口 + PIN 授权 + 定时调度
├── sync_engine.py     # 双向同步核心
├── static/index.html  # Web 管理单页
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .github/workflows/docker-publish.yml
└── data/              # 挂载卷：accounts.json + config.json
```

## 同步规则

| 内容 | 双向行为 |
|---|---|
| 观看历史 | 合并双方记录，保留各自 `watched_at`，补缺失的，不删除 |
| 评分 | 冲突时按 `rated_at` 较新者覆盖（last-write-wins） |
| Watchlist | 合并双方，补缺失的，不删除 |

## 发版到 GitHub

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin git@github.com:<你的用户名>/trakt-multi-sync.git
git push -u origin main

# 打 tag 触发镜像构建
git tag v0.1.0
git push origin v0.1.0
```

GitHub Actions 会自动构建多架构镜像推到 `ghcr.io/<你的用户名>/trakt-multi-sync`。
如果在仓库 Settings → Secrets 配置了 `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`，会同时推 Docker Hub。

## 数据文件（在 data/ 卷里）

| 文件 | 内容 |
|---|---|
| `data/accounts.json` | 所有账号的 access/refresh token（**敏感，勿外泄**） |
| `data/config.json` | 主/从账号、同步开关、间隔、admin_token |


## 避坑

1. **节点拓扑是星型**：只让 B/C/D 与 A 同步，不要让 B 再去和 C 直接同步
2. **公网暴露务必设 admin_token**：在「系统设置」里设一个管理台密码
3. PIN 码有效期 10 分钟，超时需重新生成
4. 定时间隔建议 ≥ 1800 秒（30 分钟），避免触发 Trakt 限流
5. 第一次全量同步历史很长时耐心等日志，不要重复点【立即跑一轮】（已有运行锁）
