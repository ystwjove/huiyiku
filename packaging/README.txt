会议库 Huiyiku
==============

双击 Huiyiku.exe，打开独立的桌面窗口（不是浏览器标签页）。
关闭窗口即退出程序。
窗口使用系统自带的 WebView2（Edge 内核，Windows 10/11 一般自带）。
若机器缺少 WebView2，会自动回退为系统浏览器访问
http://127.0.0.1:8787。

使用前
------
1. 自备 FFmpeg：放入 PATH，或放到本目录 ffmpeg\ffmpeg.exe。
2. 要转写：下载 huiyiku-asr-windows-x64.zip，解压到本目录 asr\
   其中应有 asr\huiyiku-asr.exe。体积约数 GB（含 torch）。
3. 报告 / 问答：在向导里填写你自己的 LLM Key（仅支持
   OpenAI-compatible Chat Completions 格式端点，如 DeepSeek、
   通义、硅基流动、OpenAI）。不填也可以导入、转写、校对。

数据
----
录音、数据库和转写保存在本目录 meeting-data\
ASR 组件解压到本目录 asr\
配置在本目录 app.json
请把本文件夹放到固定位置，不要留在「下载」。

安全
----
exe 未代码签名。SmartScreen 可能拦截。请核对本仓库 Release 的 SHA256。
不要从第三方网盘下载。

转写在本机完成，音频默认不上传。报告和问答会把转写文本发到你配置的大模型。
录音与外发的合法性由你负责。

许可证：Apache-2.0，见 LICENSE 与 NOTICE。第三方见 THIRD_PARTY_NOTICES.md。
本软件按现状提供，不构成法律建议。录音是否合法、转写文本能否发给你配置的
大模型，由你按当地法律和云厂商条款处理。请勿把 ffmpeg.exe 或 API Key 放进
再分发的 zip。
