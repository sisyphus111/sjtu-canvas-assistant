# SJTU Canvas Assistant

一个从零搭建的本地 Python CLI 框架，用 Canvas Access Token 读取上海交通大学 Canvas 数据，下载课程文件，并生成可供后续 Codex/MCP 使用的本地索引。

默认 Canvas 主站：

```text
https://oc.sjtu.edu.cn
```

第一版只使用 Canvas API，不模拟 jAccount 登录，不保存密码，不自动提交作业。

## 参考与取舍

本项目阅读并借鉴了 [SJTU Canvas Helper](https://github.com/Okabe-Rintarou-0/SJTU-Canvas-Helper) 的接口方向：

- Canvas 基础数据走 `/api/v1/...` 和 Bearer token。
- 课程列表包含 `teachers` 和 `term`。
- 文件、文件夹、模块、作业分别通过 Canvas API 拉取，并处理分页。
- SJTU 视频/字幕/PPT 回放不是 Canvas token 单独能完成的能力，需要 Canvas 外部工具 LTI 页面和 SJTU 视频站 token/cookie 流程，所以第一版只保留接口占位。
- MCP Server 后续应直接复用本项目的 `CanvasClient`、`CourseSyncer` 和索引模型，而不是重新写一套 Canvas 访问逻辑。

本项目不会照搬 Tauri 桌面 App，只提供可扩展的本地 CLI 和 Python 模块。

## 安装

建议使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

安装后会得到两个等价命令：

```bash
sjtu-canvas --help
sjtu-canvas-assistant --help
```

也可以不安装脚本，直接运行：

```bash
python -m sjtu_canvas_assistant.cli --help
```

## 创建 Canvas Access Token

在 SJTU Canvas 页面中创建个人访问令牌：

1. 打开 `https://oc.sjtu.edu.cn`。
2. 登录后进入左侧 `Account` / `账户`。
3. 打开 `Settings` / `设置`。
4. 找到 `Approved Integrations` / `已批准的集成`。
5. 点击 `New Access Token` / `新建访问令牌`。
6. 填写用途，例如 `sjtu-canvas-assistant-local`，按需设置过期时间。
7. 复制生成的 token。Canvas 通常只显示一次，请妥善保存。

不要把 token 写进代码或提交到 git。

## 配置

复制示例配置：

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
CANVAS_TOKEN=你的 Canvas Access Token
CANVAS_BASE_URL=https://oc.sjtu.edu.cn
CANVAS_DATA_DIR=data
```

`.env` 已被 `.gitignore` 忽略。

也可以临时用环境变量：

```bash
export CANVAS_TOKEN="你的 Canvas Access Token"
```

## 常用命令

验证 token 并显示当前用户：

```bash
sjtu-canvas whoami
```

列出当前账号可访问课程：

```bash
sjtu-canvas courses
sjtu-canvas courses --active-only
```

输出 JSON：

```bash
sjtu-canvas courses --json
```

列出某门课的全部核心信息：

```bash
sjtu-canvas course 12345
```

分别列出文件、文件夹、模块、作业：

```bash
sjtu-canvas files 12345
sjtu-canvas folders 12345
sjtu-canvas modules 12345
sjtu-canvas assignments 12345
```

下载前预检文件数量、总大小和本地路径：

```bash
sjtu-canvas plan 12345
sjtu-canvas plan 12345 --output data --json
```

按课程下载文件并生成索引：

```bash
sjtu-canvas download 12345
```

指定输出目录：

```bash
sjtu-canvas download 12345 --output data
```

只生成索引，不下载缺失文件：

```bash
sjtu-canvas index 12345
```

查看视频/字幕模块后续接入计划：

```bash
sjtu-canvas video-status 12345
```

## 下载与索引行为

下载时会尽量保持 Canvas 文件夹结构。例如 Canvas 中的：

```text
course files/课件/Week 1/intro.pdf
```

会保存为：

```text
data/课程名 [课程ID]/课件/Week 1/intro.pdf
```

路径和文件名会做安全清洗，避免 `/ \ : * ? " < > |` 等跨平台非法字符。

如果本地已存在同大小文件，默认跳过，不重复下载。若本地同名文件大小不同，会保留原文件并为新下载文件追加 `__canvas_文件ID` 后缀以避免覆盖。

每次 `download` 或 `index` 会生成：

```text
data/课程名 [课程ID]/course_index.json
data/课程名 [课程ID]/course_index.md
```

索引包含：

- 课程基本信息
- 文件、文件夹、模块、作业
- 本地下载路径和状态
- 文件更新时间
- 视频/字幕/PPT 回放的后续接入说明
- 未来 MCP Server 的候选工具接口

## 错误处理

已覆盖的常见错误：

- token 缺失：提示设置 `CANVAS_TOKEN`
- 401：token 无效或过期
- 403：无权限访问课程或资源
- 404：课程、文件夹或资源不存在
- 网络超时/连接失败
- 文件名非法字符清洗
- 下载文件大小不匹配
- 本地文件名冲突

错误信息不会打印 token。

## 视频、字幕、PPT 回放后续接入

根据参考项目实现，SJTU 课程视频大致流程是：

1. 通过 Canvas 网页会话访问 `/courses/{course_id}/external_tools/8329`。
2. 解析 LTI/OIDC 表单并提交到 `v.sjtu.edu.cn`。
3. 从跳转 URL 中取得 `tokenId`。
4. 调用 `getAccessTokenByTokenId` 换取 SJTU 视频 API token 和 `canvasCourseId`。
5. 调用 `findVodVideoList` 列出视频。
6. 调用 `getVodVideoInfos` 获取播放地址。
7. 调用 `transfer/translate/detail` 获取字幕。
8. 调用 `vod-analysis/query-ppt-slice-es` 获取 PPT 切片，再合成为 PDF。

这条链路需要已登录的 Canvas/JAccount Web session 或等价 cookie。第一版不做网页登录自动化，也不读取密码，所以只在 `video_stub.py` 中保留接口和端点常量。

## MCP 后续接入

项目已经把能力拆成可复用模块：

- `CanvasClient`: Canvas API、分页、错误处理、下载流
- `CourseSyncer`: 课程元数据拉取、下载规划、本地同步
- `indexer`: JSON/Markdown 索引
- `models`: 稳定数据模型

本地 MCP stdio server 已提供，启动命令：

```bash
sjtu-canvas-mcp
```

也可以通过主 CLI 启动：

```bash
sjtu-canvas mcp
```

在支持 MCP 的客户端中可配置为：

```json
{
  "mcpServers": {
    "sjtu-canvas": {
      "command": "/Users/hebing/Documents/Canvas/.venv/bin/sjtu-canvas-mcp",
      "cwd": "/Users/hebing/Documents/Canvas"
    }
  }
}
```

`cwd` 建议指向项目根目录，这样 server 能读取本地 `.env`。不要把 `.env` 提交到 git。

当前暴露的工具：

- `get_current_user`
- `list_courses`
- `get_course`
- `list_course_files`
- `list_course_folders`
- `list_course_modules`
- `list_course_assignments`
- `get_course_bundle`
- `plan_course_download`
- `generate_course_index`
- `download_course_files`
- `video_status`

作业相关能力只读，不开放提交。

## 开发验证

```bash
python -m pytest
sjtu-canvas --help
sjtu-canvas video-status
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"manual","version":"0"}}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' | sjtu-canvas-mcp
```

如果没有真实 token，也可以运行上述 mock 测试和 help 命令验证框架。
