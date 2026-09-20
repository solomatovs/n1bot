"""Вырезка каталогов из tar-потока с переносом по новому пути.

Читает tar со stdin, пишет tar в stdout: остаются только члены из указанных
каталогов, их имена переписываются на новые. Владельцы и права сохраняются,
на диск ничего не ложится.

Вызов: tarsub.py <из>=<в> [<из>=<в> ...]
  tarsub.py opt/site=usr/local/lib/python3.11/site-packages opt/code-sandbox=usr/src
"""

import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Final


class TarMode:
    """Режимы tarfile.open: типизация требует строковых литералов,
    поэтому Final, а не enum."""

    READ_STREAM: Final = "r|"
    WRITE_STREAM: Final = "w|"


@dataclass(frozen=True)
class Move:
    source: str
    target: str

    @classmethod
    def parse(cls, raw: str) -> "Move":
        source, separator, target = raw.partition("=")
        if not separator:
            raise SystemExit(f"tarsub: expected <from>=<to>, got {raw!r}")

        return cls(source.strip("/"), target.strip("/"))

    def rename(self, name: str) -> str | None:
        stripped = name.lstrip("./")
        if stripped == self.source:
            return self.target

        if not stripped.startswith(self.source + "/"):
            return None

        return self.target + stripped[len(self.source) :]


class TarSubset:
    """Переписывает поток tar, оставляя члены под указанными каталогами."""

    def __init__(self, moves: list[Move]) -> None:
        self._moves = moves

    def run(self, source: tarfile.TarFile, target: tarfile.TarFile) -> int:
        count = 0
        for member, payload in self._selected(source):
            target.addfile(member, payload)
            count += 1

        return count

    def _selected(
        self, source: tarfile.TarFile
    ) -> Iterator[tuple[tarfile.TarInfo, IO[bytes] | None]]:
        for member in source:
            renamed = self._rename(member.name)
            if renamed is None:
                continue

            member.name = renamed
            if member.islnk():
                member.linkname = self._rename(member.linkname) or member.linkname

            payload: IO[bytes] | None = None
            if member.isfile():
                payload = source.extractfile(member)

            yield member, payload

    def _rename(self, name: str) -> str | None:
        for move in self._moves:
            renamed = move.rename(name)
            if renamed is not None:
                return renamed

        return None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    moves = [Move.parse(raw) for raw in argv[1:]]
    with (
        tarfile.open(fileobj=sys.stdin.buffer, mode=TarMode.READ_STREAM) as source,
        tarfile.open(fileobj=sys.stdout.buffer, mode=TarMode.WRITE_STREAM) as target,
    ):
        count = TarSubset(moves).run(source, target)

    print(f"tarsub: {count} entries", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
