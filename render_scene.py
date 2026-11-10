import argparse
import json
import os
import re
import subprocess
import tempfile
import torch
from pathlib import Path

os.environ.setdefault("NUMBA_CACHE_DIR", str(Path(tempfile.gettempdir()) / "full_cast_numba_cache"))
os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "full_cast_mpl_cache"))
Path(os.environ["NUMBA_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

from TTS.api import TTS


MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"

# Keep comfortably below XTTS's 400-token limit.
MAX_CHARS = 180


ROOT = Path(__file__).resolve().parent
from voice_paths import voice_clip

VOICE_FILES = {
    name: voice_clip(name) or ROOT / "voices" / f"{name}.mp3"
    for name in ("Savannah Peachwood", "Lee Daniels", "Jacob Morgan", "Teddy Hamilton")
}


def split_long_text(text, max_chars=MAX_CHARS):
    """
    Split long text into safe XTTS chunks.

    Nothing is deleted or summarized.
    The complete original text is preserved.
    """

    text = re.sub(r"\s+", " ", text).strip()

    if len(text) <= max_chars:
        return [text]

    # Prefer sentence boundaries.
    sentences = re.split(
        r"(?<=[.!?…])\s+",
        text
    )

    chunks = []
    current = ""

    for sentence in sentences:

        sentence = sentence.strip()

        if not sentence:
            continue

        if (
            current
            and len(current) + 1 + len(sentence)
            <= max_chars
        ):
            current += " " + sentence
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(sentence) <= max_chars:
            current = sentence
            continue

        # Sentence is still too long.
        # Try punctuation boundaries.
        pieces = re.split(
            r"(?<=[,;:—–-])\s+",
            sentence
        )

        for piece in pieces:

            piece = piece.strip()

            if not piece:
                continue

            if (
                current
                and len(current) + 1 + len(piece)
                <= max_chars
            ):
                current += " " + piece

            elif len(piece) <= max_chars:

                if current:
                    chunks.append(current)

                current = piece

            else:

                # Final fallback:
                # split on words.
                words = piece.split()

                for word in words:

                    if not current:
                        current = word

                    elif (
                        len(current) + 1 + len(word)
                        <= max_chars
                    ):
                        current += " " + word

                    else:
                        chunks.append(current)
                        current = word

    if current:
        chunks.append(current)

    return chunks


def prepare_reference(src, dst):

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
            "highpass=f=60,"
            "lowpass=f=12000,"
            "loudnorm=I=-20:TP=-2:LRA=7",

            str(dst),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def render_scene(plan_file, output_file):

    plan_file = Path(plan_file)
    output_file = Path(output_file)

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    plan = json.loads(
        plan_file.read_text(
            encoding="utf-8"
        )
    )

    print()
    print("================================")
    print("LOADING XTTS-V2")
    print("================================")
    print()

    use_gpu = torch.cuda.is_available()
    device = torch.cuda.get_device_name(0) if use_gpu else "CPU"
    print(f"Synthesis device: {device}")
    tts = TTS(
        MODEL,
        progress_bar=True,
        gpu=use_gpu
    )

    with tempfile.TemporaryDirectory(
        prefix="scene_render_"
    ) as temp_dir:

        temp_dir = Path(temp_dir)

        # --------------------------------------------
        # Prepare voice references
        # --------------------------------------------

        prepared_voices = {}

        print()
        print("Preparing voice references...")
        print()

        for voice_name, voice_path in VOICE_FILES.items():

            if not voice_path.exists():

                raise FileNotFoundError(
                    f"\nVoice file not found:\n"
                    f"{voice_path}\n\n"
                    f"Expected inside:\n"
                    f"voices\\"
                )

            reference_wav = (
                temp_dir
                / f"{voice_name.replace(' ', '_')}_ref.wav"
            )

            print(
                f"Preparing: {voice_name}"
            )

            prepare_reference(
                voice_path,
                reference_wav
            )

            prepared_voices[
                voice_name
            ] = reference_wav

        print()
        print("Voice references ready.")
        print()

        # --------------------------------------------
        # Create synthesis queue
        # --------------------------------------------

        synthesis_queue = []

        for segment_index, segment in enumerate(
            plan["segments"]
        ):

            text = segment.get(
                "text",
                ""
            ).strip()

            if not text:
                continue

            voice_name = segment.get(
                "voice"
            )

            # Older scene plans stored the prepared reference filename rather
            # than the cast name. Derive the bundled cast name so those plans
            # still use the real XTTS path.
            if not voice_name and segment.get("voice_ref"):
                voice_name = Path(segment["voice_ref"]).stem.replace("_ref", "").replace("_", " ")

            if not voice_name:

                raise ValueError(
                    f"Segment {segment_index} "
                    f"has no voice assigned."
                )

            if voice_name not in prepared_voices:

                raise ValueError(
                    f"Unknown voice: "
                    f"{voice_name}"
                )

            chunks = split_long_text(
                text
            )

            for chunk_index, chunk in enumerate(
                chunks
            ):

                synthesis_queue.append(
                    {
                        "speaker":
                            segment.get(
                                "speaker",
                                "Unknown"
                            ),

                        "voice":
                            voice_name,

                        "text":
                            chunk,

                        "language":
                            segment.get(
                                "language",
                                "en"
                            ),

                        "temperature":
                            segment.get(
                                "temperature",
                                0.65
                            ),

                        "length_penalty":
                            segment.get(
                                "length_penalty",
                                1.0
                            ),

                        "repetition_penalty":
                            segment.get(
                                "repetition_penalty",
                                2.0
                            ),

                        "top_k":
                            segment.get(
                                "top_k",
                                50
                            ),

                        "top_p":
                            segment.get(
                                "top_p",
                                0.85
                            ),

                        "speed":
                            segment.get(
                                "speed",
                                1.0
                            ),

                        "chunk_index":
                            chunk_index,

                        "chunk_total":
                            len(chunks),
                    }
                )

        total = len(
            synthesis_queue
        )

        print()
        print("================================")
        print("SYNTHESIS")
        print("================================")
        print()

        print(
            f"Original segments: "
            f"{len(plan['segments'])}"
        )

        print(
            f"Synthesis chunks: "
            f"{total}"
        )

        print()

        clips = []

        # --------------------------------------------
        # Generate audio
        # --------------------------------------------

        for index, segment in enumerate(
            synthesis_queue,
            start=1
        ):

            voice_name = segment[
                "voice"
            ]

            text = segment[
                "text"
            ]

            output_wav = (
                temp_dir
                / f"{index:05d}.wav"
            )

            print(
                f"[{index}/{total}] "
                f"{segment['speaker']} "
                f"→ {voice_name}"
            )

            if segment[
                "chunk_total"
            ] > 1:

                print(
                    f"    Part "
                    f"{segment['chunk_index'] + 1}/"
                    f"{segment['chunk_total']}"
                )

            print(
                f"    {text}"
            )

            print()

            tts.tts_to_file(
                text=text,

                language=segment[
                    "language"
                ],

                speaker_wav=str(
                    prepared_voices[
                        voice_name
                    ]
                ),

                file_path=str(
                    output_wav
                ),

                split_sentences=False,

                temperature=segment[
                    "temperature"
                ],

                length_penalty=segment[
                    "length_penalty"
                ],

                repetition_penalty=segment[
                    "repetition_penalty"
                ],

                top_k=segment[
                    "top_k"
                ],

                top_p=segment[
                    "top_p"
                ],

                speed=segment[
                    "speed"
                ],
            )

            clips.append(
                output_wav
            )

        if not clips:

            raise RuntimeError(
                "No audio was generated."
            )

        # --------------------------------------------
        # Join clips directly into MP3
        # --------------------------------------------

        print()
        print("================================")
        print("ENCODING FINAL MP3")
        print("================================")
        print()

        concat_file = (
            temp_dir
            / "concat.txt"
        )

        with concat_file.open(
            "w",
            encoding="utf-8"
        ) as f:

            for clip in clips:

                f.write(
                    f"file '{clip.as_posix()}'\n"
                )

        # We encode straight to MP3.
        #
        # 128 kbps is a good audiobook compromise:
        # much smaller than WAV while retaining good speech quality.

        subprocess.run(
            [
                "ffmpeg",
                "-y",

                "-f",
                "concat",

                "-safe",
                "0",

                "-i",
                str(concat_file),

                "-af",
                "loudnorm=I=-18:TP=-2:LRA=7",

                "-ar",
                "44100",

                "-ac",
                "2",

                "-codec:a",
                "libmp3lame",

                "-b:a",
                "128k",

                str(output_file),
            ],
            check=True
        )

    print()
    print("================================")
    print("CHAPTER 1 COMPLETE")
    print("================================")
    print()

    print(
        f"Output: {output_file}"
    )

    print()


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "plan"
    )

    parser.add_argument(
        "output"
    )

    args = parser.parse_args()

    render_scene(
        args.plan,
        args.output
    )
