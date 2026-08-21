# Caido_xhs — 小红书设备信息自动抓包工具

监听 [Caido](https://caido.io) 应用层代理（MITM）抓取的 HTTP(S) 流量，按规则提取指定小红书app数据包字段（如 `did` / `build` / `session` / `userid` / `nickname` / `platform` 等），自动生成客户端指纹（`fingerprint` / `x_legacy_fid`），写入XHS_sender的mongo数据库中的设备管理集合中，免去了本地抓包的麻烦，实现一键换号，自动添加设备信息。

> ⚠️ **免责声明**：本项目仅供**个人学习、研究与安全测试**使用。使用前请确保你已获得目标系统的合法授权，并遵守目标平台的服务条款与当地法律法规。因使用本项目产生的任何法律风险由使用者自行承担。

---

## ✨ 功能特性

- 🕵️ **应用层抓包**：基于 Caido 代理，自动解密 HTTPS，拿到明文请求/响应
- 🎯 **精准过滤**：按 `host + path` 前缀只处理目标请求，无关流量自动忽略
- 🔧 **灵活提取**：从响应 JSON / 请求头 URL 编码 form / 请求头原文多位置提取字段
- 🔐 **自动认证**：使用 Caido PAT（永久有效）自动获取/续期 access token，**全程免手动、免运维**
- 🧬 **指纹生成**：内置小红书客户端指纹计算。
- 🗄️ **增量写入**：HTTP 轮询 + 增量游标，重启不重抓历史；按 `userid` 唯一索引去重
- 🚀 **一键部署**：Docker Compose 一键启动 Caido + 监听器两个容器，容器重启自动恢复



https://github.com/user-attachments/assets/3c46623f-be59-40b2-83c5-85fa255ce590


---

## 🏗️ 架构

```
[客户端 App / 浏览器]
        │  HTTP(S) 代理指向 Caido:18000
        ▼
[Caido_xhs 容器]  ──MITM 解密──>  [目标服务器]
        │
        │  GraphQL API（HTTP 轮询，增量处理）
        ▼
[caido_listener 容器]  ← 本项目
        │  按 config.yaml 过滤 / 提取 / 生成
        ▼
[MongoDB]  xhs_demo.devices
```

- **Caido_xhs**：抓包代理（官方镜像 `caido/caido`），Web 界面与代理共用端口 `18000`
- **caido_listener**：监听器（本仓库构建），轮询 Caido GraphQL API 提取数据写 Mongo数据库
- **认证**：PAT（永久）→ device flow → access token，自动续期，无需人工干预

---

## 🚀 快速开始（Docker 一键安装）

### 环境要求

- Linux / macOS / Windows（Docker Desktop）
- Docker 20.10+ 与 Docker Compose v2

### 1. 克隆并配置

```bash
git clone https://github.com/evi1s/Caido_xhs
cd Caido_xhs
chmod +x install.sh
./install.sh          # 首次运行会生成 config.yaml 模板并提示退出
vim config.yaml       # 填写: auth.pat / mongo.* / targets / extract_fields
./install.sh          # 再次运行：构建并启动两个容器
```

### 2. 首次认领 Caido 实例（必须手动一次）

```
浏览器打开 http://<服务器IP>:18000
  → 用 Caido 账号登录并认领实例（Caido 官方机制，任何脚本无法代替）
  → Developer → Create Token → Personal Access Tokens

```
也可以直接访问：https://dashboard.caido.io/developer
<img width="701" height="367" alt="image" src="https://github.com/user-attachments/assets/5527fec9-2053-46d9-a27b-47d74d597434" />

把生成的 PAT 填入 `config.yaml` 的 `auth.pat`，然后：

```bash
docker compose restart listener
```

### 3. 配置客户端代理

- 手机 / 浏览器代理指向 `<服务器IP>:18000`
- 安装并信任 Caido CA 证书（`http://<服务器IP>:18000` → Settings → CA Certificate），HTTPS 解密必需

### 4. 查看结果

```bash
docker logs -f caido_listener
# 出现 "已写入 userid=xxx: {...}" 即提取成功
```

数据写入 MongoDB：数据库 `xhs_demo`，集合 `devices`。

---

## 📝 配置文件说明（config.yaml）

所有行为通过 `config.yaml` 配置，修改后 `docker compose restart listener` 生效。

| 配置节 | 说明 |
|---|---|
| `caido.url` | Caido GraphQL 地址（compose 部署填 `http://caido:8080`） |
| `auth.pat` | Caido Personal Access Token（永久有效，自动续期用） |
| `auth.auto_renew` | token 到期前自动续期（默认 `true`，免运维） |
| `mongo.*` | MongoDB 连接（host/port/账号/库/集合）⚠️要对应小红书私信系统的软件设置--数据库设置+数据库集合设置(设备集) |
| `targets` | 抓取目标列表（host + path 前缀，命中才处理） |
| `extract_fields` | 提取字段（每字段多个提取位置，按顺序尝试） |
| `field_postprocess` | 字段后处理（如 `strip_prefix` 去前缀） |
| `fixed_fields` | 固定值兜底（未提取到时写入） |
| `generated_fields` | 自动生成字段（指纹/FID，算法内置） |
| `dedup_key` | 去重字段（唯一索引，同一值只保留最新一条） |
| `polling` | 轮询参数（扫描间隔/批量/详情重试） |

### 提取位置语法

| 语法 | 说明 | 示例 |
|---|---|---|
| `response_json:路径` | 响应体 JSON，`.` 分隔路径，数字为数组下标 | `response_json:data.nickname` |
| `request_header_form:头名:键名` | 请求头值按 URL 编码 form 解析再取键名 | `request_header_form:xy-platform-info:deviceId` |
| `request_header:头名` | 直接取请求头值 | `request_header:user-agent` |

### 生成字段

```yaml
generated_fields:
  fingerprint:
    enabled: true
    type: "xhs_fingerprint"
    input: "did"
  x_legacy_fid:
    enabled: true
    type: "xhs_fid"
```

### 写入的数据格式（xhs_demo.devices）

```json
{
  "nickname": "example_user",
  "userid": "67cdcbbb000000000d0000f7",
  "did": "FE0020C4-8700-4CE8-B914-AFCEB77E2B9E",
  "build": "9221801",
  "version": "9.22.1",
  "session": "1786480096029534942082",
  "platform": "iOS",
  "xy-direction": "",
  "fingerprint": "20260817213022a0f719f5ee7f8f247cb3787b30e215a000b79d1292f94ebb",
  "x_legacy_fid": "1786973422-0-0-73f123b9a057b3f792c13f252987a1fc"
}
```

按 `userid` 唯一索引去重：同一账号只保留最新一条，不同账号各一条。

---

## 🧩 文件说明

```
├── docker-compose.yml      # 一键编排（Caido_xhs + caido_listener）
├── install.sh              # 一键安装脚本
├── config.yaml.example     # 配置模板（复制为 config.yaml 后填写）
├── caido_listener.py       # 主监听脚本（HTTP 轮询 + 提取 + 写 Mongo + 自动续期）
├── token_fetcher.py        # PAT → access token 获取脚本（自动续期时调用）
├── Dockerfile              # 监听器镜像构建
├── requirements.txt        # Python 依赖
├── .gitignore              # 排除 config.yaml / data / 日志
└── README.md
```

---

## 🛠️ 手动部署（不使用 Compose）

```bash
# 1. 启动 Caido
docker run -d --name Caido_xhs -p 18000:8080 -v caido-data:/data --restart unless-stopped caido/caido:latest

# 2. 构建监听器
docker build -t caido-listener .
docker run -d --name caido_listener --restart unless-stopped \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/data:/app/data \
  --network <与Caido相同的网络> \
  caido-listener
```

---

## 🔄 升级

```bash
git pull                          # 拉取最新代码
docker compose pull               # 更新 Caido 官方镜像
docker compose up -d --build      # 重建并重启
```

---

## ❓ 常见问题

**Q: 收不到流量 / Mongo 中的小红书设备管理集合没数据？**
- 确认客户端代理指向 `<服务器IP>:18000`
- 确认已安装并信任 Caido CA 证书（HTTPS 解密必需，iOS 需在"证书信任设置"开启完全信任 "设置---通用---关于本机---证书信任设置）
- 确认 `targets` 的 host 与请求的 Host 完全一致
- `docker logs -f caido_listener` 查看日志定位

**Q: token 过期怎么办？**
- 无需处理：`auto_renew: true` 时脚本会在到期前自动用 PAT 续期
- 手动获取：`docker exec caido_listener python3 /app/token_fetcher.py`

**Q: 想抓其他 App / 接口？**
- 修改 `targets`（目标）和 `extract_fields`（字段），重启容器即可

**Q: 换数据库 / 集合？**
- 修改 `config.yaml` 的 `mongo.database` / `mongo.collection`，重启即可

**Q: 容器重启后监听器会自动运行吗？**
- 会。监听器脚本是容器主进程（前台运行），`restart: unless-stopped` 保证容器重启后自动拉起，增量游标持久化，不重抓历史。

---

## 📄 License

[MIT](LICENSE)

# 项目版本 v1.0.0
