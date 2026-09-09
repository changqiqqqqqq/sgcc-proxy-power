# 95598 代理购电资料服务：Linux / Docker 部署

该目录是服务器版项目。它在 Linux 容器中使用 Playwright Chromium 无头扫描
国家电网 P2019“执行的电价和收费标准”公告及南方电网五省“电价及收费标准”目录，提供网页管理界面，并把目录、状态和附件保存在持久卷。
服务器本身不需要桌面环境。

## 目录结构

```text
sgcc-proxy-power-linux/
├── app/
│   ├── main.py          # 服务入口，运行方式为 python -m app.main
│   ├── scanner.py       # Playwright 无头扫描及省市区选择
│   ├── csg.py           # 南方电网五省分页扫描与 PDF/图片附件保存
│   ├── downloader.py    # 附件流式下载、格式校验、重试
│   ├── catalog.py       # 目录持久化与本地状态核对
│   └── scheduler.py     # 定时任务
├── web/                 # 管理页面静态文件
├── seed/catalog.json    # 国家电网初始目录
├── seed/csg_catalog.json# 南方电网五省初始样本和栏目配置
├── data/                # 目录、设置和下载文件，随项目一起迁移
├── tests/               # 离线测试
├── .env.example         # 部署配置模板
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

运行数据统一保存于 `/data`，容器镜像中的 `/app` 保持代码只读布局。建议的数据布局为：

```text
/data/
├── catalog.json
├── download-report.json
├── schedule.json
├── downloads/<省份>/...
└── logs/service.log
```

## 首次启动

要求服务器安装 Docker Engine 24+ 和 Docker Compose v2，并能访问
`https://95598.cn`、`https://95598.csg.cn` 及容器镜像仓库。

首次部署先复制配置模板，并把 UID/GID 调整为当前 Linux 用户：

```bash
cd sgcc-proxy-power-linux
cp .env.example .env
sed -i "s/^SGCC_UID=.*/SGCC_UID=$(id -u)/" .env
sed -i "s/^SGCC_GID=.*/SGCC_GID=$(id -g)/" .env
```

然后编辑 `.env`，将 `SGCC_ADMIN_TOKEN` 换成自己的长随机令牌。启动服务：

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f --tail=200
```

首次启动会载入随项目附带的国家电网目录快照和南方电网五省初始样本；点击网页中的“扫描双电网最新目录”后，会从
95598.cn 和 95598.csg.cn 重新获取目录。国家电网按省公司组织代码遍历公告全部分页，匹配标题中“代理”和“购电”并读取详情附件；南方电网扫描广东、广西、云南、贵州、海南五省，
按标题包含“代理购电”筛选“电价及收费标准”目录，读取全部分页并保存 PDF/图片附件。默认定时任务每天
03:00 扫描，并在发现未获取资料后自动下载。
迁移包中的 `data/downloads` 只保留目录占位文件，首次打开页面会看到完整目录和“未获取”
状态；点击“下载全部未获取”即可开始落盘，文件会持续保存在该目录中。

默认只监听服务器本机的 `127.0.0.1:8765`。可通过 SSH 隧道访问：

```bash
ssh -L 8765:127.0.0.1:8765 user@SERVER
```

随后在自己电脑打开 `http://127.0.0.1:8765/`。

网页顶部“操作令牌”输入 `.env` 中的同一个值。可信局域网访问时，可把
`SGCC_BIND_ADDRESS` 设为 `0.0.0.0`，然后执行 `docker compose up -d --build` 重新加载配置。

然后重新创建容器：

```bash
docker compose up -d --build
```

对公网提供页面时，应保持容器端口只绑定 `127.0.0.1`，在前面部署带 HTTPS 和
身份认证的 Caddy、Nginx 或现有网关。`SGCC_ADMIN_TOKEN` 仅用于应用操作，公网身份
认证仍由前置网关负责，也不要提交到代码仓库。

## 运行与维护

查看健康状态：

```bash
curl -fsS http://127.0.0.1:8765/api/health
docker compose ps
```

手动扫描、下载、定时启停、每日执行时间、分钟间隔和扫描后自动下载均可在网页中操作。
展开省份后可以逐项勾选或选择当前显示项；点击“下载所选”时，服务会先获取缺失附件，再按“电网/省份”生成 ZIP 供浏览器下载，适合从无桌面的 Linux 服务器导出文件。
保存后写入 `data/schedule.json`。环境变量是首次启动默认值；默认 `0 3 * * *` 表示按
`SGCC_TIMEZONE` 每天 03:00 扫描。也可以在首次启动前关闭定时扫描：

```dotenv
SGCC_SCAN_ENABLED=false
```

如果不希望扫描结束后自动下载，把 `SGCC_DOWNLOAD_AFTER_SCAN` 设为 `false`。若是
不使用 Docker 的 Linux 环境，可用 `SGCC_BROWSER_EXECUTABLE` 指定系统 Chromium
路径；Docker 镜像已经内置浏览器，该项保持空白。

若服务器访问 95598 需要 HTTP/SOCKS 代理，在 `.env` 中填写浏览器和附件下载共用的代理，例如：

```dotenv
SGCC_BROWSER_PROXY=http://127.0.0.1:7890
```

更新：

```bash
docker compose build --pull
docker compose up -d
```

停止服务不会删除 `data` 中的数据：

```bash
docker compose down
```

备份整个项目（包含目录、设置及已下载资料）：

```bash
cd ..
tar czf sgcc-proxy-power-linux-backup.tgz sgcc-proxy-power-linux
```

恢复或迁移时解压该压缩包，在新服务器中重新执行 `docker compose up -d --build`。
Compose 默认把宿主机的 `./data` 直接绑定到容器 `/data`，资料不会藏在 Docker
命名卷里。若迁移后 Linux 用户 UID/GID 不同，请更新 `.env` 并确认目录可写：

```bash
mkdir -p data
sudo chown -R "$(id -u):$(id -g)" data
```

## 健康检查约定

`GET /api/health` 应在不访问 95598 的情况下快速返回 HTTP 200：

```json
{"ok":true,"service":"sgcc-proxy-power","version":"1.1.3"}
```

它只用于判断 Web 进程是否仍能响应，不应因为上游网站暂时超时、某次扫描失败或目录
尚未生成而返回失败。需要更严格的就绪状态时，可另设 `/api/ready`，检查 `/data`
可写、目录可解析和调度器状态，但不要把它用于 Docker 自动重启。

## 现有 Windows 版本迁移风险

原有 `sgcc_proxy_power_downloader.py` 和 `95598_interactive_console.py` 不适合作为
Linux 服务入口，主要原因如下：

1. 浏览器路径写死为 `msedge.exe`，并含 PowerShell、`taskkill`、`os.startfile`、
   `ctypes.windll` 和前台窗口控制。服务器版应只调用 Playwright 随镜像安装的 Chromium。
2. 旧服务只绑定 `127.0.0.1`，控制令牌会直接注入公开返回的 HTML；暴露到网络前需要
   独立登录或反向代理认证，并限制跨站请求。
3. 目录、报告、日志和下载文件都相对于脚本目录写入。容器升级后会丢失，必须统一写入
   `/data` 持久卷，并采用临时文件加原子替换，避免进程中止留下半份 JSON。
4. 旧下载使用一次性 `response.content`，大附件会全部进入内存。服务器版应流式下载到
   `.part` 文件，限制最大体积，校验 HTTP 状态、最终域名和文件签名后再原子改名。
5. 前端可提交原始 URL 触发下载。服务端必须仅允许 HTTPS 且主机属于明确的 95598
   附件域名列表，并在每次重定向后复查，避免形成任意地址请求入口。
6. Chromium 需要足够的共享内存，Compose 已配置 `shm_size: 1gb`；扫描结束、超时和
   `SIGTERM` 时都要关闭 page/context/browser，避免长期运行后积累子进程。
7. 定时器只能有一个执行者。当前 Compose 只启动单个应用进程；不要简单增加 Uvicorn
   worker 或 Compose 副本，否则每个进程都会重复扫描和下载。扩容时应把调度器拆为
   独立 worker，并增加跨进程任务锁。
8. 95598 的短时空结果会被拦截，避免覆盖上一份有效目录。新扫描应先写快照，完成字段校验且达到
   合理完整度后再切换为当前目录；失败信息单独记录。
9. 初始附件接近 1 GB，后续会继续增长。需要为项目 `data` 目录配置磁盘告警、备份和
   可选保留策略，并对并行数和请求频率做保守限制。

## 服务器安全与稳定性建议

- 保持无头模式，不在容器中开启远程调试端口，也不把浏览器用户目录对外共享。
- 仅允许应用访问所需的 95598 HTTPS 域名；下载 URL 必须经过服务端白名单验证。
- 所有修改操作使用认证令牌并校验来源；反向代理限制请求体大小和访问频率。
- 记录扫描批次、所选省市区、页签名称、目录数、下载结果和错误，但不要记录令牌。
- 扫描和下载分别设置总超时、单请求重试和并发上限；任务重叠时返回“已有任务运行”。
- 生产环境固定镜像摘要并定期更新 Python、Playwright 和 Chromium 安全补丁。
