"""Адресация вложений: один компонент считает и storage-ключ, и путь в песочнице."""

from __future__ import annotations

import pytest
from conftest import FakeUrl

from boba.chainlit.domain.keys import (
    AttachmentLinks,
    AttachmentUrl,
    ObjectKey,
    ThreadDir,
    WorkspaceMount,
)

USER = "7"
THREAD = "11111111-1111-1111-1111-111111111111"
NAME = "report.pdf"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


MOUNT = "/workspace"
"""Точка рабочего каталога; в приложении её задаёт профиль песочницы."""


@pytest.fixture(autouse=True)
def workspace_mount() -> None:
    """В приложении точку ставит загрузчик инструментов из профиля."""
    WorkspaceMount.configure(MOUNT)


class TestPaths:
    """Ключ хранилища и путь того же файла внутри песочницы."""

    def test_render_is_storage_key(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        if key.render() != f"{USER}/{THREAD}/upload/{NAME}":
            raise AssertionError('key.render() == f"{USER}/{THREAD}/upload/{NAME}"')

    def test_in_thread_is_path_inside_image(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        if key.in_thread() != f"{THREAD}/upload/{NAME}":
            raise AssertionError('key.in_thread() == f"{THREAD}/upload/{NAME}"')

    def test_in_workspace_is_path_inside_sandbox(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        if key.in_workspace() != f"{MOUNT}/{THREAD}/upload/{NAME}":
            raise AssertionError('key.in_workspace() == f"{MOUNT}/{THREAD}/…')

    def test_parse_round_trip(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        if ObjectKey.parse(key.render()) != key:
            raise AssertionError("ObjectKey.parse(key.render()) == key")


class TestFromWorkspace:
    """Путь, которым оперирует агент, -> ключ хранилища."""

    def test_absolute_sandbox_path(self) -> None:
        path = f"{MOUNT}/{THREAD}/upload/{NAME}"
        key = ObjectKey.from_workspace(USER, THREAD, path)
        if key.render() != f"{USER}/{THREAD}/upload/{NAME}":
            raise AssertionError('key.render() == f"{USER}/{THREAD}/upload/{NAME}"')

    def test_path_without_mount_prefix(self) -> None:
        key = ObjectKey.from_workspace(USER, THREAD, f"{THREAD}/upload/{NAME}")
        if key.name != NAME:
            raise AssertionError("key.name == NAME")

    def test_round_trip_with_in_workspace(self) -> None:
        built = ObjectKey.build(USER, THREAD, NAME, "el-1")
        if ObjectKey.from_workspace(USER, THREAD, built.in_workspace()) != built:
            raise AssertionError("ObjectKey.from_workspace(USER, THREAD, built.in_wor…")

    def test_file_outside_upload_dir_rejected(self) -> None:
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(USER, THREAD, f"{MOUNT}/{NAME}")

    def test_error_names_the_expected_path(self) -> None:
        """По тексту ошибки агент понимает, куда положить файл."""
        with pytest.raises(ValueError, match="attachments dir") as failure:
            ObjectKey.from_workspace(USER, THREAD, f"{MOUNT}/{NAME}")
        wanted = f"{MOUNT}/{THREAD}/{{mermaid|upload}}/{NAME}"
        if wanted not in str(failure.value):
            raise AssertionError(f"текст ошибки не подсказывает путь: {wanted}")

    def test_foreign_thread_rejected(self) -> None:
        other = "22222222-2222-2222-2222-222222222222"
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(USER, THREAD, f"{MOUNT}/{other}/upload/{NAME}")

    def test_parent_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(USER, THREAD, f"{MOUNT}/{THREAD}/upload/..")

    def test_nested_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(
                USER, THREAD, f"{MOUNT}/{THREAD}/upload/sub/{NAME}"
            )

    def test_bare_name_rejected(self) -> None:
        """Голое имя двусмысленно: рядом с cwd агента лежит другой файл."""
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(USER, THREAD, NAME)


class TestMermaidDir:
    """Каталог mermaid/ адресуется тем же ключом, что и upload/."""

    SPEC = "schema.mmd"

    def test_render_is_storage_key(self) -> None:
        key = ObjectKey.build(
            USER, THREAD, self.SPEC, "el-1", dir_thread=ThreadDir.MERMAID
        )
        if key.render() != f"{USER}/{THREAD}/mermaid/{self.SPEC}":
            raise AssertionError('key.render() == f"{USER}/{THREAD}/mermaid/{self.SPE…')

    def test_in_workspace_points_to_mermaid_dir(self) -> None:
        key = ObjectKey.build(
            USER, THREAD, self.SPEC, "el-1", dir_thread=ThreadDir.MERMAID
        )
        if key.in_workspace() != f"{MOUNT}/{THREAD}/mermaid/{self.SPEC}":
            raise AssertionError('key.in_workspace() == f"{MOUNT}/{THREAD}/…')

    def test_parse_round_trip(self) -> None:
        key = ObjectKey.build(
            USER, THREAD, self.SPEC, "el-1", dir_thread=ThreadDir.MERMAID
        )
        if ObjectKey.parse(key.render()) != key:
            raise AssertionError("ObjectKey.parse(key.render()) == key")

    def test_from_workspace_accepts_mermaid_path(self) -> None:
        path = f"{MOUNT}/{THREAD}/mermaid/{self.SPEC}"
        key = ObjectKey.from_workspace(USER, THREAD, path)
        if key.dir != ThreadDir.MERMAID:
            raise AssertionError("key.dir == ThreadDir.MERMAID")
        if key.render() != f"{USER}/{THREAD}/mermaid/{self.SPEC}":
            raise AssertionError('key.render() == f"{USER}/{THREAD}/mermaid/{self.SPE…')

    def test_build_defaults_to_upload(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        if key.dir != ThreadDir.UPLOAD:
            raise AssertionError("key.dir == ThreadDir.UPLOAD")

    def test_unknown_dir_rejected_in_parse(self) -> None:
        with pytest.raises(ValueError, match="invalid object_key"):
            ObjectKey.parse(f"{USER}/{THREAD}/other/{self.SPEC}")

    def test_unknown_dir_rejected_in_workspace_path(self) -> None:
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(
                USER, THREAD, f"{MOUNT}/{THREAD}/other/{self.SPEC}"
            )


class TestLinks:
    def test_url_points_to_attachment_route(self) -> None:
        links = AttachmentLinks(FakeUrl.WORKSPACE)
        url = links.url(THREAD, "el-1", ThreadDir.UPLOAD)
        if not (url.endswith(AttachmentUrl(THREAD, ThreadDir.UPLOAD, "el-1").path())):
            raise AssertionError("url.endswith(AttachmentUrl(THREAD, ThreadDir.UPLOAD…")

    def test_url_keeps_the_directory(self) -> None:
        """Без каталога отдача искала бы файл только в upload/ — это был 404."""
        links = AttachmentLinks(FakeUrl.WORKSPACE)

        url = links.url(THREAD, "el-1", ThreadDir.MERMAID)

        if not (url.endswith(f"/attachment/{THREAD}/mermaid/el-1")):
            raise AssertionError('url.endswith(f"/attachment/{THREAD}/mermaid/el-1")')
