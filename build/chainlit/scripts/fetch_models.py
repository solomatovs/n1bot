"""Загрузка моделей в build/src: запускается внутри образа base, где есть pip и CA контура.

Вызов:
  fetch_models.py fastembed <модель> <каталог кэша>
  fetch_models.py onnx <репозиторий hf> <подкаталог> <каталог назначения>
"""

import pathlib
import shutil
import sys
from enum import StrEnum


class Kind(StrEnum):
    FASTEMBED = "fastembed"
    ONNX = "onnx"


def fetch_fastembed(model: str, cache_dir: str) -> None:
    from fastembed import TextEmbedding

    embedding = TextEmbedding(model_name=model, cache_dir=cache_dir)
    list(embedding.embed(["probe"]))
    print(f">>> fastembed: {model} -> {cache_dir}")


def fetch_onnx(repo: str, subdir: str, dest: str) -> None:
    from huggingface_hub import snapshot_download

    root = snapshot_download(repo, allow_patterns=[subdir + "*"])
    target = pathlib.Path(dest)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(pathlib.Path(root) / subdir, target)
    print(f">>> onnx-genai: {repo} -> {target}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    kind = Kind(argv[1])
    if kind is Kind.FASTEMBED:
        fetch_fastembed(argv[2], argv[3])
        return 0

    fetch_onnx(argv[2], argv[3], argv[4])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
