# WhatsApp Bot — 网页版

面向外贸销售的 WhatsApp AI 助手：自动回复客户询盘、按计划主动跟进、管理客户/订单/产品资料，支持多账号（多租户）。由 Python 后端（FastAPI）、Web 管理台和 Node WhatsApp Bridge（Baileys）组成。

## 快速开始

需要 Python 3.10+、Node 20+。

```bash
git clone https://github.com/Grant-dex/WhatsApp_bot.git
cd WhatsApp_bot

# 首次安装网页版依赖
./run_web.sh --setup

# 启动网页控制台
./run_web.sh
```

浏览器打开：`http://127.0.0.1:8000`。

默认数据目录是项目下的 `data/`；`run_web.sh` 会自动创建 `config.yaml` 副本和 SQLite 数据库。API Key 写入 `data/.env` 或通过首次配置页保存。

## 部署

```bash
docker compose up -d --build     # 只监听 127.0.0.1:8000
```

公网入口建议用 Cloudflare 命名隧道 + Access，完整步骤见 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 连接 WhatsApp（可选）

网页控制台本身不依赖 Electron。需要收发 WhatsApp 时：

```bash
npm --prefix bridge install   # 首次一次
./run_web.sh --with-bridge
```

然后在「桥接状态」页扫码登录。没有配置 Bridge 时，网页控制台仍可查看客户、订单、资料和日志。

## 常用参数

```bash
./run_web.sh --port 8080          # 换端口
./run_web.sh --host 0.0.0.0       # 允许局域网访问（注意安全）
./run_web.sh --no-scheduler       # 只开网页，不跑自动跟进调度
./run_web.sh --with-bridge        # 同时启动 Node WhatsApp Bridge
./run_web.sh --no-browser         # 不自动打开浏览器
```

## 可选 OCR

精简依赖默认不含 OCR。需要上传扫描版 PDF/图片时：

```bash
.venv/bin/pip install -r requirements-ocr.txt
```

普通 PDF/Word/Excel/CSV/TXT 文本读取已包含在 `requirements.txt`。

## 测试

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
node --check bridge/index.js
node --test bridge/test/*.test.js tests/js/*.test.js
```

## 自动回复的行为

- **人工接管**：你在手机（或其他已登录设备）上亲自回复某个客户后，这条消息会记入对话，AI 对该客户自动暂停 `business.human_takeover_minutes` 分钟（默认 120，设为 0 关闭），期间不自动回复、不主动跟进。对话页可以手动「人工接管 2 小时」或「恢复 AI」。
- **连发合并**：客户在几秒内连发多条消息时，Bridge 会等客户停下（默认 6 秒，最长 20 秒）再让 AI 一次性回复全部内容。可用环境变量 `MESSAGE_BATCH_MS` / `MESSAGE_BATCH_MAX_MS` 调整，`MESSAGE_BATCH_MS=0` 关闭。
- **图片 / 语音 / 文件**：会以「[图片]」「[语音]」等占位记入对话并标记「需人工查看」，AI 不会对看不到的内容自动回复。
- 回复前会显示「正在输入…」。Bridge 默认不打印消息内容，排查问题时可设 `BRIDGE_DEBUG=1`。

## 网页版结构

- `src/web_main.py`：网页版启动入口
- `src/api_server.py` + `src/admin_api.py`：FastAPI 与后台 API
- `src/static/admin.html`：管理控制台
- `src/database.py`、`src/send_service.py`、`src/scheduler.py`：数据、发送、调度
- `bridge/`：WhatsApp Node Bridge（可选运行）
- `run_web.sh` / `requirements.txt`：网页版启动与依赖
- `tests/`：Python 与 Node 回归测试

## 安全与回滚

- 默认只监听 `127.0.0.1`；不要在没有认证/防火墙保护时使用 `--host 0.0.0.0`。
- 不连接真实 WhatsApp、不执行真实发送前，建议加 `--no-scheduler` 并使用测试数据库。
- 停止网页版：运行 `run_web.sh` 的终端按 `Ctrl+C`。


## 八页界面与隔离验收

管理台沿用原生 HTML/CSS/JavaScript。公共样式与页面脚本位于 `src/static/assets/`，仅该资源目录通过 `/assets` 提供；`design-reference/` 不对外公开。八页设计参考已规范命名，确认稿为 `design-reference/dashboard-preview.html`。

```bash
# 首次准备依赖
./run_web.sh --setup
# 隔离测试；不启动 WhatsApp Bridge，也不启动调度器
./run_web.sh --no-scheduler --no-browser --data-dir ./data-ui-test --port 8080
```

缺少 AI Key 时仍可查看管理台，右上角“配置 AI”进入原有设置流程；实际 AI 回复仍需要有效凭据。未连接 Bridge 时会如实显示离线与认证未知，不会显示已连接。

浏览器截图与隔离验证方式见 `tests/ui/README.md`；本轮改动、验收结果、差异及回退说明见 `UI-DELIVERY.md`。发布到互联网前需要另外完成认证授权、HTTPS、网络边界、凭据管理、数据备份与运行监控，本轮没有部署。
