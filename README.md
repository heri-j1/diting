# 谛听 Diting — 会议录音转文字（本地部署）

中文会议专用。开会时**置顶字幕窗实时出字幕**，散会后自动精转成**带时间戳、带说话人标签的文稿**、按热词校正专有名词，并调用 qwen 生成**会议纪要**。全程本地运行，数据不出本机。

## 快速开始

**Windows 一键使用**：双击 `start.bat`（自动开浏览器）；`stop.bat` 关闭；首次部署双击 `install.bat`（装依赖+下模型，10-30 分钟）。

手动方式：

```bash
# 1. 安装依赖（torch 被 Windows 智能应用控制拦截，走 onnxruntime 路线）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 2. 下载模型（约 1.6GB，int8 量化 onnx，从 ModelScope）
.venv/Scripts/python download_models.py

# 3a. 图形界面（推荐）：浏览器操作全部功能
.venv/Scripts/python webapp.py        # 自动打开 http://127.0.0.1:8321

# 3b. 命令行：一条命令全流程
.venv/Scripts/python record_meeting.py
```

## Web 界面

浏览器（默认自动打开 `http://127.0.0.1:8321`，仅本机可访问）里完成全部操作：

- **控制**：开始/停止录音、状态徽章、计时、双轨音量条、**📱 手机扫码查看**（弹窗显示局域网二维码，手机扫码即看实时字幕；含"等待连接/已连接"状态与一键开启局域网+重启）
- **实时字幕**：定稿行（白）+ 正在说（绿），随说随出
- **文稿**：停止后自动渲染带时间戳的文稿与会议纪要
- **历史文稿**：左侧列表，点击即看任意一场
- **设置**：热词表编辑、qwen 接口配置（api_base/key/model）

`webapp.py` 只做会话编排与通信，采集/模型/转写逻辑与命令行共用同一套模块。

## 日常使用

```bash
.venv/Scripts/python record_meeting.py              # 全流程（推荐）
python record_meeting.py --no-window                # 字幕输出到终端，回车停止
python record_meeting.py --no-live                  # 不开实时字幕
python record_meeting.py --no-transcribe            # 只录不转
python record_meeting.py --no-summarize             # 转写但不生成纪要
python record_meeting.py --stop-after 1800          # 30 分钟后自动停止
python record_meeting.py --stop-after 1800 --no-transcribe --no-summarize  # 纯录+字幕

.venv/Scripts/python transcribe.py recordings/      # 事后转写已有录音
.venv/Scripts/python transcribe.py 某段音频.mp3     # 转写任意音频（wav/mp3/flac…）
.venv/Scripts/python summarize.py 文稿.md           # 单独生成纪要
.venv/Scripts/python check_devices.py               # 检查录音设备
```

- **字幕窗操作**：拖动移动位置；■ 按钮 / Esc 停止录音
- **输出**：`recordings/时间戳_loopback.wav`（会议对端声音）、`时间戳_mic.wav`（本机麦克风，无麦克风自动跳过）、`时间戳_transcript.md`（文稿+纪要）、`时间戳_transcript.srt`（精确时间轴字幕，剪辑软件直接可用）

## 导出 SRT / Word

- 转写完成时**自动生成 SRT**（双轨按时间合并，块起止为 VAD 真实时间）
- 网页文稿卡右上角「导出 SRT / 导出 Word」按钮，随时转换任意历史文稿
- 命令行批量转换（生成 .srt + .docx）：

```bash
.venv/Scripts/python export.py 文稿_transcript.md
.venv/Scripts/python export.py recordings/     # 批量
```

## 整体架构

```
【开会中】
 会议软件(腾讯会议/Zoom/钉钉…) ─ 系统声 ─→ WASAPI loopback ─┐
 本机麦克风(你说的) ────────────────────────────────────────┤ capture.py 双轨轮询采集
                                      │ 按 20ms 轮询，静音补零帧（时间轴对齐墙钟）
                    ┌─────────────────┴──────────────────┐
                    ▼                                    ▼
           wav 双轨落盘（存档）                live_subtitles.py 实时字幕【第一遍】
                                              流式 FSMN-VAD（100ms 块，静音断句）
                                              流式 Paraformer-online（600ms 块增量出字）
                                              句尾标点定稿 → subtitle_window.py 置顶字幕窗
                                                （白色定稿行 + 绿色实时行，拖动/■停止/Esc）

【散会后】record_meeting.py 回车/■停止后自动执行
 transcribe.py【第二遍·精转】           hotwords.py 热词校正              summarize.py 纪要
 Paraformer-large 离线重转      →      本地拼音模糊校正(离线)      →     qwen (OpenAI 兼容接口)
 FSMN-VAD 切句 + [mm:ss] 时间戳        LLM 语义校正(配接口后启用)         摘要/结论/待办
 CT-Transformer 标点                                                      （未配置=模拟模式）
            └──────────────→ 时间戳_transcript.md（文稿 + 会议纪要）←──────────────┘
```

实时字幕的识别误差不会留在最终文稿里——散会后离线大模型整篇重转（2pass），实测能把"第一句话"（实时误识为"依据化"）这类错误全部纠正。

| 文件 | 职责 |
|---|---|
| `record_meeting.py` | 命令行全流程入口（采集+字幕窗+精转+纪要） |
| `webapp.py` + `static/index.html` | Web 界面（FastAPI 后端 + WebSocket + 单页前端） |
| `capture.py` | 双轨采集：WASAPI loopback + 麦克风，轮询式、静音补齐、音频回调 |
| `live_subtitles.py` | 实时字幕（流式 VAD + 流式 ASR），Console/Window 双显示端 |
| `subtitle_window.py` | 置顶半透明字幕窗（tkinter） |
| `transcribe.py` | 会后精转：VAD 切句 → 识别 → 时间戳 → 标点 |
| `hotwords.py` | 热词校正（拼音模糊 + LLM 两层） |
| `diarize.py` | 说话人分离（CAM++ 声纹 + 余弦层次聚类） |
| `summarize.py` | 会议纪要（qwen OpenAI 兼容接口 + 模拟模式） |
| `export.py` | SRT 字幕 / Word 文档导出（转写后自动 + 命令行批量） |
| `download_models.py` | 模型下载（HTTP 直连 ModelScope） |
| `models/` | 本地模型（gitignore，约 1.5GB） |
| `hotwords.txt` / `config.json` | 热词表 / 纪要接口配置（gitignore，仓库有 example） |

## 热词定制

专有名词（人名/产品名/术语）容易转错，写在 `hotwords.txt`（一行一个，# 注释）即自动校正：

- **本地拼音模糊校正**（离线，默认生效）：同音字（盾太郎→钝太狼）、字母替代（谛听→d听/di听）等
- **LLM 语义校正**（配置 qwen 接口后自动启用）：覆盖近音字（别→扁）等拼音法治不了的情况

## 说话人分离

"对方轨"自动区分多位发言人，文稿行带 `[说话人N]` 标签（amber 高亮），纪要按人归属发言：

- 模型：CAM++ 声纹（onnx，192 维，`models/campplus_sv.onnx`）
- 流程：VAD 切段（≥600ms）→ 逐段声纹 → 余弦层次聚类 → 单段簇就近合并 → 按时间重叠归属到转写块
- 配置：`diarize: true/false`、`diarize_threshold: 0.4`（余弦距离，调小更易合并）、`num_speakers`（强制人数，缺省自动）
- 双真实音源实测：成对准确率 100%

## 会议纪要（qwen）

转写完成后自动调用 qwen 生成纪要（主题/摘要/讨论点/结论/待办），追加在文稿末尾。复制 `config.example.json` 为 `config.json` 并填入 OpenAI 兼容接口：

```json
{ "api_base": "https://xx/v1", "api_key": "sk-...", "model": "qwen-plus" }
```

`api_base` 留空 = 模拟模式（输出格式示例，标注"模拟"字样）。

## 安全 · 信任栅栏

借鉴 DSH 小鲸鱼 widget 的安全模型，让"局域网看字幕"与"操作安全"并存：

- **读开放**：`config.json` 里 `lan_access: true`（或网页设置勾选）后监听 `0.0.0.0`，局域网设备（手机/平板）可看实时字幕、文稿、导出
- **写锁死**：所有写操作（开始/停止录音、改热词、改配置、归档删除）**仅接受本机来源**（loopback/本机网卡 IP）或 `admin_hosts` 白名单，其余一律 403
- **跨站拦截**：带 `Origin` 的请求必须与 Host 同源或来自白名单（含 WebSocket 握手），防恶意网页驱动浏览器打内网接口 / DNS rebinding
- **凭证不下发**：api_key 任何接口都不回显原文（打码显示），网页保存时空值不覆盖
- 默认关闭局域网（仅 127.0.0.1）；开启后重启服务生效

## 已验证

- ✅ 端到端：扬声器播放语音 → loopback 采集 → 转写，逐字一致；真实配音（1.mp3）转写完整连贯
- ✅ 实时字幕：流式 VAD 断句 + 增量出字，CPU 处理速度约为实时的 7 倍；字幕窗置顶渲染、Esc/自动停止实测
- ✅ 时间戳：与 wav 内实际声音位置误差 < 1s；静音补齐后 wav 时长 = 真实时长
- ✅ 热词：盾太郎→钝太狼、地听/卞老大→谛听/扁老大 实测生效；LLM 层 别老大→扁老大
- ✅ 说话人分离：双真实音源成对准确率 100%；VAD 级归属正确，停顿碎片自动并入邻近说话人
- ✅ 纯 CPU 运行（onnxruntime），模型加载约 10s

## 技术说明

- **模型**：FunASR onnx int8 量化版——Paraformer-large（离线识别）、Paraformer-online（流式识别）、FSMN-VAD（离线/流式切句）、CT-Transformer（标点）、CAM++（声纹，说话人分离），共约 1.6GB
- **不用 torch**：Windows「智能应用控制」(Smart App Control) 会拦截 torch 的 DLL 加载（WinError 4551），故全程 onnxruntime；代价是 FunASR 的 SeACo 热词模型（无公开 onnx 版）无法使用，热词改由后处理校正实现
- **录音**：Windows 10+ 自带 WASAPI loopback，无需虚拟声卡；双轨分开 = 天然区分"我 / 对方"
- **注意事项**：录音请事先告知参会者；loopback 静音期由程序补零帧保持时间轴连续

## 下一步（可选）

- 真实会议实测（用网页或命令行跑一场，按实际痛点迭代）
- GPU 加速（需关闭智能应用控制，不可逆；当前 CPU 已有 7 倍实时余量，优先级低）
- 一键安装脚本 / LICENSE（开源准备）
