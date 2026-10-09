# Third-party notices —— ASR 组件（可选）

本文件对应 `huiyiku-asr.exe`（可选 ASR 组件 zip），**不在** UI zip 内。
UI zip 的归属声明见 `THIRD_PARTY_NOTICES.md`。`scripts/release_zip.py` 的
`check_asr_zip` 强制要求本文件与 `licenses/` 许可正文随 ASR zip 分发。

- **完整许可正文**：zip 内 `licenses/python-packages.txt`（当前环境全部
  发行版的正文，含 torch/torchaudio/FunASR/ModelScope 及传递依赖）与
  `licenses/cpython-and-native-libs.txt`（CPython 与随其分发的本地库）。
  由 `.venv-asr\Scripts\python.exe scripts/collect_licenses.py --mode asr`
  生成；上游 wheel 缺正文的组件补在 `licenses-asr/_upstream/`（含出处表）。
- **机器可读清单**：`packaging/dist/sbom-huiyiku-asr-<version>.cdx.json`
  （CycloneDX 1.5，`scripts/make_sbom.py --scope asr --installed`）。
- **白名单与例外**：`docs/license-exceptions.json`（含 ASR 侧 soxr/tqdm/certifi）。

## 直接依赖

| Package | Version | License | Notes |
|---|---|---|---|
| FunASR | 1.4.2 | MIT | 语音识别推理管线（`requirements-asr.txt`） |
| ModelScope | 1.39.1 | Apache-2.0 | 模型下载与加载（`requirements-asr.txt`） |
| PyTorch | 2.13.0+cpu | BSD-3-Clause | 由 PyTorch CPU 索引安装；其 LICENSE 同时覆盖内嵌第三方组件 |
| torchaudio | 2.11.0+cpu | BSD-2-Clause | FunASR `AutoModel` 需要 |
| NumPy | 2.5.2 | BSD-3-Clause | 数值基础库 |

## 传递依赖

FunASR / ModelScope / PyTorch 的传递依赖（librosa、scipy、numba、transformers、
soundfile、kaldiio、sentencepiece、tokenizers、tiktoken、omegaconf、oss2、
safetensors、pyyaml、tqdm 等）逐项登记在 `python-packages.txt` 的正文与
`packaging/dist/sbom-huiyiku-asr-<version>.cdx.json` 的组件表内。

## Copyleft 组件的处置

| Package | License | 处置 |
|---|---|---|
| **soxr**（经 librosa 进入 FunASR 运行时路径） | **LGPL-2.1-or-later** | 以**独立可替换模块**分发（`_internal/soxr/soxr_ext.pyd`），用户可用自行构建的同名模块替换以实现重新链接；包内附上游声明与 **LGPL-2.1 全文**（`licenses/python-packages.txt` 中 soxr 条目）。**源码书面承诺**：如需 python-soxr / libsoxr 的对应源码，请在本仓库提 Issue 索取，作者将提供获取途径（上游：github.com/dofuuz/python-soxr、github.com/chirlu/soxr）。 |
| tqdm | MPL-2.0 AND MIT | 双许可，按 MIT 分支履行；MPL 声明随正文一并保留 |
| certifi | MPL-2.0 | 不修改其源文件；随包保留 MPL 声明 |

除上述三项外，ASR 组件的许可均在宽松范围（MIT / BSD / Apache-2.0 / ISC /
0BSD / Unlicense）。如后续版本引入新的 copyleft 组件，必须先在
`docs/license-exceptions.json` 登记并更新本表。

## 模型权重

FunASR 权重不随本仓库或 ASR zip 分发；用户在 `asr_home/models` 下自行下载。
逐模型的 ID / revision / checksum / 许可登记在 `docs/model-manifest.json`；
4 个本地模型的许可已于 2026-09-28 通过 ModelScope 模型接口逐一核实为
Apache-2.0（见各条 `license_verified`）。

## FFmpeg

FFmpeg 不随 ASR 组件分发；用户在 UI 侧自行提供 CLI 构建。
