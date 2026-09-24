"""Общая настройка pytest для deploycli."""

import shutil

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Пропустить тесты под маркером ``docker``, если docker не найден в PATH."""
    if shutil.which("docker") is not None:
        return
    skip_docker = pytest.mark.skip(reason="docker не найден в PATH")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip_docker)
