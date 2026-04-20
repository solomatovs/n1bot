"""Растущий байтовый буфер для чтения строк из файла без повторных аллокаций.

Получает fd в конструкторе, не владеет им (не закрывает):

    with open(path, "rb") as f:
        buf = GrowBuffer(f)
        for line in buf.iter_lines_forward(b"\\n", offset=0):
            process(line)
        next_offset = buf.consumed

Контракт времени жизни memoryview:
    Выданные ``iter_lines_*`` и ``tail()`` ``memoryview`` валидны до следующего
    вызова любого метода, который читает из fd (``iter_lines_*``). Если при
    таком повторном чтении буфер должен вырасти, а на него есть живые
    ``memoryview`` — будет поднят ``BufferError``. Это защищает от тихого
    stale-указателя: клиент видит громкую ошибку и обязан освободить/
    материализовать (``bytes(mv)``) старые ``memoryview`` до следующего чтения.
"""

from __future__ import annotations

from collections.abc import Iterator
from io import BufferedReader


class GrowBuffer:
    """Байтовый буфер, который растёт но никогда не сжимается.

    Принимает fd в конструкторе, читает через readinto() прямо в bytearray.
    Не владеет fd — вызывающий код отвечает за открытие и закрытие.
    """

    _INITIAL_CAPACITY = 4096

    def __init__(
        self, fd: BufferedReader, *, max_capacity: int | None = None
    ) -> None:
        """Создать буфер поверх ``fd``.

        :param max_capacity: верхняя граница ёмкости в байтах. ``None``
            (по умолчанию) — без ограничения. Если при чтении требуется
            вырасти больше ``max_capacity`` — поднимается ``BufferError``.
            Если ``max_capacity`` меньше ``_INITIAL_CAPACITY`` — начальная
            ёмкость урезается до ``max_capacity``.
        """
        if max_capacity is not None and max_capacity <= 0:
            raise ValueError(f"max_capacity must be positive, got {max_capacity}")

        self._fd = fd
        self._max_capacity = max_capacity
        initial = self._INITIAL_CAPACITY
        if max_capacity is not None:
            initial = min(initial, max_capacity)
        self._buf = bytearray(initial)
        self._size = 0
        self._consumed = 0

    @property
    def capacity(self) -> int:
        """Текущая ёмкость буфера (никогда не уменьшается)."""
        return len(self._buf)

    @property
    def max_capacity(self) -> int | None:
        """Верхняя граница ёмкости. ``None`` — без ограничения."""
        return self._max_capacity

    @property
    def consumed(self) -> int:
        """Количество байт, обработанных последним iter_lines_*()."""
        return self._consumed

    def tail(self) -> memoryview:
        """Непотреблённый хвост буфера — байты после последнего separator.

        Это «неполная последняя строка» без завершителя. Пустая memoryview,
        если файл заканчивается separator'ом или буфер пуст.
        """
        return memoryview(self._buf)[self._consumed : self._size]

    def iter_lines_forward(
        self, separator: bytes, offset: int = 0
    ) -> Iterator[memoryview]:
        """Читает [offset, EOF] и yield'ит полные строки в порядке от начала к концу.

        Неполная последняя строка (без separator) не включается.
        Количество обработанных байт от offset доступно через ``consumed``.
        """
        end = self._load(separator, offset)
        if end == 0:
            return

        sep_len = len(separator)
        start = 0
        while start < end:
            pos = self._buf.find(separator, start, end)
            if pos == -1:
                break
            yield memoryview(self._buf)[start:pos]
            start = pos + sep_len

    def iter_lines_backward(
        self, separator: bytes, offset: int = 0
    ) -> Iterator[memoryview]:
        """Читает [offset, EOF] и yield'ит полные строки в обратном порядке.

        Неполная последняя строка (без separator) не включается.
        Количество обработанных байт от offset доступно через ``consumed``.
        """
        end = self._load(separator, offset)
        if end == 0:
            return

        sep_len = len(separator)
        line_end = end - sep_len
        while True:
            prev_sep = self._buf.rfind(separator, 0, line_end)
            if prev_sep == -1:
                yield memoryview(self._buf)[0:line_end]
                return
            line_start = prev_sep + sep_len
            yield memoryview(self._buf)[line_start:line_end]
            line_end = prev_sep

    def _load(self, separator: bytes, offset: int) -> int:
        """Прочитать [offset, EOF] в буфер и вернуть позицию после последнего separator.

        Возвращает 0 если нечего итерировать (пустой регион или нет separator'а).
        Побочно обновляет ``consumed``.
        """
        self._fd.seek(offset)
        self._fill()

        if self._size == 0:
            self._consumed = 0
            return 0

        last_sep = self._buf.rfind(separator, 0, self._size)
        if last_sep == -1:
            self._consumed = 0
            return 0

        end = last_sep + len(separator)
        self._consumed = end
        return end

    def _fill(self) -> None:
        """Прочитать данные из fd в буфер (от текущей позиции fd)."""
        self._size = 0
        while True:
            if self._size == len(self._buf):
                self._grow()
            n = self._fd.readinto(memoryview(self._buf)[self._size :])
            if not n:
                break
            self._size += n

    def _grow(self) -> None:
        """Удвоить ёмкость буфера in-place, но не выше ``max_capacity``.

        Падает с ``BufferError`` в двух случаях:
        - достигнут ``max_capacity`` и дальше расти некуда;
        - на bytearray есть живые экспорты (ранее выданные ``memoryview``)
          — рост при живых view-шках привёл бы к stale-указателю.
        """
        cur = len(self._buf)
        target = cur * 2
        if self._max_capacity is not None:
            target = min(target, self._max_capacity)

        grow_by = target - cur
        if grow_by <= 0:
            raise BufferError(
                f"GrowBuffer: capacity {cur} reached max_capacity "
                f"{self._max_capacity} — input exceeds configured limit"
            )

        try:
            self._buf += b"\x00" * grow_by
        except BufferError as e:
            raise BufferError(
                "GrowBuffer: cannot grow while memoryviews are outstanding — "
                "release or materialize (bytes(mv)) previously yielded views "
                "before the next iter_lines_*/fill"
            ) from e
