# LyrebirdMS Bot NAS 最终增强版

这是一个面向 **NAS / Docker Compose / 自用管理员** 场景整理过的版本，目标不是做复杂架构，而是：

- 开箱即用
- 中国大陆代理场景可配
- **Bot 与管理面板同容器**
- 默认带 MySQL / PostgreSQL / Redis
- 出问题时能从面板和日志快速定位
- 在不偏离原有 Bot 流程的前提下，增强易用性与可维护性

---

## 这版相对原项目的核心变化

### 部署层

- Bot 与管理面板同容器启动
- Compose 默认内置：
  - MySQL
  - PostgreSQL
  - Redis
- 默认数据卷和目录已配好，适合 NAS 直接持久化
- 管理面板默认端口 `47521`
- 默认 bridge 网络，不依赖 host 网络
- 启动脚本支持**引导模式**：即使没有 config.json，也能先起面板

### 国内场景适配

- Telegram 代理继续兼容 `config.json` 中的 `proxy`
- `requests` 类外部请求支持：
  - `HTTP_PROXY`
  - `HTTPS_PROXY`
  - `NO_PROXY`
- Redis / MySQL / PostgreSQL / 内网服务建议走 `NO_PROXY`

### Bot 与功能增强

- 搜索会话缓存支持 Redis，失败自动回退到本地文件缓存
- AI 翻译 provider 支持切换：
  - `gemini`
  - `gemini_api`
  - `openai_compatible`
- 翻译任务状态落盘到 `runtime/tasks.json`
- 翻译失败支持从面板重试
- `/help` 更清晰
- 搜索无图时不再依赖外部占位图


### PWA 与移动端继续增强

- 支持安装到手机主屏幕
- 新增安装引导浮层与移动端底部导航
- 新增离线回退页，NAS 短暂不可达时仍可看到缓存页面
- 面板操作新增成功提示 Toast
- 编辑中的配置表单新增离开页面未保存提醒
- 危险操作新增确认提示

### 管理面板增强

现在的面板不只是“看状态”，还能做一些实际操作：

- 查看 Bot 心跳与基础状态
- 查看 MySQL / PostgreSQL / Redis / MovieServer / Emby / AI 连通性
- 查看最近日志 / cron 日志
- 查看与搜索下载记录
- 查看与搜索用户记录
- 查看翻译任务并对失败任务重试
- 一键重试最近失败翻译任务
- 清空会话缓存
- 修改部分运行时功能开关
- 修改 AI provider / 模型 / Base URL / chunk size
- 查看并**直接编辑 blackseeds 规则**
- 导出诊断 JSON

- 完整配置中心保存时自动创建备份
- 下载提交流水支持批量勾选后重试或删除
- 用户列表支持批量调整积分 / 免费额度 / 状态
- 翻译任务支持批量勾选后重试或删除
- 在面板里直接编辑 `config.json`

### 首次启动引导

这版新增了一个非常实用的行为：

- 管理面板提供“快速配置向导”表单，优先适合第一次部署

- 如果 `./data/config/config.json` 不存在
- 容器会自动把 `config-example.json` 写进去
- **只启动管理面板，不启动 Bot**
- 你可以先在面板或宿主机补配置
- 配好后重启容器即可

这比“配置不完整就直接崩”更适合 NAS 用户。

---

## 目录结构与持久化

建议保留这些目录：

- `./data/config` -> 配置目录，面板会写入 `config.json`
- `./data/logs` -> 日志
- `./data/runtime` -> 运行状态 / 任务状态 / 会话缓存
- `./data/mysql` -> MySQL 数据
- `./data/postgres` -> PostgreSQL 数据
- `./data/redis` -> Redis 数据
- `./data/config/blackseeds.txt` -> 资源过滤规则

---

## 一键启动步骤

### 1）复制环境变量模板

```bash
cp .env.example .env
```

### 2）第一次启动可以不先写 config.json

直接启动也可以：

```bash
docker compose --env-file .env up -d --build
```

如果没有检测到 `./data/config/config.json`，系统会：

- 自动创建 `./data/config/config.json`
- 进入引导模式
- 只启动管理面板

然后你打开：

```text
http://你的NASIP:47521
```

在面板里就能看到缺哪些配置，并可直接通过“快速配置向导”保存。

### 3）如果你已经有旧版 config.json

把旧配置放到：

```text
./data/config/config.json
```

再启动即可。

### 4）中国大陆部署建议先改 `.env`

```env
PROXY_MODE=true
HTTP_PROXY=http://你的代理IP:端口
HTTPS_PROXY=http://你的代理IP:端口
NO_PROXY=localhost,127.0.0.1,mysql,postgres,redis,movieserver,emby
ADMIN_PANEL_TOKEN=改成你自己的强口令
```

### 5）查看日志

```bash
docker compose logs -f lyrebirdmsbot
```

---

## 现在默认内置的数据库配置

### MySQL

- Host: `mysql`
- Port: `3306`
- DB: `emby`
- User: `lyrebird`
- Password: `lyrebird`

### PostgreSQL

- Host: `postgres`
- Port: `5432`
- DB: `ms-bot`
- User: `lyrebird`
- Password: `lyrebird`

### Redis

- URL: `redis://redis:6379/0`

`config-example.json` 已尽量与这些默认值对齐。

---

## 数据库初始化

Compose 已附带初始化脚本：

- `docker/mysql-init/001-init.sql`
- `docker/postgres-init/001-init.sql`

第一次启动后会自动创建基础表，不需要手工建表。

---

## 部署前最后检查

在第一次让 Bot 真正上线前，建议先做这 6 步：

1. 先只打开面板，确认“部署前检查”全部通过
2. 在“配置中心”里把 `ADMIN_PANEL_TOKEN` 改掉
3. 确认 `MovieServer`、`Emby`、`MySQL`、`PostgreSQL`、`Redis` 都显示正常
4. 如果你在中国大陆，确认 `.env` 里的 `HTTP_PROXY / HTTPS_PROXY / NO_PROXY` 已填写
5. 先测试 `/status` 和 `/help`，再测一次真实搜索和下载
6. 第一次改大配置前，先在面板里创建配置备份

也可以在容器里手动执行：

```bash
docker compose exec lyrebirdmsbot python /app/preflight_check.py
```

如果返回非 0，说明还有配置、依赖或目录权限问题。

## 管理面板说明

### 首页你能看到什么

- Bot 当前状态与心跳
- 24h 下载统计
- 用户统计
- 翻译任务统计
- 会话缓存后端与 key 数量
- 当前 AI provider

### 服务连通性

面板会展示：

- MySQL
- PostgreSQL
- Redis
- MovieServer
- Emby
- AI provider 配置是否完整

这能直接帮助你定位“为什么国内不响应”。

### 可操作项

- 开关翻译能力
- 开关入库通知
- 开关 TMDB 背景
- 修改日志等级
- 修改 AI provider 相关非敏感配置
- 重试失败翻译任务
- 清空会话缓存
- 编辑 blackseeds
- 编辑 config.json
- 导出诊断 JSON

- 完整配置中心保存时自动创建备份
- 下载提交流水支持批量勾选后重试或删除
- 用户列表支持批量调整积分 / 免费额度 / 状态
- 翻译任务支持批量勾选后重试或删除

### 重要提醒

如果你在面板里直接改了 `config.json`：

- 文件会保存到磁盘
- **Bot 不会自动热重载全部配置**
- 最稳妥的方式是保存后重启容器

---

## AI 翻译 provider 配置

### 1）gemini（兼容原有思路）

```json
"translation_enabled": true,
"ai_provider": "gemini",
"gemini_api_key": "你的key",
"gemini_model": "gemini-2.5-flash"
```

### 2）gemini_api（直接走 Gemini API）

```json
"translation_enabled": true,
"ai_provider": "gemini_api",
"gemini_api_key": "你的key",
"gemini_model": "gemini-2.5-flash"
```

### 3）openai_compatible（兼容 OpenAI Chat Completions）

```json
"translation_enabled": true,
"ai_provider": "openai_compatible",
"ai_base_url": "https://你的网关/v1",
"ai_api_key": "你的key",
"ai_model": "gpt-4o-mini"
```

---

## Redis 会话持久化

这版默认 Redis 已在 compose 里起好。

用途：

- 搜索会话缓存
- 避免 Bot 重启后部分上下文直接丢失

如果 Redis 不可用，系统会自动回退到本地文件缓存，不会直接导致 Bot 不可用。

---

## 任务状态说明

翻译任务会落盘到：

```text
./data/runtime/tasks.json
```

当前支持的状态：

- `queued`
- `running`
- `success`
- `failed`
- `retry_requested`

管理员可以在面板里：

- 看任务状态
- 筛选任务
- 对失败任务单独重试
- 批量重试最近失败任务

---

## blackseeds 规则

`./data/config/blackseeds.txt` 用来过滤不想展示或不想下载的资源项。

这版支持：

- 面板里查看
- 面板里直接编辑保存
- 后续排障时可结合日志确认是否命中过滤规则

---

## 国内部署建议

### Telegram

Telegram 通常需要代理。建议：

- `config.json` 里的 `proxy.scheme` 使用 `http`
- `.env` 里的 `HTTP_PROXY / HTTPS_PROXY` 与实际代理保持一致

### 内网服务

以下服务建议走 `NO_PROXY`：

- `mysql`
- `postgres`
- `redis`
- `localhost`
- `127.0.0.1`
- `MovieServer` 内网域名/IP
- `Emby` 内网域名/IP

### 为什么国外服务器正常、国内不正常

常见原因通常只有几类：

- Telegram 代理不稳定
- 只有 Pyrogram 走代理，别的 HTTP 请求没走代理
- 内网地址被错误地也送进代理
- 网络慢导致超时，但日志里原来没写清楚

这版已经尽量把这些情况暴露到日志和面板里。

---

## 常用排障方式

### 看容器日志

```bash
docker compose logs -f lyrebirdmsbot
```

### 看健康检查 JSON

```text
http://你的NASIP:47521/api/health
```

### 下载诊断包

```text
http://你的NASIP:47521/diagnostics.json
```

诊断包里会包含：

- 脱敏后的配置
- 健康检查结果
- 最近事件
- 最近任务
- 最近日志
- blackseeds 预览

### 最推荐的排障顺序

1. 看首页心跳是否正常
2. 看健康检查里 MySQL / PostgreSQL / Redis 是否通过
3. 看 MovieServer / Emby 是否可达
4. 看 Bot 日志里是否有代理、网络、鉴权错误
5. 再去 Telegram 里测试 `/start`

---

## 升级建议

如果你是从旧版迁移：

1. 先备份：
   - 旧 `config.json`
   - 旧 `blackseeds.txt` 或旧 blackseeds 规则文件
   - 数据目录
2. 再替换新代码
3. 把旧配置放到 `./data/config/config.json`
4. 执行：

```bash
docker compose --env-file .env up -d --build
```

---

## 当前仍未完全做满的点

这版已经比较适合 NAS 自用，但还不是大型后台系统。当前还没有完全做满的包括：

- 更细的下载任务状态机
- 下载任务的一键重提
- 更完整的用户管理动作
- 完整权限系统
- 配置热重载
- 面板内首次启动向导的表单式配置器

但对“自用、NAS、尽量开箱即用、出问题能查”这个目标来说，这版已经足够接近实用化。


## 本轮新增优化

- 管理面板新增“快速配置向导”，用表单方式填写 Telegram / MovieServer / Emby / 代理 / 内置数据库开关
- 保存 config.json 后会在面板提示“建议重启容器”，避免用户误以为所有配置都会自动热重载
- 入口脚本会在启动 Bot 前等待 MySQL / PostgreSQL 端口就绪，减少 NAS 上首次启动或重启时的时序问题


## 本轮新增

- 管理面板支持点击查看 **用户详情 / 下载详情 / 任务详情**
- 下载记录和下载提交流水现在可直接从面板进入详情页
- `/help` 和 `/status` 的提示更明确，方便自助排障

## 推荐排障顺序

1. 先看面板首页的 **服务健康**
2. 再看 `/status` 输出
3. 然后点开下载记录、用户记录或任务详情定位具体失败原因
4. 最后再看容器日志


## 本轮新增

- 用户详情页支持积分增减、免费额度增减、账号状态切换（a/b/d）
- 用户列表中的用户 ID 可点击进入详情页
- 管理动作会写入日志和运行事件，便于排障


## 本轮新增

- 面板支持对失败的下载提交流水执行单条重试和批量重试。
- Bot 增加下载任务重试观察器，管理员从面板请求重试后会由后台轮询执行。
- 任务详情页支持直接执行重试或删除，用户详情页保留管理员调整入口。
- 快速配置向导增加步骤提示，更适合第一次部署。


## 本轮新增：完整配置中心

管理面板新增了更完整的 UI 配置能力：

- 可直接在面板中编辑 Telegram、MySQL、PostgreSQL、MovieServer、Emby、AI Provider、代理、面板令牌等主要配置
- 支持导出当前 `config.json` 作为备份
- 支持从面板上传并导入配置备份
- 新增服务检测中心，可统一查看 MySQL、PostgreSQL、Redis、MovieServer、Emby、AI Provider 的状态

推荐使用顺序：

1. 先打开面板完成快速配置向导
2. 再到“完整配置中心”补齐所有高级字段
3. 点击“重新检测全部服务”确认外部依赖正常
4. 保存后重启容器，让 Bot 进程完整加载新配置

> 注意：面板保存配置后会写回磁盘上的 `config.json`，但不会强制热重载 Bot 主进程，因此生产使用仍建议重启一次容器。


## 本轮新增：面板内管理 `.env`

现在管理面板除了可以编辑 `config.json`，还支持直接在 UI 中维护常用 `.env` 项：

- `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`
- `PROXY_MODE`
- `LOG_LEVEL`
- `ADMIN_PANEL_TITLE`
- `ADMIN_PANEL_TOKEN`
- `BOOTSTRAP_MODE`

同时保留原始 `.env` 文本编辑与导出能力。

建议：

1. 先通过“完整配置中心”保存主要业务配置
2. 再通过“.env 常用项”配置代理与面板访问令牌
3. 保存后重启容器，使代理和日志级别完全生效

## 本轮新增：服务单独检测与下载任务筛选

- 服务检测中心支持对单个服务执行单独检测
- 下载提交流水支持按状态筛选，便于排查失败和重试中的任务


## 面板新增

- 配置中心已支持按功能区使用，常见维护可直接从 UI 完成。
- 新增配置备份与恢复，可在面板中创建当前 config.json / .env / blackseeds 的快照，并支持恢复。
- 面板顶部增加分区导航，便于在总览、下载与任务、用户、配置中心、日志与诊断之间切换。


## 新增的批量操作

### 用户批量调整

在“用户”页可以勾选多个用户，然后一次性：

- 增加/扣减积分
- 增加/扣减免费额度
- 批量切换状态（白名单 / 普通 / 禁用）

### 下载提交流水批量处理

在“下载与任务”页可以勾选多条下载提交流水，然后：

- 批量请求重试
- 批量删除历史记录

### 翻译任务批量处理

在“下载与任务”页的翻译任务区可以勾选多条任务，然后：

- 批量请求重试
- 批量删除历史记录

### 配置自动备份

在“完整配置中心”保存时，系统会先自动创建一份备份，随后再写入新的配置。
如果保存后出现异常，可以在“配置备份与恢复”里直接回滚。


## PWA 与移动端体验

- 管理面板已支持 PWA，可在手机浏览器中添加到主屏幕。
- 支持 manifest、service worker、独立启动显示与移动端底部导航。
- 适合在 NAS 局域网内用手机快速查看状态、编辑配置、执行常见管理动作。
- 首次进入面板后，若浏览器支持安装，会出现“安装到主屏幕”入口。



## 本次整合版补强

这版不再按小步试探，而是把管理面板尽量收口成可长期使用的 NAS 后台：

- 配置中心拆成分页：安装向导、核心配置、服务与 AI、`.env / 代理`、备份恢复、高级编辑
- 首次安装向导支持分步骤填写 Telegram、MovieServer/Emby、数据库说明、代理网络与最终校验
- 支持一键应用 Compose 内置数据库预设，并在保存前自动备份
- 支持从 UI 直接校验当前配置和服务状态
- 备份恢复、危险操作、批量操作都带确认提示，更适合移动端误触场景
- 底部导航、PWA、离线页、安装提示、Toast、未保存提醒仍然保留

### 推荐使用方式

1. 第一次启动后先打开面板
2. 进入“配置中心 → 安装向导”按顺序填写
3. 点击“应用内置 Compose 预设”填好默认数据库地址
4. 点击“校验当前配置”确认 Telegram / MovieServer / Emby / 数据库都正常
5. 返回 NAS 重启容器，再测试 `/start`、`/help`、`/status`
=======
# LyrebirdMS-New-Bot
>>>>>>> 3ce580a1c826cd9b6190d9b8f910c0d9298006cd
