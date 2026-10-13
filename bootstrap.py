"""一次性安装模型；从固定官方来源下载并校验，不执行下载的脚本。"""
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
manifest = json.loads((ROOT / "models-manifest.json").read_text(encoding="utf-8"))


def fetch(url, path, expected=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (not expected or hashlib.sha256(path.read_bytes()).hexdigest().startswith(expected)):
        return
    temp = path.with_suffix(".download")
    subprocess.run(["curl.exe", "--fail", "-L", "--retry", "2", "--connect-timeout", "20",
                    "--max-time", "600", "-H", "Accept: application/octet-stream", url,
                    "-o", str(temp)], check=True)
    if expected and not hashlib.sha256(temp.read_bytes()).hexdigest().startswith(expected):
        temp.unlink(missing_ok=True)
        raise RuntimeError("下载文件校验不一致，请重试：" + path.name)
    temp.replace(path)


def main():
    model = manifest["matanyone2"]
    vendor = ROOT / "vendor" / "MatAnyone2"
    if not (vendor / "matanyone2" / "__init__.py").exists():
        with tempfile.TemporaryDirectory(prefix="video-matting-setup-") as temporary:
            archive = Path(temporary) / "source.zip"
            fetch(f"https://codeload.github.com/pq-yang/MatAnyone2/zip/{model['source_commit']}", archive)
            with zipfile.ZipFile(archive) as package:
                base = Path(temporary).resolve()
                for member in package.infolist():
                    if not (base / member.filename).resolve().is_relative_to(base):
                        raise RuntimeError("源代码包含有无效路径")
                package.extractall(base)
            source = Path(temporary) / ("MatAnyone2-" + model["source_commit"])
            vendor.parent.mkdir(exist_ok=True)
            if vendor.exists():
                raise RuntimeError("模型源码目录不完整，请备份后重新准备。")
            shutil.move(str(source), str(vendor))
    fetch(model["asset_url"], ROOT / "models" / "matanyone2.pth", model["sha256"])
    selector = manifest["selector"]
    fetch(selector["url"], ROOT / "models" / "torch" / "hub" / "checkpoints" / Path(selector["url"]).name,
          selector["sha256_prefix"])
    sam = manifest["sam2"]
    sam_vendor = ROOT / "vendor/SAM2"
    if not (sam_vendor / "sam2/__init__.py").exists():
        with tempfile.TemporaryDirectory(prefix="video-matting-sam2-") as temporary:
            archive = Path(temporary) / "source.zip"
            fetch(f"https://codeload.github.com/{sam['repository']}/zip/{sam['source_commit']}", archive)
            with zipfile.ZipFile(archive) as package:
                base = Path(temporary).resolve()
                for member in package.infolist():
                    if not (base / member.filename).resolve().is_relative_to(base):
                        raise RuntimeError("SAM 2 源码包含无效路径")
                package.extractall(base)
            source = Path(temporary) / ("sam2-" + sam["source_commit"])
            if sam_vendor.exists():
                raise RuntimeError("SAM 2 源码目录不完整，请备份后重新准备。")
            shutil.move(str(source), str(sam_vendor))
    fetch(sam["url"], ROOT / "models/sam2.1_hiera_tiny.pt", sam["sha256"])
    print("模型下载与校验完成。")


if __name__ == "__main__":
    main()
