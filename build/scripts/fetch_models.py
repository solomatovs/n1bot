"""Загрузка моделей в runtime/models: идёт внутри образа base — там pip и CA контура.

Вызов:
  fetch_models.py fastembed <модель> <каталог кэша>
  fetch_models.py onnx <репозиторий hf> <подкаталог> <каталог назначения>
  fetch_models.py links <каталог кэша> — заменить ссылки кэша файлами
"""

import pathlib
import shutil
import sys
from enum import StrEnum


class Kind(StrEnum):
    FASTEMBED = "fastembed"
    ONNX = "onnx"
    LINKS = "links"


def fetch_fastembed(model: str, cache_dir: str) -> None:
    from fastembed import TextEmbedding

    embedding = TextEmbedding(model_name=model, cache_dir=cache_dir)
    list(embedding.embed(["probe"]))
    replaced = replace_links(pathlib.Path(cache_dir))
    print(f">>> fastembed: {model} -> {cache_dir}, links replaced: {replaced}")


def replace_links(cache_dir: pathlib.Path) -> int:
    """Кэш huggingface кладёт файл в blobs/, а в snapshots/ ставит на него
    символическую ссылку. Ссылок в runtime-каталоге быть не должно: файл
    переезжает на место ссылки — так же библиотека раскладывает кэш там, где
    ссылки не поддерживаются, и офлайн-загрузка этот вид читает."""
    links: list[pathlib.Path] = []
    for path in sorted(cache_dir.rglob("*")):
        if path.is_symlink():
            links.append(path)

    for link in links:
        target = link.resolve(strict=True)
        link.unlink()
        shutil.move(str(target), str(link))

    return len(links)


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
    if kind is Kind.LINKS:
        print(f">>> links replaced: {replace_links(pathlib.Path(argv[2]))}")
        return 0

    if kind is Kind.FASTEMBED:
        fetch_fastembed(argv[2], argv[3])
        return 0

    fetch_onnx(argv[2], argv[3], argv[4])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
