import os
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
TRANSCRIBER_LIB_DIR = ROOT_DIR / "runtime" / "transcriber"
WHISPER_MODEL_DIR = ROOT_DIR / "runtime" / "whisper_models"


def seconds_label(value):
    seconds = max(0, int(round(float(value))))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class TranscriptionEngine:
    def __init__(self, model_name="small"):
        self.model_name = model_name
        self.model = None

    def _load_model(self):
        if self.model is not None:
            return self.model
        lib_path = str(TRANSCRIBER_LIB_DIR)
        if lib_path not in sys.path:
            sys.path.insert(0, lib_path)
        from faster_whisper import WhisperModel

        WHISPER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self.model = WhisperModel(
            self.model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=min(8, os.cpu_count() or 4),
            num_workers=1,
            download_root=str(WHISPER_MODEL_DIR),
        )
        return self.model

    def transcribe_file(self, media_path):
        media_path = Path(media_path)
        audio_path = media_path.with_suffix(".wav")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error", "-i", str(media_path),
                    "-vn", "-ac", "1", "-ar", "16000", str(audio_path),
                ],
                check=True,
                capture_output=True,
            )
            model = self._load_model()
            segments, info = model.transcribe(
                str(audio_path),
                language="zh",
                beam_size=5,
                vad_filter=False,
                condition_on_previous_text=True,
                temperature=0,
                initial_prompt=(
                    "以下是中文财经、股票与投资口播。可能包含科技股、龙头、板块、估值、"
                    "市盈率、业绩、财报、成交量、仓位、买点、卖点等术语。请完整逐句转录。"
                ),
            )
            rows = []
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue
                rows.append(
                    {
                        "start": round(segment.start, 2),
                        "end": round(segment.end, 2),
                        "start_display": seconds_label(segment.start),
                        "end_display": seconds_label(segment.end),
                        "text": text,
                    }
                )
            return {
                "model": self.model_name,
                "audio_preprocessing": "ffmpeg pcm_s16le 16kHz mono",
                "vad_filter": False,
                "language": info.language,
                "language_probability": round(info.language_probability, 4),
                "text": "".join(row["text"] for row in rows),
                "segments": rows,
            }
        finally:
            audio_path.unlink(missing_ok=True)
            media_path.unlink(missing_ok=True)
