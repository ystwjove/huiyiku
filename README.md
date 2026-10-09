# 会议库 / huiyiku

Local meeting knowledge base: import recordings, transcribe on-device with FunASR, review speakers, then generate cited reports and Q&A through **your own** cloud LLM key (BYOK). Audio stays on disk by default; this project never proxies your billing.

会议库是单人、本机存储的会议录音知识库，也支持小团队在可信内网共享同一个库（见「内网共享」）。双击 exe 打开独立桌面窗口。转写在本机完成，音频默认不上传。报告、问答和项目摘要会把**转写文本**发到你自己配置的大模型 API。作者不收 Key、不经手录音。

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

**许可证：[Apache License 2.0](LICENSE)**（可直接使用、修改、再分发，含专利授权）。许可能做什么、必须保留什么、以及录音/外发/FFmpeg 等法律边界，见 **[docs/法律与许可.md](docs/法律与许可.md)**。本页下列三条是摘要，**不是法律意见**。

---

## 请先读这三件事

1. **转写在本机。** 默认使用 FunASR Paraformer，音频不发到云端。本仓库和 UI zip **不捆绑** PyTorch / 模型权重 / FFmpeg / API Key。
2. **报告和问答会外发文本。** 只有在你配置了 LLM Key 并确认条款之后，转写文本才会发到该服务。未配置 Key 时仍可导入、转写、校对、统计和导出。
3. **录音须先获得参会人同意。** 美国多个全员同意州未经全体同意录音可构成刑事犯罪；欧盟工作场景录同事/客户通常落在 GDPR 家庭豁免之外；中国向第三方提供个人信息应告知并取得同意，声纹属敏感生物识别（本版不做）。详见 [docs/法律与许可.md](docs/法律与许可.md)。本软件按 Apache-2.0 **AS IS** 提供。本工具不做同意管理，勾选不能代替守法。

---

## Windows 使用（终端用户）

1. 从 GitHub Releases 下载 `huiyiku-windows-x64.zip`，解压后双击 `Huiyiku.exe`。推荐解压到 `%LOCALAPPDATA%\Programs\Huiyiku`，不要留在「下载」后直接当安装目录删掉。
2. 双击后打开**独立的桌面窗口**（自带标题栏与任务栏图标，不是浏览器标签页）。窗口用系统自带的 WebView2（Edge 内核）渲染；极少数未安装 WebView2 的机器会自动回退为系统浏览器访问 `http://127.0.0.1:8787`。**关闭窗口即退出**（后台服务随之停止）。端口固定 8787，被**其它程序**占用时会报错退出，不要改端口「躲占用」。
3. **自备 FFmpeg。** 放入 PATH，或放到 exe 同级 `ffmpeg\ffmpeg.exe`。本项目不代为分发 FFmpeg 二进制。官方构建见 [FFmpeg Windows builds](https://www.ffmpeg.org/download.html)。
4. **要转写再装 ASR 组件。** 下载 `huiyiku-asr-windows-x64.zip`（约数 GB，含 torch），解压到程序目录的 `asr\`（开发时即仓库根目录下的 `asr\`），其中应有 `huiyiku-asr.exe`。源码开发也可在仓库里建 `.venv-asr` 并安装 `requirements-asr.txt`，权重放到 `asr\models\`。
5. 核对 Release 附带的 `SHA256SUMS`。exe **未做代码签名**，SmartScreen 或杀毒软件可能误报。请以本仓库 Actions 构建的 zip 为准，不要从第三方网盘下载。

录音、数据库、转写和 ASR 组件默认都在程序目录下：`meeting-data\` 与 `asr\`（开发时即仓库根目录；解压 exe 则在 `Huiyiku.exe` 旁）。**不要把 zip 留在「下载」里当安装目录**，删掉解压文件夹会连库和模型一起删掉。

界面-only 建议 8GB 内存即可浏览。本机转写建议 **16GB 内存 + SSD**；8GB 能试，但请少开浏览器。不需要 GPU。

### 本机构建 Windows zip

```powershell
python -m pip install -r requirements-build.txt
python scripts/package_windows.py --ui
# ASR 包须在已安装 torch/funasr 的解释器里打（日常 CI 不构建）：
# python scripts/package_windows.py --asr
```

产物在 `packaging/dist/`：`huiyiku-windows-x64.zip` 与 `SHA256SUMS`。UI zip **不含** torch、权重、FFmpeg 二进制、`portable.txt`。推送 `v0.1.0` tag 时 GitHub Actions 会打 UI zip 并挂到 Release。

### 硬件与说话人实测（2026-08-21）

| 项 | 结果 |
|---|---|
| 机器 | 64 位 Windows，约 16GB 内存，CPU（无 GPU） |
| 管线 | FunASR 1.4.2 `paraformer-zh` + `fsmn-vad` + `ct-punc` + `cam++`，权重从 `asr_home/models` 加载 |
| 官方短示例 | 直接输出 `SPEAKER_00` 与毫秒时间戳；说话人标签由 FunASR `sentence_info.spk` 给出，第一版默认启用 |
| 1 小时真会 CER / 峰值内存 | 未用真实会议录音（不入库）。官方短示例进程峰值工作集 **5.17 GB**（含 CAM++），低于 8GB 硬门槛。长音频走 VAD + 60 秒批，详见 [`docs/hardware.md`](docs/hardware.md) |

精确 model ID / revision / SHA256：[`docs/model-manifest.json`](docs/model-manifest.json)。

---

## 开发者运行

```powershell
git clone <this-repo> huiyiku
cd huiyiku
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pytest
python -m huiyiku --help
```

默认 venv **不要**安装 torch。本机转写请另建 `.venv-asr`（见 `requirements-asr.txt`）。

源码一条命令拉起 API + Worker + 桌面窗口（先构建前端；开发机上若无 WebView2 会回退到系统浏览器）：

```powershell
cd web; npm install; npm run build; cd ..
python -m huiyiku
```

只起 API / Worker：`python -m huiyiku --serve` 与 `python -m huiyiku --worker`。

切片 0 离线脚本（不经 UI）：

```powershell
python -m huiyiku pipeline --input path\to\meeting.wav --out-dir pipeline-out
```

需要 Python 3.12+、Git、FFmpeg；前端构建再加 Node。源码在 Windows / macOS / Linux 上均可开发，**终端用户验收以 Windows exe 为准**。

---

## 配置 LLM（BYOK）

密钥只放在数据目录的 `secrets.json` 或环境变量里，**不要**写入 `app.json`，更不要提交到 Git。

环境变量示例（占位符，禁止填入真实 Key）：

```text
HUIYIKU_LLM_API_KEY=sk-...
HUIYIKU_ACCESS_TOKEN=
```

`secrets.json` 形状：

```json
{
  "asr_api_key": "",
  "embedding_api_key": "",
  "llm_api_key": "sk-..."
}
```

向导里可登记任意 OpenAI-compatible 端点（DeepSeek、通义 compatible、硅基流动、OpenAI 等）。**只支持 Chat Completions 格式**（`/v1/chat/completions`）；不支持 Anthropic Messages（`/v1/messages`）或 OpenAI Responses（`/v1/responses`）格式，填这两类端点会连不通。Base URL 填到域名或 `/v1` 均可，完整路径会自动补全。启用远程服务前必须确认该厂商的条款、数据保留和跨境传输。本项目不内置、不代理账单。

应用配置 `app.example.json`（真实 `app.json` 任何位置都不要入库）：

```json
{
  "data_dir": "",
  "host": "127.0.0.1",
  "port": 8787,
  "ffmpeg_path": "",
  "asr_home": ""
}
```

---

## 转写质量与热词

FunASR Paraformer 对专名（路名、人名、产品名、术语）识别较弱。在数据目录 `settings.json` 加热词可显著改善：

```json
{
  "hotwords": "某某路 huiyiku 智能会议 例会"
}
```

空格分隔，改完对**新转写**生效（已有的转写可用会议页「重新转写」重跑）。转写文本支持**双击分段直接修正**文字。

---

## 内网共享（可选）

默认**关闭**，只有本机能打开。内部小范围使用时，在设置页打开「内网共享」开关即可，再点一次就关掉。不用改 `app.json`，也不用重启。不要求访问令牌。这是「轻共享」，不是多用户产品。

开启步骤：

1. 本机打开会议库，进入「设置」，打开「内网共享」。
2. 首次对外监听时 Windows 防火墙可能弹「允许访问」，勾选允许（建议仅专用网络）。
3. 把设置页列出的地址发给同事，例如 `http://192.168.1.20:8787`。关掉开关后，这些地址立即失效。

必须知道的限制：

- **全程明文 HTTP。** 转写文本和音频流在内网线路上不加密，同网段抓包可见。仅在可信内网使用。
- **无多用户隔离。** 没有按人区分的账号、权限和修改审计；两人同时校对同一场会议可能互相覆盖。
- **转写任务全员排队。** 转写 worker 单进程，一人转长会时其他人的任务等待。
- **宿主机在线才可用。** 关窗口/关机即断线。数据仍只存宿主机一份，备份同「数据目录与备份」。

---

## 数据目录与备份

| 模式 | 配置文件 | 默认数据目录（录音/库） | ASR |
|---|---|---|---|
| exe | exe 旁 `app.json` | exe 旁 `meeting-data` | exe 旁 `asr` |
| 开发 | 仓库目录 `app.json` 或 `HUIYIKU_APP_JSON` | 仓库目录 `meeting-data` | 仓库目录 `asr`，或仓库内 `.venv-asr` |

备份：退出程序（以便 WAL checkpoint）后，复制 `app.json` 与整个 `meeting-data\`（含 `secrets.json`）。**默认不必备份 `asr\`**，体积大且可再下载。备份副本等同于持有云账号，请自行保管。不要把库放到 OneDrive 或网络盘。

恢复后可以浏览已有转写；要转写新会议，目标机器仍需安装 ASR zip 到 `asr_home`。

---

## 费用

本项目没有强制订阅。作者不收费。推荐用法下，云账单里通常只有 **你自己的 LLM token**。

| 项目 | 要不要付钱 |
|---|---|
| 本机 FunASR 转写 | 否（一次性下载组件与权重） |
| 报告 / 问答 / 组摘要 | 按你的 LLM 厂商计费 |
| 远程 ASR / Embedding | 默认关；手动打开才会产生费用和外发 |
| GitHub 源码与 Release zip | 否 |

一小时中文会的转写大约 2–4 万 token；再加摘要/问答，费用随模型和厂商变化，常见按量套餐大约是 **几角到数元人民币** 量级。具体以账单为准。不配 LLM Key 则零云账单，只是没有自动报告和问答。

---

## 录音合法性（简版）

完整说明与法域表：[docs/法律与许可.md](docs/法律与许可.md) 第 4 节。本工具便利化**你已有权处理的**录音的转写，不是窃听工具。

| 法域 | 要点 |
|---|---|
| 美国全员同意州（CA/FL/IL/MD/MA/PA/WA 等） | 未经全体参会人同意录音在若干州可构成刑事犯罪；跨州适用更严一方 |
| 欧盟 GDPR | 家庭豁免仅限纯粹个人活动；工作场景录同事/客户通常要告知，录音者可能成为数据控制者 |
| 中国 PIPL | 第 72 条个人事务豁免有限；向云厂商提供文本应告知并同意。声纹属敏感生物识别（第 28–29 条），**本版不做声纹** |

首次启动会确认：你理解须符合当地法律并获得参会人同意；以及是否同意把文本发到自己配置的 API。项目**不做同意管理库**。勾选不能代替守法。系统不取得用户内容所有权。

---

## 仓库卫生

真实会议录音、转写、评测样本、带真名的截图**不要提交**。测试只用合成音频、人造转写和脱敏 fixture。

仓库目录说明见 [`docs/仓库结构.md`](docs/仓库结构.md)。实现方案（给开发者，不是本 README）见 [`docs/开发方案.md`](docs/开发方案.md)。文档索引：[`docs/README.md`](docs/README.md)。

---

## 许可证与第三方

- 本项目源码与 UI zip：**[Apache-2.0](LICENSE)**，可直接使用、修改、再分发（须保留 LICENSE 与 [NOTICE](NOTICE)）。说明：[docs/法律与许可.md](docs/法律与许可.md)
- 第三方摘录：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- 入站例外登记：[`docs/license-exceptions.json`](docs/license-exceptions.json)
- 本机权重登记：[`docs/model-manifest.json`](docs/model-manifest.json)

默认 Python / Node 依赖为 MIT / MIT-0 / Apache-2.0 / BSD-2/3 / ISC / 0BSD。UI 锁文件不含 `torch` / `funasr` / `numpy`。**FFmpeg 由用户自备**，本仓库不内嵌其二进制（避免把 LGPL/GPL 构建义务带进默认 zip）。云 LLM 是你与厂商的服务合同，不是本项目的开源许可。
