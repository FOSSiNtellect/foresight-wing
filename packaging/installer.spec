# PyInstaller spec for ForesightWingSetup.exe: one file carrying the whole ForesightWing folder (dist/ForesightWing). Built by
# packaging/build.py after foresight.spec. A splash picture shows while the files unpack.
import os
from pathlib import Path

ROOT = Path(SPECPATH).parent
OUT = Path(os.environ.get("FORESIGHT_BUILD_DIR") or ROOT)  # set by build.py: outside the (OneDrive) project folder
BUILD = OUT / "build"
DIST = OUT / "dist"

a = Analysis([str(ROOT / "packaging" / "installer.py")], pathex=[str(ROOT)],
             datas=[(str(DIST / "ForesightWing"), "payload/ForesightWing")],
             excludes=["numpy", "PIL", "httpx", "fastapi", "uvicorn", "onnxruntime", "ctranslate2", "av", "torch",
                       "transformers"])
splash = Splash(str(BUILD / "splash.png"), binaries=a.binaries, datas=a.datas, text_pos=None)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, splash, splash.binaries, a.binaries, a.datas, [], name="ForesightWingSetup", console=False,
          icon=str(BUILD / "foresight.ico"), version=str(BUILD / "version.txt"), upx=False)
