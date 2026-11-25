"""Generate original Oshified test images, audio, and watermark videos."""
import argparse
from array import array
import math
from pathlib import Path
import random
import subprocess
import sys
import wave

from PIL import Image, ImageDraw


def create_portrait(path, avatar):
    size = (768, 768)
    image = Image.new("RGB", size, "#8a9fb0" if avatar else "#aab1a8")
    draw = ImageDraw.Draw(image)
    skin = "#ecc4a2" if avatar else "#cda37f"
    edge = "#302439" if avatar else "#655244"
    draw.rounded_rectangle((126, 29, 644, 699), radius=220, fill="#7f6584" if avatar else "#7f8577")
    draw.ellipse((116, 546, 656, 924), fill="#6683a1" if avatar else "#475968")
    draw.rounded_rectangle((318, 461, 450, 593), radius=56, fill=skin)
    draw.ellipse((198, 157, 571, 529), fill=skin, outline=edge, width=8)
    draw.pieslice((179, 113, 586, 423), start=180, end=360, fill=edge)
    for x in (301, 467):
        draw.ellipse((x - 46, 287, x + 46, 339), fill="#ede9e4", outline=edge, width=7)
        draw.ellipse((x - 18, 293, x + 18, 338), fill="#53697b" if avatar else "#554331")
        draw.ellipse((x - 8, 303, x + 8, 327), fill="#211f24")
        draw.arc((x - 53, 259, x + 53, 303), 190, 340, fill=edge, width=9)
    draw.line((382, 326, 369, 376, 390, 378), fill="#9e7967", width=8)
    draw.arc((335, 380, 441, 452), 16, 166, fill="#8d514d", width=10)
    image.save(path)


def video(path, color, duration, audio=False):
    vf = "drawbox=x=528:y=548:w=88:h=52:color=white:t=fill"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
               "-i", f"color=c={color}:s=640x640:r=16:d={duration}"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1"]
    subprocess.run(command + ["-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def create_audio(path):
    """Create an original two-second sound with softly modulated voice formants."""
    sample_rate = 24000
    samples = array("h")
    rng = random.Random(17017)
    syllables = [
        (0.32, 116, (650, 1700, 2600)), (0.26, 142, (420, 2150, 2900)),
        (0.12, 0, (0, 0, 0)), (0.43, 131, (580, 1250, 2450)),
        (0.13, 0, (0, 0, 0)), (0.56, 120, (340, 900, 2400)),
    ]
    for duration, pitch, formants in syllables:
        for index in range(round(sample_rate * duration)):
            t = index / sample_rate
            window = min(t / 0.045, (duration - t) / 0.060, 1)
            if pitch == 0:
                amplitude = rng.uniform(-0.04, 0.04)
            else:
                base = pitch * (1 + 0.017 * math.sin(2 * math.pi * 4.2 * t) + 0.04 * t)
                amplitude = 0.24 * math.sin(2 * math.pi * base * t)
                for strength, formant in zip((0.12, 0.08, 0.04), formants):
                    harmonic = round(formant / pitch)
                    amplitude += strength * math.sin(2 * math.pi * harmonic * base * t)
            samples.append(round(amplitude * window * 32767))
    if sys.byteorder == "big":
        samples.byteswap()
    with wave.open(str(path), "wb") as track:
        track.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        track.writeframes(samples.tobytes())


def create_media(root):
    root.mkdir(exist_ok=False)
    create_audio(root / "voice-formant.wav")
    create_portrait(root / "avatar.png", True)
    create_portrait(root / "natural.png", False)
    create_portrait(root / "pose.png", False)
    video(root / "end-card.mp4", "0x253A49", "1", audio=True)
    video(root / "corner.mp4", "0x466B80", "0.5")
    video(root / "corner-mask.mp4", "black", "0.5")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for the synthetic media")
    create_media(parser.parse_args().output)
