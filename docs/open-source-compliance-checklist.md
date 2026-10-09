# 开源合规清单

随依赖变更更新。发布节点必须人工核对 `LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES.md` 与本文件。

## 白名单（方案 2.7）

允许直接依赖：MIT、MIT-0、Apache-2.0、BSD-2-Clause、BSD-3-Clause、ISC、0BSD。

禁止进入默认依赖：GPL、AGPL、SSPL、LGPL、MPL、未知许可、BSD-4-Clause。

已知例外见 [`license-exceptions.json`](license-exceptions.json)。SQLite 官方 blessing 按方案记录。`certifi`（httpx 传递依赖，MPL-2.0）在建仓时登记为入站例外，切片 0 许可扫描会读取该表。

## 日常 CI

- [x] `requirements.lock` / `requirements-dev.lock` 不含 `torch`、`funasr`、`modelscope`、`numpy`
- [x] `requirements-asr.txt` 单独存在；日常 CI 不安装
- [x] `requirements-build.txt` 中的 PyInstaller 不进入运行时锁文件
- [x] 媒体扩展名、`app.json`、`secrets.json`、疑似 Key 入库即失败
- [x] 单文件超过 1MB（锁文件除外）失败
- [x] `scripts/check_notices.py`：`requirements.lock` 与前端生产依赖每一项都必须登记在 `THIRD_PARTY_NOTICES.md`
- [x] `scripts/collect_licenses.py --check`：`licenses/` 许可正文齐全（无缺失条目）
- [ ] 远程模型启用前的条款确认为产品行为，不在 CI 代替用户签署

## 发布节点（切片 2 后）

- [x] CycloneDX SBOM 覆盖冻进 `_internal` 的包：`scripts/make_sbom.py` 在打包时产出 `packaging/dist/sbom-huiyiku-<version>.cdx.json`（Python 包 + 前端生产依赖 + CPython 原生库）
- [x] 打包脚本把 `LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES.md` 打进 UI zip（发布时再人工打开 zip 核对）
- [x] 许可正文目录 `licenses/` 随 UI zip 分发（`Huiyiku/licenses/`），门禁强制必须存在
- [x] ASR zip 必须带 `THIRD_PARTY_NOTICES_ASR.md`（`check_asr_zip` 强制）；ASR 侧 SBOM 用 `make_sbom.py --scope asr --lock requirements-asr.txt` 在发布前生成
- [x] 模型许可核实：4 个本地模型的 ModelScope 模型卡已逐一核实为 Apache-2.0（2026-09-28，证据见 `model-manifest.json` 各条 `license_verified`）
- [x] ASR zip：`licenses/` 许可正文随包（`collect_licenses.py --mode asr`，当前 86 个包全覆盖）+ `THIRD_PARTY_NOTICES_ASR.md`，两者均由 `check_asr_zip` 强制
- [x] ASR SBOM：`make_sbom.py --scope asr --installed`（用 `.venv-asr` 运行）产出 `packaging/dist/sbom-huiyiku-asr-<version>.cdx.json`
- [ ] ASR 侧 copyleft 复核：`soxr` 为 LGPL-2.1-or-later（经 librosa 进入 FunASR 运行时路径），已按「独立可替换模块 + 附 LGPL 全文 + 源码书面承诺」处置并登记在 `license-exceptions.json`；若日后要求零 LGPL，需先验证排除 soxr 后 FunASR 推理不受影响
- [x] 门禁拒绝 UI zip 含 torch / 权重 / FFmpeg 二进制 / `portable.txt` / Key / 媒体
- [x] `docs/model-manifest.json` 登记模型 ID / revision / 许可 URL；checksum 可在本机补测后填写
- [ ] GitHub 网页打开 Secret scanning 与 Push protection（workflow 不能代替）
- [x] 公开前：`git log` 与 `git rev-list --objects --all` 无 `.env`、无真实 `sk-`、无媒体（2026-09-28 全历史扫描通过）
