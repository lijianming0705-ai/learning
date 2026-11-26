from __future__ import annotations

import json
import os
import sys
import wave
from pathlib import Path

# Redirect library stdout noise to stderr; reserve real stdout strictly for JSON IPC lines.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

import numpy as np  # noqa: E402
from mlx_audio.tts.utils import load_model  # noqa: E402


def _write_wav(audio_np: np.ndarray, sample_rate: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".tmp.wav")
    clipped = np.clip(audio_np.astype(np.float32), -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    try:
        with wave.open(str(tmp_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16.tobytes())
        os.replace(tmp_path, out_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _emit(payload: dict[str, object]) -> None:
    _REAL_STDOUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _REAL_STDOUT.flush()


def main() -> None:
    loaded_model_id: str | None = None
    model = None

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            text = str(req.get("text", "")).strip()
            model_id = str(req.get("model") or "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit")
            voice = str(req.get("voice") or "Vivian")
            lang_code = str(req.get("lang_code") or "chinese")
            out_path = Path(str(req["out_path"]))

            if not text:
                raise ValueError("Empty text")

            if model is None or loaded_model_id != model_id:
                model = load_model(model_id)
                loaded_model_id = model_id

            chunks: list[np.ndarray] = []
            sample_rate = 24000
            for res in model.generate(
                text=text,
                voice=voice,
                lang_code=lang_code,
                verbose=False,
            ):
                if getattr(res, "sample_rate", None):
                    sample_rate = int(res.sample_rate)
                arr = np.asarray(res.audio, dtype=np.float32).reshape(-1)
                if arr.size > 0:
                    chunks.append(arr)

            if not chunks:
                raise RuntimeError("No audio samples generated")

            full_audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
            _write_wav(full_audio, sample_rate, out_path)
            _emit({"ok": True, "path": str(out_path), "sample_rate": sample_rate})
        except Exception as exc:  # noqa: BLE001
            _emit({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    main()
