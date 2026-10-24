r"""Build ForesightWing.exe and the one-file installer ForesightWingSetup.exe:

  python packaging/build.py            → ForesightWingSetup.exe (and the ForesightWing folder) in %LOCALAPPDATA%\ForesightBuild
  python packaging/build.py --app-only → just the ForesightWing folder

The output stays out of the project folder on purpose: it is gigabytes, and this project sits in a OneDrive folder,
which would upload it (and holds files open while it does, which breaks rebuilds).

Needs PyInstaller, the app's requirements, and the speech models in the local Hugging Face cache (setup.bat downloads
them). Only the files Foresight Wing loads are bundled: Parakeet's int8 weights (not the 3 GB fp32 ones) and Whisper small.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("FORESIGHT_BUILD_DIR") or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ForesightBuild")
BUILD, DIST = OUT / "build", OUT / "dist"
sys.path.insert(0, str(ROOT))

from foresight import __version__, doctor  # noqa: E402
from foresight.config import settings  # noqa: E402

SKIP = {"encoder-model.onnx", "encoder-model.onnx.data", "decoder_joint-model.onnx"}  # Parakeet fp32 (int8 is used)


def models() -> None:
    """Copy the speech models into build/models, in the Hugging Face cache layout the loaders read offline."""
    from huggingface_hub import constants

    out = BUILD / "models"
    if out.exists():
        shutil.rmtree(out)
    for repo, needed in ((doctor.parakeet_repo(), f"*.{settings.parakeet_quantization or 'onnx'}*"),
                         (doctor.whisper_repo(), "model.bin")):
        src = Path(constants.HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}"
        if not any(src.glob(f"snapshots/*/{needed}")):
            sys.exit(f"{repo} is not in the Hugging Face cache: run setup.bat first")
        dst = out / src.name
        for sub in ("refs", "trees"):
            if (src / sub).is_dir():
                shutil.copytree(src / sub, dst / sub, dirs_exist_ok=True)
        for snap in (src / "snapshots").iterdir():
            (dst / "snapshots" / snap.name).mkdir(parents=True, exist_ok=True)
            for f in snap.iterdir():
                if f.name not in SKIP:
                    shutil.copy2(f.resolve(), dst / "snapshots" / snap.name / f.name)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"speech models: {size / 1e6:.0f} MB")


def artwork() -> None:
    """Icon (the Foresight Wing orb), the installer's splash picture, and the Windows version information."""
    from PIL import Image, ImageDraw, ImageFont

    import run

    run.tray_image(256).save(BUILD / "foresight.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    splash = Image.new("RGB", (460, 200), (240, 244, 249))
    splash.paste(run.tray_image(96), (28, 52), run.tray_image(96))
    draw = ImageDraw.Draw(splash)
    try:
        title, body = ImageFont.truetype("segoeui.ttf", 26), ImageFont.truetype("segoeui.ttf", 15)
    except OSError:
        title = body = ImageFont.load_default()
    draw.text((146, 66), "Foresight Wing", fill=(31, 31, 31), font=title)
    draw.text((146, 108), "Installing… this takes about a minute.", fill=(95, 99, 104), font=body)
    splash.save(BUILD / "splash.png")
    parts = (__version__.split(".") + ["0", "0", "0", "0"])[:4]
    nums = ", ".join(str(int(p)) for p in parts)
    (BUILD / "version.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({nums}), prodvers=({nums})),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'FOSSiNtellect'),
    StringStruct('FileDescription', 'Foresight Wing'),
    StringStruct('FileVersion', '{__version__}'),
    StringStruct('ProductName', 'Foresight Wing'),
    StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""", encoding="utf-8")


def pyinstaller(spec: str) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--log-level", "WARN",
                    "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"), str(ROOT / "packaging" / spec)],
                   check=True, cwd=ROOT, env={**os.environ, "FORESIGHT_BUILD_DIR": str(OUT)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-only", action="store_true", help="build dist/ForesightWing only, not the installer")
    args = parser.parse_args()
    t0 = time.time()
    BUILD.mkdir(parents=True, exist_ok=True)
    models()
    artwork()
    pyinstaller("foresight.spec")
    app = DIST / "ForesightWing"
    print(f"ForesightWing.exe folder: {sum(f.stat().st_size for f in app.rglob('*') if f.is_file()) / 1e9:.2f} GB")
    if not args.app_only:
        pyinstaller("installer.spec")
        print(f"ForesightWingSetup.exe: {(DIST / 'ForesightWingSetup.exe').stat().st_size / 1e9:.2f} GB")
    print(f"built in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
