"""Explicit download command used by setup-whisper.bat, never by recording."""
import argparse
import os
from pathlib import Path
import tempfile

from .preferences import Preferences
from .transcription import MODELS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=sorted(MODELS), default="small")
    args = parser.parse_args()
    from faster_whisper import download_model
    directory = Path(os.environ.get("AYAN_DATA_DIR", "data"))
    models = directory / "models"
    models.mkdir(parents=True, exist_ok=True)
    target = models / ("whisper-" + args.model)
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix="whisper-install-", dir=models) as temporary:
            staging = Path(temporary) / "model"
            # Official faster-whisper model mapping and HTTPS/Hugging Face download.
            download_model(args.model, output_dir=str(staging))
            required = ("model.bin", "config.json", "tokenizer.json")
            if not all((staging / name).is_file() for name in required):
                raise RuntimeError("Whisper model download is incomplete; required files are missing.")
            staging.rename(target)
    else:
        required = ("model.bin", "config.json", "tokenizer.json")
        if not all((target / name).is_file() for name in required):
            raise RuntimeError("Installed Whisper model is incomplete; remove its model folder and retry.")
    Preferences(directory).update({"stt_provider": "auto", "stt_model": args.model})
    print(f"Whisper {args.model} files are installed. Model memory will be used only when speech recognition starts. Restart Ayan.")


if __name__ == "__main__":
    main()
