# PyInstaller spec for ForesightWing.exe (a folder app: fast to start). Built by packaging/build.py, which first puts
# the speech models in build/models and the icon in build/foresight.ico.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
OUT = Path(os.environ.get("FORESIGHT_BUILD_DIR") or ROOT)  # set by build.py: outside the (OneDrive) project folder
BUILD = OUT / "build"

datas = [(str(ROOT / "foresight" / "static"), "foresight/static"), (str(BUILD / "models"), "models")]
for package in ("faster_whisper", "onnx_asr", "soundcard"):  # VAD model, audio preprocessors, Windows audio headers
    datas += collect_data_files(package)
for dist in ("onnx-asr", "faster-whisper", "huggingface-hub", "onnxruntime", "ctranslate2", "tokenizers", "av"):
    datas += copy_metadata(dist)  # these read their own version at run time (importlib.metadata)

hiddenimports = collect_submodules("uvicorn") + ["pystray._win32"]
hiddenimports += [f"foresight.{p.stem}" for p in (ROOT / "foresight").glob("*.py") if p.stem != "__init__"]

# Not used by Foresight Wing, but importable on a developer PC: ctranslate2 tries transformers/torch inside a try block.
excludes = ["torch", "transformers", "tensorflow", "jax", "keras", "matplotlib", "IPython", "notebook", "pytest",
            "sympy", "pandas", "pyarrow", "tkinter", "nvidia"]
GPU_ONLY = ("nvidia/", "nvidia\\", "onnxruntime_providers_cuda", "onnxruntime_providers_tensorrt")

a = Analysis([str(ROOT / "run.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=hiddenimports,
             binaries=collect_dynamic_libs("ctranslate2"), excludes=excludes, noarchive=False)
# Foresight Wing runs speech recognition on the CPU: leave out CUDA (about 1 GB of NVIDIA libraries that ctranslate2 and
# onnxruntime would pick up on a PC that has them) and the GPU providers.
a.binaries = [b for b in a.binaries if not any(g in b[0] for g in GPU_ONLY)]
a.datas = [d for d in a.datas if not any(g in d[0] for g in GPU_ONLY)]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ForesightWing", console=False,
          icon=str(BUILD / "foresight.ico"), version=str(BUILD / "version.txt"))
coll = COLLECT(exe, a.binaries, a.datas, name="ForesightWing")
