import argparse
import subprocess
from pathlib import Path


def prepare_reference(src, dst):
    src = Path(src)
    dst = Path(dst)

    dst.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-af",
            "highpass=f=60,lowpass=f=12000,loudnorm=I=-20:TP=-2:LRA=7",
            str(dst),
        ],
        check=True,
    )

    print(f"Reference created: {dst}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("input")
    parser.add_argument("output")

    args = parser.parse_args()

    prepare_reference(args.input, args.output)