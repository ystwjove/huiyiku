# Contributing to 会议库 (huiyiku)

Thank you for considering a contribution. By submitting a pull request or other contribution, you license your work under the Apache License 2.0, the same license as this project. You represent that you have the right to do so.

请用中文或英文描述变更。Issue 与 PR 中不要粘贴真实录音、转写、密钥。

## 开发环境

- Python 3.12+（方案目标为 3.12；3.13 可用于本地）
- Node.js：切片 1 起前端需要；切片 0 仓库骨架不强制
- FFmpeg：转写流水线需要，可放在 PATH 或 `ffmpeg\ffmpeg.exe`
- 本机 ASR：另见 `requirements-asr.txt`。**默认 venv 不要安装 torch / FunASR**

Windows 克隆后不要关闭 `core.autocrlf`。源码以 `.gitattributes` 的 `eol=lf` 为准。提交前执行一次：

```text
git add --renormalize .
```

### 安装（不含 ASR）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
```

Linux / macOS 把激活命令换成 `source .venv/bin/activate`。

前端：

```powershell
cd web
npm install
npm run build
```

源码一条命令（需已 build 前端）：`python -m huiyiku`

公开仓库只含本项目。PR 默认按 Apache-2.0 贡献。不要提交 `meeting-data/`、`asr/` 权重、真实 `app.json` 或 Key。

本机 ASR venv：

```powershell
python -m venv .venv-asr
.\.venv-asr\Scripts\python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
.\.venv-asr\Scripts\python -m pip install -r requirements-asr.txt
.\.venv-asr\Scripts\python -m pip install --no-deps -e .
```

权重下载到仓库根目录 `asr/models`（或 `HUIYIKU_ASR_MODEL_DIR`）。不要提交 `asr/`。

Windows zip（构建机，不进运行时锁文件）：

```powershell
python -m pip install -r requirements-build.txt
python scripts/package_windows.py --ui
```

日常 CI 不打 ASR zip。`requirements.lock` 不得出现 pyinstaller。

### 测试与门禁

```powershell
python -m pytest
python -m ruff check src tests scripts
python scripts/check_lockfile.py
python scripts/check_licenses.py
python scripts/check_repo_hygiene.py
```

CI 在无云厂商 Key、不安装 torch 的条件下必须保持绿色。不要把真实 Key 配进 GitHub Secrets 来跑评测。

## 不要提交

- `app.json`、`secrets.json`、`.env`、`meeting-data/`、`asr/`
- 媒体文件（wav/mp3/m4a/mp4 等）、SQLite 库、真实转写和报告
- 单文件超过 1MB（锁文件除外）
- 把 `torch`、`funasr`、`modelscope`、`numpy` 写进 `requirements.lock` / `requirements-dev.lock`

程序发布号只写在 `pyproject.toml` 的 `[project].version`。不要在源码里再写死第二份版本号。

## ASR extra

构建或调试本机转写时，使用单独的 venv 和 `requirements-asr.txt`。精确模型 ID、revision、checksum 在切片 0 实测后写入 `docs/model-manifest.json`。默认 UI 安装路径不得 import `torch` 或 `funasr`。

## 许可

本项目与入站贡献均为 **Apache-2.0**（全文 [LICENSE](LICENSE)）。你可以如何使用、再分发时必须保留什么、以及录音/FFmpeg/云 API 等法律边界，见 [docs/法律与许可.md](docs/法律与许可.md)。

默认依赖必须落在 MIT / MIT-0 / Apache-2.0 / BSD-2-Clause / BSD-3-Clause / ISC / 0BSD。已知例外见 `docs/license-exceptions.json`。PyInstaller 只出现在 `requirements-build.txt`。
