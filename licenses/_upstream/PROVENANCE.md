# licenses/_upstream —— 上游未随包提供许可正文时的补充文本

`licenses/` 下的正文默认从**本机已安装发行版的 `dist-info`** 提取（脚本
`scripts/collect_licenses.py`）。少数发行版的 wheel/sdist 不带许可文件，只有
元数据里的许可标识；这类组件在此目录下补一份正文，收集脚本会自动接管，
`--check` 也据此判定不再缺正文。

规则：**只允许放上游原文**，不转抄、不改写；来源与校验信息记录在下表。
新增条目时必须同时登记下表。

| 包 | 版本 | 许可 | 正文来源 | 取得方式 | sha256（前 16 位） |
|---|---|---|---|---|---|
| jieba | 0.42.1 | MIT | https://raw.githubusercontent.com/fxsjy/jieba/master/LICENSE | 上游仓库原文 | `18ba0984839f8585`（完整值见 `licenses/_upstream/jieba.txt.sha256`） |
| proxy-tools | 0.1.0 | MIT | 上游 PyPI 元数据声明 MIT（setup.py `license='MIT'`、License 分类器）；wheel 与 sdist 均不含许可文件 | MIT 标准正文 + 元数据中的版权行（Author: Jonathan Tushman） | `59deae4a17f7eef7`（完整值见 `licenses/_upstream/proxy-tools.txt.sha256`） |

脚注：两个文件的 sha256 由构建机记录在 `packaging/dist/SHA256SUMS` 之外，
可用 `sha256sum licenses/_upstream/*.txt` 复核；如上游更新正文，应重新取得并更新本表。

说明：`proxy-tools` 的版权行取自其包元数据 `Author` 字段（wheel METADATA 与
PyPI JSON 一致），上游未提供独立的版权声明文件。
