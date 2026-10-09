# licenses-asr/_upstream —— ASR 组件上游未随包提供许可正文时的补充文本

ASR 模式的正文默认从**当前解释器（`.venv-asr`）已安装发行版的 `dist-info`**
提取（`scripts/collect_licenses.py --mode asr`）。下列组件 wheel/sdist 均不带
许可文件，只能在此补正文；`--check` 与打包门禁据此判定不再缺正文。

只放上游原文或标准许可正文，不转抄、不改写。公共组件（如 jieba）复用
`licenses/_upstream/`，不重复存放。

| 包 | 版本 | 许可 | 正文来源 | sha256（前 16 位） |
|---|---|---|---|---|
| antlr4-python3-runtime | 4.9.3 | BSD-3-Clause | https://raw.githubusercontent.com/antlr/antlr4/master/LICENSE.txt（上游仓库原文） | `3db1fb3ee79a4b4f` |
| sentencepiece | 0.2.x | Apache-2.0 | https://raw.githubusercontent.com/google/sentencepiece/master/LICENSE（上游仓库原文） | `cfc7749b96f63bd3` |
| tokenizers | 0.2x | Apache-2.0 | https://raw.githubusercontent.com/huggingface/tokenizers/main/LICENSE（上游仓库原文） | `c71d239df91726fc` |
| tqdm | 4.6x | MPL-2.0 AND MIT | https://raw.githubusercontent.com/tqdm/tqdm/master/LICENCE（上游仓库原文，双许可并列） | `fcff87c3a47ce802` |
| jamo（正文来源） | 0.4.1 | Apache-2.0 | 上游 sdist `jamo-0.4.1.tar.gz` 无许可文件；许可标识来自 PyPI 分类器 `License :: OSI Approved :: Apache Software License`，正文为 canonical Apache-2.0 | `55ea4eb3267cd92e` |
| torch-complex | 0.4.4 | Apache-2.0 | 上游 sdist `torch_complex-0.4.4.tar.gz` 无许可文件；许可标识来自 PyPI 分类器，正文为 canonical Apache-2.0 | `55ea4eb3267cd92e` |
| soxr | 1.1.0 | LGPL-2.1-or-later | https://raw.githubusercontent.com/dofuuz/python-soxr/main/LICENSE.txt（上游声明） | `aeeb7bce59a3dcc7` |
| soxr（LGPL 全文） | 2.1 | LGPL-2.1-or-later | https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt（GNU 官方全文；LGPL 要求随分发附完整许可文本） | `20e50fe7aae3e563` |

完整 sha256 见同目录 `*.txt.sha256`（`sha256sum licenses-asr/_upstream/*.txt` 可复核）。

说明：`jamo` 与 `torch-complex` 的正文取自本仓库 `LICENSE`（canonical
Apache License 2.0 全文）——Apache-2.0 要求再分发时附许可副本，而非附版权行，
故此处以标准全文为准；二者的版权归属在上游仓库，未在本文件内改写。
