"""Адресация вложений: один компонент считает и storage-ключ, и путь в песочнице."""

from __future__ import annotations

import pytest

from boba.chainlit.chat.data.object_key import AttachmentLinks, AttachmentUrl, ObjectKey
from boba.sandbox import WORKSPACE_MOUNT

USER = "7"
THREAD = "11111111-1111-1111-1111-111111111111"
NAME = "report.pdf"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestPaths:
    """Ключ хранилища и путь того же файла внутри песочницы."""

    def test_render_is_storage_key(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        assert key.render() == f"{USER}/{THREAD}/upload/{NAME}"

    def test_in_thread_is_path_inside_image(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        assert key.in_thread() == f"{THREAD}/upload/{NAME}"

    def test_in_workspace_is_path_inside_sandbox(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        assert key.in_workspace() == f"{WORKSPACE_MOUNT}/{THREAD}/upload/{NAME}"

    def test_parse_round_trip(self) -> None:
        key = ObjectKey.build(USER, THREAD, NAME, "el-1")
        assert ObjectKey.parse(key.render()) == key


class TestFromWorkspace:
    """Путь, которым оперирует агент, -> ключ хранилища."""

    def test_absolute_sandbox_path(self) -> None:
        path = f"{WORKSPACE_MOUNT}/{THREAD}/upload/{NAME}"
        key = ObjectKey.from_workspace(USER, THREAD, path)
        assert key.render() == f"{USER}/{THREAD}/upload/{NAME}"

    def test_path_without_mount_prefix(self) -> None:
        key = ObjectKey.from_workspace(USER, THREAD, f"{THREAD}/upload/{NAME}")
        assert key.name == NAME

    def test_round_trip_with_in_workspace(self) -> None:
        built = ObjectKey.build(USER, THREAD, NAME, "el-1")
        assert ObjectKey.from_workspace(USER, THREAD, built.in_workspace()) == built

    def test_file_outside_upload_dir_rejected(self) -> None:
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(USER, THREAD, f"{WORKSPACE_MOUNT}/{NAME}")

    def test_error_names_the_expected_path(self) -> None:
        """По тексту ошибки агент понимает, куда положить файл."""
        with pytest.raises(ValueError, match="attachments dir") as failure:
            ObjectKey.from_workspace(USER, THREAD, f"{WORKSPACE_MOUNT}/{NAME}")
        assert f"{WORKSPACE_MOUNT}/{THREAD}/upload/{NAME}" in str(failure.value)

    def test_foreign_thread_rejected(self) -> None:
        other = "22222222-2222-2222-2222-222222222222"
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(
                USER, THREAD, f"{WORKSPACE_MOUNT}/{other}/upload/{NAME}"
            )

    def test_parent_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(
                USER, THREAD, f"{WORKSPACE_MOUNT}/{THREAD}/upload/.."
            )

    def test_nested_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(
                USER, THREAD, f"{WORKSPACE_MOUNT}/{THREAD}/upload/sub/{NAME}"
            )

    def test_bare_name_rejected(self) -> None:
        """Голое имя двусмысленно: рядом с cwd агента лежит другой файл."""
        with pytest.raises(ValueError, match="attachments dir"):
            ObjectKey.from_workspace(USER, THREAD, NAME)


class TestLinks:
    def test_url_points_to_attachment_route(self) -> None:
        links = AttachmentLinks("http://boba/workspace")
        url = links.url(THREAD, "el-1")
        assert url.endswith(AttachmentUrl(THREAD, "el-1").path())
