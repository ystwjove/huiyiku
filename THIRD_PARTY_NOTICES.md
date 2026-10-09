# Third-party notices

会议库 bundles or depends on the following third-party software. Exact pinned
versions are in `requirements.lock` (runtime), `requirements-dev.lock`
(development) and `web/package-lock.json` (frontend). This file is the
human-readable attribution required by Apache-2.0 redistribution.

- **完整许可正文**：`licenses/`（随 UI zip 分发为 `Huiyiku/licenses/`），由
  `scripts/collect_licenses.py` 从本机已安装发行版的 `dist-info` 与基础解释器
  的官方许可文件中提取，不手工转抄。
- **机器可读清单**：`packaging/dist/sbom-huiyiku-<version>.cdx.json`
  （CycloneDX 1.5，由 `scripts/make_sbom.py` 生成），覆盖冻结进 `_internal`
  的 Python 包、前端生产依赖与随 CPython 分发的本地库。
- **白名单与例外**：`docs/license-exceptions.json`。门禁脚本：
  `scripts/check_licenses.py`（许可白名单）、`scripts/check_notices.py`
  （本文件对锁文件与前端依赖的覆盖度）。

Do not add FunASR, PyTorch, NumPy, or ModelScope here as **default** runtime
components. They belong to the optional ASR zip only, whose attribution is in
`THIRD_PARTY_NOTICES_ASR.md`.

## 运行时依赖（冻结进 UI zip）

| Package | Version | License | Notes |
|---|---|---|---|
| FastAPI | 0.141.1 | MIT | Web API |
| Starlette | 1.6.0 | BSD-3-Clause | Transitive via FastAPI |
| Uvicorn | 0.52.4 | BSD-3-Clause | ASGI server |
| SQLAlchemy | 2.0.52 | MIT | Persistence |
| Alembic | 1.19.1 | MIT | Migrations |
| Mako | 1.4.1 | MIT | Transitive via Alembic |
| MarkupSafe | 3.0.3 | BSD-3-Clause | Transitive via Mako |
| greenlet | 3.5.5 | MIT AND PSF-2.0 | Transitive via SQLAlchemy |
| httpx | 0.28.1 | BSD-3-Clause | Remote LLM HTTP client |
| httpcore | 1.0.9 | BSD-3-Clause | Transitive via httpx |
| h11 | 0.16.0 | MIT | Transitive via httpcore / Uvicorn |
| anyio | 4.14.2 | MIT | Transitive via httpx / Starlette |
| idna | 3.19 | BSD-3-Clause | Transitive via httpx / anyio |
| certifi | 2026.7.22 | MPL-2.0 | Transitive via httpx; inbound exception, see `docs/license-exceptions.json` |
| click | 8.4.2 | BSD-3-Clause | Transitive via Uvicorn |
| colorama | 0.4.6 | BSD-3-Clause | Transitive via click |
| Pydantic | 2.13.4 | MIT | Transitive via FastAPI |
| pydantic-core | 2.46.4 | MIT | Transitive via Pydantic |
| annotated-types | 0.8.0 | MIT | Transitive via Pydantic |
| annotated-doc | 0.0.5 | MIT | Transitive via FastAPI |
| typing-extensions | 4.16.0 | PSF-2.0 | Transitive |
| typing-inspection | 0.4.4 | MIT | Transitive via Pydantic |
| jieba | 0.42.1 | MIT | Chinese tokenization for FTS |
| python-multipart | 0.0.32 | Apache-2.0 | Multipart parsing |
| pywebview | 6.2.1 | BSD-3-Clause | Native desktop window (WinForms + WebView2 host) |
| proxy-tools | 0.1.0 | MIT | Transitive via pywebview |
| bottle | 0.13.4 | MIT | Transitive via pywebview (internal server unused; remote URL mode) |
| pythonnet | 3.1.0 | MIT | .NET bridge for pywebview, transitive |
| clr-loader | 0.3.1 | MIT | .NET runtime loading for pythonnet, transitive; pip metadata lacks license, see `docs/license-exceptions.json` |
| cffi | 2.1.1 | MIT-0 | C FFI for clr-loader, transitive |
| pycparser | 3.0 | BSD-3-Clause | C parser for cffi, transitive |
| python-docx | 1.2.0 | MIT | Word (.docx) report export |
| lxml | 6.1.3 | BSD-3-Clause | Transitive via python-docx |
| setuptools | (frozen) | MIT | 被 PyInstaller 从构建环境一并冻结进 `_internal`；运行时不作为 pip 依赖导入 |

## CPython 本体与随其分发的本地库、内嵌组件

这些二进制来自官方 CPython Windows 构建并被一并冻结，pip / Node 元数据里查不到，
因此在此显式登记。正文见 `licenses/cpython-and-native-libs.txt`。

| Component | License | 冻结产物 | Notes |
|---|---|---|---|
| CPython | PSF-2.0 | `python313.dll`、`base_library.zip` | 运行时本体 |
| OpenSSL | Apache-2.0 | `libcrypto-3.dll`、`libssl-3.dll` | 3.x 起为 Apache-2.0 |
| zlib | Zlib | `zlib1.dll` | |
| libffi | MIT | `libffi-8.dll` | |
| expat | MIT | `pyexpat.pyd` | |
| libmpdec | BSD-2-Clause | `_decimal.pyd` | |
| Tcl/Tk | Tcl/Tk（BSD 风格） | `tcl86t.dll`、`tk86t.dll`、`_tkinter.pyd`、`tcl8/`、`tkinter/` | `huiyiku/bootstrap.py` 用 tkinter 做错误弹窗与文件选择框，故有意捆绑 |
| SQLite | blessing / public domain | `sqlite3.dll`、`_sqlite3.pyd` | 白名单例外，见 `docs/license-exceptions.json` |
| Microsoft Visual C++ Runtime | Visual Studio 再分发条款 | `VCRUNTIME140.dll`、`VCRUNTIME140_1.dll` | |

### Microsoft WebView2 SDK binaries (via pywebview)

The UI zip redistributes the following Microsoft binaries from pywebview's
`webview/lib` directory (`_internal/webview/lib/` in the frozen app):
`Microsoft.Web.WebView2.Core.dll`, `Microsoft.Web.WebView2.WinForms.dll`,
`WebView2Loader.dll` (win-x86/x64/arm64), and pywebview's
`WebBrowserInterop.x86/x64.dll`. The Microsoft WebView2 SDK files are
licensed under the Microsoft Software License Terms (WebView2 SDK); they are
redistributable per those terms. The WebView2 **Runtime** itself is not
bundled — it is expected to be present on the user's system (see README).

## Frontend (web/)

| Package | Version | License | Notes |
|---|---|---|---|
| React | 19.2.8 | MIT | UI |
| react-dom | 19.2.8 | MIT | UI |
| scheduler | 0.27.0 | MIT | Transitive via react-dom |
| @tanstack/react-virtual | 3.14.10 | MIT | Transcript virtualizer |
| @tanstack/virtual-core | 3.17.8 | MIT | Transitive via @tanstack/react-virtual |
| Vite | 6.x | MIT | Bundler (dev/build only) |
| @vitejs/plugin-react | 4.x | MIT | Vite plugin (dev/build only) |
| TypeScript | 7.x | Apache-2.0 | Type checker (dev only) |
| @types/react | 19.2.x | MIT | Type declarations (dev only) |
| @types/react-dom | 19.2.x | MIT | Type declarations (dev only) |

正文见 `licenses/frontend.txt`（构建产物里 React 等以打包形式再分发）。

## Development and build

| Package | License | Notes |
|---|---|---|
| pytest | MIT | Tests; not shipped in UI zip as a product feature |
| ruff | MIT | Linter; not a runtime dependency |
| PyInstaller | GPL + frozen application exception | `requirements-build.txt` only; not a runtime import |

## ASR（可选 ASR zip）

`huiyiku-asr.exe` 再分发 FunASR、ModelScope、PyTorch、NumPy 等，不在 UI zip 内。
其归属声明见 `THIRD_PARTY_NOTICES_ASR.md`，由 `check_asr_zip` 强制要求随包分发。

## FFmpeg

FFmpeg is **not** distributed with this repository or the UI zip. Users supply
their own CLI build. Calling FFmpeg as a separate process is described in
`docs/开发方案.md` section 2.7.5.

## Models

On-device FunASR weights are not in git. Register each model ID, revision,
checksum, and license in `docs/model-manifest.json` before a Release. Remote
LLM services are BYOK; their terms are not open-source licenses.

模型许可目前登记为 `Apache-2.0 (verify on model card)`，发布前须逐条到
ModelScope 模型卡核实，并把 `docs/model-manifest.json` 中该字段改为已核实状态。
