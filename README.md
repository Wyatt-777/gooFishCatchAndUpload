# 闲鱼铺货助手

面向 Windows 的本地桌面辅助工具：从一个获得授权的闲鱼卖家主页采集有限数量的商品，下载图片，并将单件商品安全预填到发布页。程序不会点击最终发布按钮。

## 工作流

1. 启动应用并在它打开的 Chrome/Edge 窗口中自行登录闲鱼。
2. 粘贴 `https://www.goofish.com/personal?userId=...` 形式的卖家主页链接。
3. 设置本次最多采集数量（1–100），点击“采集卖家商品”。
4. 在商品列表检查结果，点击工具栏“下载图片”。
5. 对需要处理的商品点击“发布辅助”，检查内容后预填发布页；类目和最终发布均由用户手动确认。

## 技术栈

- Python 3.12、PyQt6
- Playwright，通过仅限本机的 CDP 连接 Chrome/Edge
- SQLite 本地会话缓存
- 标准库图片下载
- pytest、ruff

Playwright 必须保留：卖家主页和商品详情是动态页面，纯 HTTP 请求只能得到登录/应用壳，不能稳定读取商品、类目 ID 或完成发布页文件上传。

## 安装与运行

```powershell
uv python install 3.12
uv venv --python 3.12
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
xianyu-assistant
```

运行前请执行一次浏览器安装：

```powershell
playwright install chromium
```

更详细的模块边界、保留的数据和不在范围内的功能见 [卖家主页工作流架构](docs/卖家主页工作流架构.md)。

## Windows 发布包

在开发电脑执行：

```powershell
.\scripts\build_windows.ps1
```

将生成的 `dist\XianyuAssistant\` 整个文件夹复制到其他 Windows 电脑；用户双击其中的
`XianyuAssistant.exe` 即可运行，无需安装 Python。目标电脑仍需安装 Chrome 或 Edge，并在首次
运行时登录闲鱼。
