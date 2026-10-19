"""Explicit download command used by setup-whisper.bat, never by recording."""
import argparse
import os
from pathlib import Path
import tempfile

from .preferences import Preferences
from .transcription import MODELS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=sorted(MODELS), default="medium")
    args = parser.parse_args()
    from faster_whisper import WhisperModel, download_model
    directory = Path(os.environ.get("AYAN_DATA_DIR", "data"))
    models = directory / "models"
    models.mkdir(parents=True, exist_ok=True)
    target = models / ("whisper-" + args.model)
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix="whisper-install-", dir=models) as temporary:
            staging = Path(temporary) / "model"
            # Official faster-whisper model mapping and HTTPS/Hugging Face download.
            download_model(args.model, output_dir=str(staging))
            model = WhisperModel(str(staging), device="cpu", compute_type="int8", local_files_only=True)
            del model  # Release file handles before rename on Windows.
            staging.rename(target)
    else:
        model = WhisperModel(str(target), device="cpu", compute_type="int8", local_files_only=True)
        del model
    Preferences(directory).update({"stt_provider": "auto", "stt_model": args.model})
    print(f"Whisper {args.model} is installed and validated. Restart Ayan.")


if __name__ == "__main__":
    main()
