# SJTU Canvas Assistant

一个从零搭建的本地 Python CLI 框架，用 Canvas Access Token 读取上海交通大学 Canvas 数据，下载课程文件，并生成可供后续 Codex/MCP 使用的本地索引。

默认 Canvas 主站：

```text
https://oc.sjtu.edu.cn
```

第一版不模拟 jAccount 登录，不保存密码，不自动提交作业。课程、文件、作业等基础数据走 Canvas API；视频模块通过 Canvas Access Token 发起 sessionless LTI，再只读调用 SJTU 视频 API。

## 参考、致谢与许可证

本项目阅读并借鉴了学长项目 [SJTU Canvas Helper](https://github.com/Okabe-Rintarou-0/SJTU-Canvas-Helper) 的接口方向。该参考项目使用 MIT License，版权声明为 `Copyright (c) 2025 Zihong Lin`。本项目同样以 MIT License 开源，并保留对参考项目的明确链接和致谢。

- Canvas 基础数据走 `/api/v1/...` 和 Bearer token。
- 课程列表包含 `teachers` 和 `term`。
- 文件、文件夹、模块、作业分别通过 Canvas API 拉取，并处理分页。
- SJTU 视频/字幕/PPT 回放通过 Canvas 外部工具 8329 接入。当前实现优先使用 Canvas `sessionless_launch` API 发起 LTI，不需要模拟 jAccount 表单登录。
- MCP Server 直接复用本项目的 `CanvasClient`、`CourseSyncer`、`SJTUVideoClient` 和索引模型，而不是重新写一套访问逻辑。

本项目不会照搬 Tauri 桌面 App，只提供可扩展的本地 CLI、Python 模块和 MCP stdio server。如果未来直接复制或改写参考项目中的实质代码，应继续保留其原 MIT 许可证声明。

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

# 可选调试项；通常留空，由 video 命令自动通过 Canvas LTI 换取。
SJTU_VIDEO_TOKEN=
SJTU_VIDEO_COURSE_ID=
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

列出和查看自己已经提交过的作业：

```bash
sjtu-canvas submissions 12345
sjtu-canvas submissions 12345 --all
sjtu-canvas submission 12345 ASSIGNMENT_ID
```

下载自己提交过的作业附件：

```bash
sjtu-canvas download-submissions 12345
sjtu-canvas download-submissions 12345 --assignment-id ASSIGNMENT_ID
sjtu-canvas download-submissions 12345 --latest-only
```

默认会保留 Canvas submission history 中各次提交的附件，路径形如：

```text
data/课程名 [课程ID]/submissions/作业名 [作业ID]/attempt-1/文件名
```

这几个命令只读取和下载你自己的提交记录，不会提交、修改或评论作业。JSON/MCP 输出会隐藏附件下载 URL。

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

查看视频/字幕模块状态：

```bash
sjtu-canvas video-status 12345
sjtu-canvas video status
```

列出某门课的视频：

```bash
sjtu-canvas video list 12345
sjtu-canvas video list 12345 --json
```

读取某个视频的播放元数据：

```bash
sjtu-canvas video info 12345 VIDEO_ID
```

导出字幕为 SRT：

```bash
sjtu-canvas video subtitle 12345 VIDEO_ID
sjtu-canvas video subtitle 12345 VIDEO_ID --output data/subtitles/example.srt
```

读取 PPT 回放切片元数据：

```bash
sjtu-canvas video ppt 12345 VIDEO_ID
```

下载 PPT 回放图片并合成 PDF：

```bash
sjtu-canvas video ppt-export 12345 VIDEO_ID
sjtu-canvas video ppt-export 12345 VIDEO_ID --output data/ppt/example
sjtu-canvas video ppt-export 12345 VIDEO_ID --no-pdf
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
- 视频/字幕/PPT 回放的只读接入状态
- MCP Server 工具接口状态

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

## 视频、字幕、PPT 回放

此 fork 保留 [Aspirin0000/sjtu-canvas-assistant](https://github.com/Aspirin0000/sjtu-canvas-assistant)
的 MIT 许可证，适配 SJTU 的“课堂视频新版”。认证链路为：

1. 使用 Canvas Access Token 调用 `external_tools/sessionless_launch?id=8329`。
2. 在同一个 HTTP 会话中提交新版 `/jy-lti-adapter/lti/canvas/` OIDC 初始化与启动表单。
3. 从最终跳转取得临时 `jwt_token`，调用 `/lms/launch-context` 读取 `canvasRecord.teachingClassId`。
4. 用 `jwt-token` 请求头访问 `/jy-application-resourcemanage` 的只读接口。

`video list` 按 `teclIds` 分页获取 `/v1/subject_vod_list_new`，同时保留平台状态，
输出 `playable` 区分可点击、不可点击和未知；不可点击不自动等于没有录制。
`video info` 用列表中的录播 ID 查询 `/v1/course_vod_urls_new`。
录播 ID、教学班 ID 与流条目中的 `courId` 不可互换。

字幕通过 `/v1/course/ai/translate/{录播ID}?useOriginal=true` 获取；AI PPT 通过
`/v1/course/ai/ppt?courseId={录播ID}` 获取。PPT 的 `docList.imageSeekTime` 为秒，
客户端转换为既有导出格式使用的毫秒偏移。

平台可以提供回放但没有 AI 素材。成功响应中的 `data:null` 属于素材不可用，
不作为网络或认证失败；HTTP 错误、失败的业务状态和不支持的响应结构仍明确报错。

- `video subtitle` 的 JSON 包含 `availability`。没有有效字幕时不生成空 SRT，
  不覆盖已有文件，`srt_path` 为 null。
- `video ppt --json` 在没有 AI 切片时返回空列表；文本输出明确提示无 AI 切片。
- `video ppt-export` 在切片不可用时返回 `availability=unavailable`，不生成 PDF，
  也不把已有 PDF 误报成本次结果。
- 回放中的 PPT 视频通道、AI 静态切片与 Canvas 附件 PPT 是三种不同资源。

正常使用只配置 Canvas token。可选调试变量 `SJTU_VIDEO_TOKEN` 必须是新版 JWT，
`SJTU_VIDEO_COURSE_ID` 为教学班 `teachingClassId`，不是 Canvas 课程 ID，也不是旧版 courId。
旧视频平台临时 token 不再兼容，应清空这两个变量让 LTI 自动认证。

本工具不做本地语音识别、不下载视频本体、不调用大模型生成字幕。CLI/MCP 输出隐藏临时
认证与播放 URL；PPT 图片的带签名 URL 在输出中脱敏，原始链接只在内存中用于下载。

## MCP 接入

项目已经把能力拆成可复用模块：

- `CanvasClient`: Canvas API、分页、错误处理、下载流
- `CourseSyncer`: 课程元数据拉取、下载规划、本地同步
- `SJTUVideoClient`: 视频 LTI 认证、视频列表、详情、字幕、PPT 切片元数据
- `PPTAssetExporter`: PPT 回放图片下载和 PDF 合成
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
- `list_my_submissions`
- `get_my_submission`
- `get_course_bundle`
- `plan_course_download`
- `generate_course_index`
- `download_course_files`
- `download_my_submission_files`
- `video_status`
- `resolve_video_auth`
- `list_course_videos`
- `get_video_info`
- `get_video_subtitle`
- `get_video_ppt_slices`
- `download_video_ppt`

作业相关能力只读；提交记录工具只下载你自己已提交的附件，不开放提交、评论或改分。视频导出工具只写本地文件，不自动发布或上传。

## 开发验证

```bash
python -m pytest
sjtu-canvas --help
sjtu-canvas video-status
sjtu-canvas video --help
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"manual","version":"0"}}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n' | sjtu-canvas-mcp
```

如果没有真实 token，也可以运行上述 mock 测试和 help 命令验证框架。
