# 闲鱼铺货助手

Windows 本地桌面辅助工具，覆盖商品采集发布辅助与客服接待两条业务链：

1. 从获得授权的闲鱼卖家主页采集商品详情、图片和属性规格，并预填发布页；
2. 只读抓取当前账号的客服聊天，在本地整理商品知识、历史问答并生成待人工审核的回复草稿。

程序不会点击闲鱼最终发布按钮。客服消息默认人工审核；每次明确确认后可启用全自动模式，自动回复并处理通过全部本地规则的订单改价。

## 主要能力

- 搜索页发现商品链接，逐条读取详情页描述、售价、图片和属性规格；
- 下载商品图片并导出 Excel；
- 预填宝贝描述、本地图片、单一价格及完全匹配的属性选项；
- 区间价、类目和无法精确匹配的属性交由用户确认；
- 通过本机 CDP 连接已登录的 Chrome 或 Edge；
- 只读同步闲鱼客服会话，入库和发送给模型前执行隐私脱敏；
- 商品知识、历史问答和清洗知识库均保存在本地 SQLite；
- DeepSeek API Key 只保存到系统凭据存储，不写入数据库或日志；
- 客服回复默认进入人工审核队列；全自动模式每次启动都需确认，并保留会话重读、去重和转人工保护。
- 顾客拍下后的订单改价只使用聊天页右上角入口；人工模式由操作员确认，全自动模式仅在型号、成交价、最新消息及价格区间全部校验通过后提交。

## 日常工作流

### 商品采集与发布辅助

1. 在“浏览器设置”启动或连接浏览器，并自行登录闲鱼；
2. 输入 `https://www.goofish.com/personal?userId=...` 格式的卖家主页；
3. 选择采集数量（1–100），开始采集；
4. 在商品表格检查详情并下载图片；
5. 点击“发布辅助”，检查预填结果后手动选择类目并决定是否发布。

### 客服知识与人工审核

1. 在“客服设置”配置 DeepSeek Base URL、模型名称和 API Key；
2. 点击“从当前账号抓取并整理”，只读同步已登录账号的聊天；
3. 可预览并导入普通聊天记录，或导入版本化的清洗知识库 JSON；
4. 在“客服接待”启动人工审核模式；
5. 检查草稿事实、价格和措辞后，再由人工确认后续操作。

清洗知识库导入按文件 SHA-256 幂等处理，不会覆盖已有的非空人工商品事实；知识文档会保留来源会话和来源消息标识。

## 安装与开发运行

要求 Python 3.12 或 3.13：

```powershell
uv python install 3.12
uv venv --python 3.12
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
python -m xianyu_assistant.main
```

使用系统已安装的 Chrome 或 Edge；程序通过本机远程调试端口连接，不需要下载 Playwright 自带浏览器。

## 质量检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests scripts
.\.venv\Scripts\python.exe -m compileall -q src scripts
```

## Windows 发布包

```powershell
.\scripts\build_windows.ps1
```

输出目录为 `dist\XianyuAssistant\`。将整个目录复制到目标 Windows 电脑，双击 `XianyuAssistant.exe` 运行。目标电脑仍需安装 Chrome 或 Edge，并在首次运行时登录闲鱼。

本地数据库和日志默认位于 `%LOCALAPPDATA%\XianyuAssistant\`。应用“客服设置”页提供数据目录入口和过期审计记录清理按钮。

详细安装、验收与数据安全说明见 [用户操作与交付说明](docs/用户操作与交付说明.md)。
