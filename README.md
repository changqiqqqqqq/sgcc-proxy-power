# 95598 代理购电资料中心

统一仓库，包含 Windows 本地控制台和 Linux/Docker 无头服务两套版本。

## 目录

- `windows/`：Windows Edge 控制台、扫描器和启动脚本。
- `linux/`：Linux/Docker 服务、Web 管理页面、测试和初始目录快照。
- `outputs/`：本地历史运行目录，仅作为开发留档，不作为部署目录。

## Windows

进入 `windows/`，双击 `start_95598_console.cmd` 启动页面；也可运行：

```text
python 95598_interactive_console.py
```

## Linux / Docker

进入 `linux/`，复制 `.env.example` 后按需修改，再执行：

```bash
docker compose up -d --build
```

管理页面默认端口为 `8765`。下载资料和扫描状态保存于挂载的 `data/` 目录。

## 多选导出

展开省份后可勾选多个资料，点击“下载所选”。服务会先补齐缺失附件，再按电网和省份生成 ZIP，浏览器可直接下载。

## 测试

```bash
python -m pytest -q linux/tests
```

运行数据、下载文件、浏览器配置、日志和打包产物已由 `.gitignore` 排除。
