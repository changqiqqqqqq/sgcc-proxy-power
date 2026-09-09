# 95598 代理购电资料中心

## 全省抓取结果

国家电网扫描使用 P2019“执行的电价和收费标准”公告接口，按省公司组织代码遍历全部分页，并在本地匹配标题中同时出现“代理”和“购电”的资料。每条公告继续读取详情中的 PDF/图片附件；旧版 `priceinfo` 数据只保留为历史快照，不再作为当前扫描数据源。

全省核对后的 PDF 位于 `provinces_verified` 目录，按省份分组保存；`provinces` 保留为此前探索性点击流程的副本。完整目录见 `all_provinces_pdf_catalog.json` 和 `proxy_power_catalog.json`，下载状态见 `proxy_power_download_report.json`。部分地区发布的是 PNG/JPG，所以资料总数会大于 PDF 数。

交互式控制台同时支持南方电网广东、广西、云南、贵州、海南五省。点击“扫描双电网目录”
后，会打开五个省份提供的“电价及收费标准”入口，按接口返回的总数遍历全部分页，筛选标题含
“代理购电”的资料，并从详情 HTML 中提取 PDF/图片附件。文件保存到
`csg_documents/<省份>/`，按实际格式显示并下载。

## 启动

### 全省目录与下载工具

```text
python sgcc_proxy_power_downloader.py --all
```

也可以双击 `run_sgcc_proxy_download.cmd`，选择重新扫描并下载、使用已有清单下载，或仅扫描。

- `--scan`：重新扫描各省目录并写入目录清单。
- `--download`：使用已有目录清单下载 PDF。
- `--download --province 北京市`：只处理指定省份。

双击 `start_95598_console.cmd`，或在该目录执行：

```text
python 95598_interactive_console.py
```

脚本会自己启动本地网页服务，并在 Edge 打开控制台。若 `8765` 已经被其他程序占用，会自动选择后续可用端口；实际地址会写入 `console_url.txt` 并打印在启动窗口中。

旧的 `95598_interactive_monitor.py` 仍保留为兼容入口，会转到新的控制台脚本。

## 页面功能

1. 首页按省份展示实际识别到的代理购电页签、所选市区、目录资料数、PDF 数、图片/其他数和本地获取进度。
2. 点击省份左侧箭头展开，查看每条资料的名称、格式、已获取/未获取状态、本地文件和网站原始地址。
3. 支持资料名称/电网搜索，以及按 PDF、图片、网页、其他格式和本地状态筛选。
4. 支持单条下载、下载当前省份未获取资料、下载全部未获取资料。
5. 展开省份后可多选资料或选择当前显示项；“下载所选”会先补齐服务器本地缺失文件，再按“电网/省份”打包为 ZIP 下载到浏览器。
6. “扫描全部省份”会重新联网读取网站最新目录；“刷新本地状态”只核对本地文件，不访问网站。

## 文件

- `95598_interactive_console.py`：控制台服务、可见 Edge 会话、扫描和下载逻辑。
- `console.html`：网页控制台界面。
- `interactive_state.json`：标题、PDF 地址、扫描时间和下载记录。
- `interactive_console.log`：运行日志。
- `pdf`：早期北京下载的 PDF 文件。
- `provinces`：全省 PDF 下载目录，按省份分组。
- `provinces_verified`：按页面内组件结果核对后的全省 PDF 下载目录。
- `sgcc_proxy_power_downloader.py`：全省扫描、目录整理和并行下载工具。
- `csg_proxy_power_downloader.py`：南方电网五省资讯公告扫描及 HTML 保存工具。
- `csg_proxy_power_catalog.json`：南方电网目录清单。
- `csg_documents`：南方电网公告 HTML/PDF/图片保存目录。
- `proxy_power_download_report.json`：全省下载状态报告。
- `proxy_power_catalog_view.json`：页面使用的标准化省份与资料明细。
- `proxy_power_catalog_view.csv`：可用 Excel 打开的省份汇总。
- `normalize_proxy_catalog.py`：合并目录结果并刷新本地获取状态。
- `.edge_profile_console`：受控 Edge 的独立配置目录，避免影响日常 Edge。

需要的 Python 组件：

```text
python -m pip install playwright requests
```

如果 Playwright 浏览器组件尚未安装，可执行 `python -m playwright install chromium`；脚本实际调用的是本机 Microsoft Edge。
