"""Optional local speech-to-text for session recordings (`uv sync --extra audio`).

Uses faster-whisper. There is no speaker diarization yet, so the transcript
has timestamps but no speaker labels; the ingest prompt is written to cope.
"""

from __future__ import annotations

from pathlib import Path


def transcribe(path: Path, model_size: str = "small", language: str | None = "en") -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError as err:
        raise SystemExit(
            "Audio transcription needs the 'audio' extra: uv sync --extra audio"
        ) from err

    model = WhisperModel(model_size, compute_type="int8")
    segments, _info = model.transcribe(str(path), language=language, vad_filter=True)
    lines = []
    for seg in segments:
        m, s = divmod(int(seg.start), 60)
        h, m = divmod(m, 60)
        lines.append(f"[{h:d}:{m:02d}:{s:02d}] {seg.text.strip()}")
    return "\n".join(lines)
