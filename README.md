# 谛听 Diting — 会议录音转文字（本地部署）

中文会议专用：双轨录音（系统声+麦克风）→ **实时字幕** → FunASR 离线精转（带时间戳）→ qwen 会议纪要。全程本地，数据不出本机。

## 使用

```bash
# 全流程：实时字幕 + 录音 + 会后精转 + 纪要（推荐）
.venv/Scripts/python record_meeting.py

# 选项
python record_meeting.py --no-live        # 不开实时字幕
python record_meeting.py --no-transcribe  # 只录不转
python record_meeting.py --no-summarize   # 转写但不生成纪要

# 只看实时字幕的演示
.venv/Scripts/python live_subtitles.py

# 事后转写 / 单独生成纪要 / 设备检查
.venv/Scripts/python transcribe.py recordings/
.venv/Scripts/python summarize.py 文稿.md
.venv/Scripts/python check_devices.py
```

输出：`recordings/时间戳_loopback.wav`（会议对端声音）、`时间戳_mic.wav`（本机麦克风，如无麦克风自动跳过）、`时间戳_transcript.md`（带 `[mm:ss]` 时间戳的文稿 + 自动追加的会议纪要）。

## 会议纪要（qwen）

转写完成后自动调用 qwen 生成纪要（主题/摘要/讨论点/结论/待办），追加在文稿末尾。在 `config.json` 里配置 OpenAI 兼容接口：

```json
{ "api_base": "https://xx/v1", "api_key": "sk-...", "model": "qwen-plus" }
```

- `api_base` 留空 = 模拟模式（输出格式示例，标注"模拟"字样）
- 也可单独生成：`.venv/Scripts/python summarize.py 文稿.md`
- 跳过纪要：录音时加 `--no-summarize`

## 已验证

- ✅ 端到端实测：扬声器播放语音 → WASAPI loopback 采集 → 转写，结果逐字一致
- ✅ CPU 转写（onnxruntime），秒级完成，模型加载约 10s
- ✅ 时间戳精度：ASR 时间戳与 wav 内实际声音位置误差 < 1s
- ✅ 静音补齐：loopback 按墙钟增量补零帧，wav 时长 = 真实时长，时间轴不漂移
- ✅ 实时字幕：流式 VAD 断句 + 流式 Paraformer 增量出字，CPU 处理速度约为实时的 7 倍

## 技术说明

- **转写（2pass 架构）**：
  - 第一遍（实时）：流式 Paraformer-online（600ms 块增量出字）+ 流式 FSMN-VAD（100ms 块，静音断句，`[[beg,-1]]`=开始 / `[[-1,end]]`=结束）
  - 第二遍（会后）：Paraformer-large 离线重转（更准），VAD 段落时间戳，CT-Transformer 标点
- **模型**：FunASR onnx int8 量化版，在 `models/`（约 1.5GB，`download_models.py` 可重下）
- **不用 torch**：本机 Windows「智能应用控制」(Smart App Control) 会拦截 torch 的 DLL 加载（WinError 4551），故采用 onnxruntime 路线，纯 CPU 即可实时转写中文
- **录音**：`capture.py` 轮询式采集（静音期不阻塞，随时可停），WASAPI loopback 录系统声，双轨 = 天然区分"我 / 对方"

## 下一步（可选）

- 置顶小窗显示实时字幕（现在输出到终端）
- GPU 加速：需关闭智能应用控制（不可逆，请自行权衡）
- CAM++ 说话人细分（同一轨内区分多人）
- 热词定制（SeacoParaformer，提升专有名词准确率）
