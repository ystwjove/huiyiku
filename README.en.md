[中文](README.md) · English

# huiyiku

Local meeting knowledge base: import recordings, transcribe on-device with FunASR, review speakers, then generate cited reports and Q&A through **your own** cloud LLM key (BYOK). Audio stays on disk by default; this project never proxies your billing.

huiyiku stores one library on this computer. A small team on a trusted LAN can share that same library (see [LAN sharing](#lan-sharing-optional)). Double-click the exe to open a desktop window. Transcription stays on the machine, and audio is not uploaded by default. Reports, Q&A, and digests send **transcript text** to the LLM API you configure. The author never receives your key or your recordings.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

**License: [Apache License 2.0](LICENSE)** (use, modify, and redistribute, including the patent grant). What the license allows, what you must keep, and the legal boundaries for recordings, outbound text, and FFmpeg are in **[docs/法律与许可.md](docs/法律与许可.md)** (Chinese). The three points below are a summary, **not legal advice**.

---

## Read these three things first

1. **Transcription stays on this machine.** The default engine is FunASR Paraformer. Audio is not sent to the cloud. This repository and the UI zip do **not** bundle PyTorch, model weights, FFmpeg, or an API key.
2. **Reports and Q&A send text out.** Transcript text is sent to a provider only after you set an LLM key and accept that provider's terms. Without a key you can still import, transcribe, review, see statistics, and export.
3. **Record only with consent from the people in the meeting.** In several US all-party-consent states, recording without everyone's consent can be a crime. Workplace recordings of colleagues or customers in the EU usually fall outside the GDPR household exemption. In China, providing personal information to a third party generally requires notice and consent. Voiceprints are sensitive biometric data, and this version does not do voiceprints. See [docs/法律与许可.md](docs/法律与许可.md). The software is provided **AS IS** under Apache-2.0. It does not manage consent. A checkbox is not a substitute for following the law.

---

## Windows (end users)

1. Download `huiyiku-windows-x64.zip` from GitHub Releases, extract it, and double-click `Huiyiku.exe`. Prefer `%LOCALAPPDATA%\Programs\Huiyiku`. Do not leave the extracted folder in Downloads and then delete it as if it were only an installer.
2. A **desktop window** opens, with its own title bar and taskbar icon. It is not a browser tab. The window uses the system WebView2 runtime (Edge). On the rare machine without WebView2, the app falls back to the system browser at `http://127.0.0.1:8787`. **Closing the window quits the app**, including the background service. The port is fixed at 8787. If **another program** already uses it, the app exits with an error. Do not change the port to dodge that.
3. **Bring your own FFmpeg.** Put it on `PATH`, or place `ffmpeg\ffmpeg.exe` next to the exe. This project does not ship FFmpeg binaries. Official builds: [FFmpeg Windows builds](https://www.ffmpeg.org/download.html).
4. **Install the ASR add-on only if you want transcription.** Download `huiyiku-asr-windows-x64.zip` (several GB, includes torch) and extract it to `asr\` next to the program (`asr\` at the repo root when developing). You should see `huiyiku-asr.exe`. From source, you can instead create `.venv-asr`, install `requirements-asr.txt`, and put weights in `asr\models\`.
5. Check `SHA256SUMS` on the Release. The exe is **not code-signed**. SmartScreen or antivirus software may warn. Use the zip built by this repository's Actions. Do not download it from a third-party file host.

Recordings, the database, transcripts, and the ASR add-on live next to the program by default: `meeting-data\` and `asr\` (the repo root when developing, or beside `Huiyiku.exe` after you extract the zip). **Do not treat a zip left in Downloads as the install directory.** Deleting that folder deletes the library and the models.

The UI alone is comfortable on 8 GB of RAM. On-device transcription wants **16 GB of RAM and an SSD**. 8 GB can be tried if you close other programs. A GPU is not required.

### Build the Windows zip yourself

```powershell
python -m pip install -r requirements-build.txt
python scripts/package_windows.py --ui
# The ASR zip must be built with an interpreter that already has torch and funasr.
# Everyday CI does not build it:
# python scripts/package_windows.py --asr
```

Output is in `packaging/dist/`: `huiyiku-windows-x64.zip` and `SHA256SUMS`. The UI zip does **not** contain torch, weights, an FFmpeg binary, or `portable.txt`. Pushing the `v0.1.0` tag makes GitHub Actions build the UI zip and attach it to the Release.

### Hardware note (2026-08-21)

| Item | Result |
|---|---|
| Machine | 64-bit Windows, about 16 GB RAM, CPU (no GPU) |
| Pipeline | FunASR 1.4.2 `paraformer-zh` + `fsmn-vad` + `ct-punc` + `cam++`, weights loaded from `asr_home/models` |
| Official short sample | Emits `SPEAKER_00` and millisecond timestamps. Speaker labels come from FunASR `sentence_info.spk` and are on by default |
| 1-hour real meeting CER / peak RAM | Not measured on a real meeting recording (those files are not committed). Peak working set on the official short sample was **5.17 GB** with CAM++, under the 8 GB hard line. Long audio uses VAD and 60-second batches. See [`docs/hardware.md`](docs/hardware.md) |

Model IDs, revisions, and SHA256: [`docs/model-manifest.json`](docs/model-manifest.json).

---

## Run from source

```powershell
git clone https://github.com/ystwjove/huiyiku.git
cd huiyiku
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pytest
python -m huiyiku --help
```

Do **not** install torch in the default venv. For on-device transcription, create a separate `.venv-asr` (see `requirements-asr.txt`).

One command starts the API, the worker, and the desktop window. Build the frontend first. If WebView2 is missing, the app falls back to the system browser.

```powershell
cd web; npm install; npm run build; cd ..
python -m huiyiku
```

API or worker only: `python -m huiyiku --serve` and `python -m huiyiku --worker`.

Offline pipeline, without the UI:

```powershell
python -m huiyiku pipeline --input path\to\meeting.wav --out-dir pipeline-out
```

You need Python 3.12+, Git, and FFmpeg. Building the frontend also needs Node. The source tree can be developed on Windows, macOS, or Linux. **The supported end-user build is the Windows exe.**

---

## Configure an LLM (BYOK)

Keep keys in `secrets.json` inside the data directory, or in environment variables. Do **not** put them in `app.json`, and do not commit them.

Environment variables (placeholders only; never paste a real key):

```text
HUIYIKU_LLM_API_KEY=sk-...
HUIYIKU_ACCESS_TOKEN=
```

Shape of `secrets.json`:

```json
{
  "asr_api_key": "",
  "embedding_api_key": "",
  "llm_api_key": "sk-..."
}
```

The setup wizard accepts any OpenAI-compatible endpoint (DeepSeek, compatible Qwen endpoints, SiliconFlow, OpenAI, and others). **Only the Chat Completions shape is supported** (`/v1/chat/completions`). Anthropic Messages (`/v1/messages`) and OpenAI Responses (`/v1/responses`) are not supported and will fail to connect. A base URL may be the host or the host plus `/v1`. A full path is completed automatically. Before you enable a remote service, accept that provider's terms, retention, and cross-border transfer rules. This project does not bill you and does not sit in the middle of the bill.

`app.example.json` (never commit a real `app.json`):

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

## Transcript quality and hotwords

FunASR Paraformer is weak on proper names: street names, people, product names, and jargon. Hotwords in `settings.json` help:

```json
{
  "hotwords": "MainStreet huiyiku standup weekly"
}
```

Separate words with spaces. The change applies to **new** transcripts. An existing meeting can be run again from its page. You can also double-click a segment and correct the text.

---

## LAN sharing (optional)

**Off by default.** Only this computer can open the library. For a small internal group, turn on **LAN sharing** in Settings. Turn it off with the same switch. You do not edit `app.json`, and you do not restart. No access token is required. This is light sharing, not a multi-user product.

1. On this computer, open huiyiku, go to Settings, and turn on LAN sharing.
2. The first time the program listens beyond localhost, Windows Firewall may ask for access. Allow it on private networks only.
3. Send colleagues the address shown on the Settings page, for example `http://192.168.1.20:8787`. Turning the switch off makes those addresses stop working immediately.

Limits:

- **Plain HTTP the whole way.** Transcripts and audio are not encrypted on the LAN. Anyone on the same network who can capture packets can read them. Use this only on a network you trust.
- **No per-person isolation.** There are no accounts, permissions, or an edit audit. Two people correcting the same meeting can overwrite each other.
- **One transcription queue.** The worker is a single process. A long job makes everyone else wait.
- **The host must stay on.** Closing the window or shutting down disconnects everyone. There is still only one copy of the data, on the host. Back it up as described below.

---

## Data directory and backups

| Mode | Config | Default data directory | ASR |
|---|---|---|---|
| exe | `app.json` next to the exe | `meeting-data` next to the exe | `asr` next to the exe |
| development | `app.json` in the repo, or `HUIYIKU_APP_JSON` | `meeting-data` in the repo | `asr` in the repo, or `.venv-asr` inside the repo |

To back up, quit the app so the WAL can checkpoint, then copy `app.json` and the whole `meeting-data\` folder (including `secrets.json`). **You do not need to back up `asr\` by default.** It is large and can be downloaded again. A backup is as sensitive as the cloud account it contains. Keep it yourself. Do not put the library on OneDrive or a network drive.

After a restore you can read existing transcripts. Transcribing a new meeting still requires the ASR zip installed at `asr_home`.

---

## Cost

There is no required subscription. The author does not charge you. In the recommended setup, the cloud bill is usually only **your own LLM tokens**.

| Item | Do you pay? |
|---|---|
| On-device FunASR | No (one-time download of the add-on and weights) |
| Reports / Q&A / digests | Billed by your LLM provider |
| Remote ASR / embeddings | Off by default. They cost money and send data out only if you turn them on |
| GitHub source and Release zips | No |

A one-hour Chinese meeting is roughly 20–40 thousand tokens of transcript. Summaries and Q&A add more. The price depends on the model and the provider. On common pay-as-you-go plans that is often a small amount of RMB. Your invoice is the authority. With no LLM key there is no cloud bill, and also no automatic report or Q&A.

---

## Recording law (short)

The full write-up and the jurisdiction table are section 4 of [docs/法律与许可.md](docs/法律与许可.md). This tool transcribes recordings **you already have a right to process**. It is not a wiretap.

| Place | Point |
|---|---|
| US all-party states (CA, FL, IL, MD, MA, PA, WA, and others) | Recording without every participant's consent can be a crime in some states. When people are in different states, the stricter rule can apply |
| EU GDPR | The household exemption is only for purely personal activity. Recording colleagues or customers at work usually requires notice, and the recorder may be a controller |
| China PIPL | The personal-affairs exemption in Article 72 is narrow. Sending text to a cloud provider should be disclosed and consented to. Voiceprints are sensitive biometric data (Articles 28–29). **This version does not do voiceprints** |

On first launch you confirm that you will follow local law and get consent, and whether transcript text may be sent to the API you configured. The project does **not** keep a consent register. A checkbox does not replace the law. The system does not take ownership of your content.

---

## Repository hygiene

Do **not** commit real meeting recordings, transcripts, evaluation samples, or screenshots that show real names. Tests use synthetic audio, made-up transcripts, and redacted fixtures.

Directory map: [`docs/仓库结构.md`](docs/仓库结构.md). The design notes for developers, not this README, are in [`docs/开发方案.md`](docs/开发方案.md). Doc index: [`docs/README.md`](docs/README.md).

---

## License and third parties

- Source and the UI zip: **[Apache-2.0](LICENSE)**. You may use, modify, and redistribute them if you keep [LICENSE](LICENSE) and [NOTICE](NOTICE). Notes: [docs/法律与许可.md](docs/法律与许可.md)
- Third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- Inbound license exceptions: [`docs/license-exceptions.json`](docs/license-exceptions.json)
- On-device weights: [`docs/model-manifest.json`](docs/model-manifest.json)

Default Python and Node dependencies are MIT, MIT-0, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, or 0BSD. The UI lockfile does not contain `torch`, `funasr`, or `numpy`. **You supply FFmpeg.** This repository does not embed its binary, so an LGPL or GPL FFmpeg build is not pulled into the default zip. A cloud LLM is a contract between you and that provider. It is not an open-source license of this project.
