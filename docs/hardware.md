# 硬件实测

日期：2026-08-21。机器约 16GB 内存、64 位 Windows、CPU torch 2.13.0，无 GPU。

| 项 | 结果 |
|---|---|
| FunASR | 1.4.2，`paraformer-zh` + `fsmn-vad` + `ct-punc` + `cam++` |
| 权重位置 | `asr_home/models`（MODELSCOPE_CACHE） |
| 官方短示例 | `SPEAKER_00`，1070–4365 ms，rtf ≈ 0.07 |
| 进程峰值工作集 | **5.17 GB**（含 CAM++） |
| 方案硬门槛 | 16GB 机器转写 1 小时会 ≤ 8GB |
| 1 小时真会 | 未测（真实录音不入库）。短音频峰值已是模型常驻，长音频走 VAD + `batch_size_s=60`，不整段灌入 |

5.17 GB < 8 GB，当前默认不关闭 CAM++。若 1 小时真会超标，按方案顺序：关 CAM++ → 更小 Paraformer / 缩短 VAD 切段 → 把新数字写回本表。

复测：

```powershell
.\.venv-asr\Scripts\python scripts\measure_asr_memory.py --wav <wav> --model-dir asr\models
```
