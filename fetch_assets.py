"""Download public pinned artifacts to this experiment only (macOS arm64)."""
import argparse
import hashlib
import shutil
import tempfile
import subprocess
import tarfile
from pathlib import Path
from corpus import STATE, DOWNLOADS, INPUTS

DATA = "https://github.com/onyx-dot-app/EnterpriseRAG-Bench/releases/download/v1.0.0/"
E5 = "https://huggingface.co/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78/"
QWEN = "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/bc640142c66e1fdd12af0bd68f40445458f3869b/"
XBIN = "xerj-1.0.0-rc.104-aarch64-apple-darwin"
LLAMA = "https://github.com/ggml-org/llama.cpp/releases/download/b12541/llama-b12541-bin-macos-arm64.tar.gz"


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def fetch(url, path, expected):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and digest(path)==expected:
        return
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        tmp = Path(f.name)
    try:
        subprocess.run(["curl","--fail","--location","--retry","2","--silent","--show-error",
                        "--output",str(tmp),url],check=True)
        if digest(tmp)!=expected:
            raise ValueError(f"Checksum mismatch for {path.name}")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agent",action="store_true",help="Also download Qwen and llama.cpp")
    args = p.parse_args()
    for name, sha in INPUTS.items():
        fetch(DATA+name,DOWNLOADS/name,sha)
    models, binaries = STATE/"models",STATE/"bin"
    binaries.mkdir(exist_ok=True)
    fetch(E5+"onnx/model_int8.onnx",models/"model_int8.onnx","4d24e2bc01a447951524466ef533e52944bf48509e6552810bcee1a2711cb02c")
    fetch(E5+"tokenizer.json",models/"tokenizer.json","0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39")
    arc = DOWNLOADS/(XBIN+".tar.gz")
    fetch("https://github.com/xerj-org/xerj/releases/download/v1.0.0-rc.104/"+arc.name,arc,
          "5f16b17dcf46abb566ec33a0f4733b459c0fbc229fcd0f58a9e6ad698f37d58e")
    if not (binaries/"xerj").exists():
        with tarfile.open(arc) as tar, tar.extractfile(XBIN+"/xerj") as src:
            with (binaries/"xerj").open("wb") as dest:
                shutil.copyfileobj(src,dest)
        (binaries/"xerj").chmod(0o755)
    if args.agent:
        name = "Qwen3-4B-Q4_K_M.gguf"
        fetch(QWEN+name,models/name,"7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5")
        arc = DOWNLOADS/"llama.tar.gz"
        fetch(LLAMA,arc,"464ed76a5faebf527a264efc87f41f8c97319dd4f7cbd21e59dc51c1582cc813")
        if not (binaries/"llama-b12541"/"llama-server").exists():
            with tarfile.open(arc) as tar:
                tar.extractall(binaries, filter="data")


if __name__=="__main__":
    main()
