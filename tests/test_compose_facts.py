"""Тесты чтения фактов о проекте через `docker compose config`.

Фикстуры в ``tests/fixtures/compose_config/*.json`` — дословные снимки,
снятые командой ``docker compose config --format json`` с реальных
временных проектов (см. заголовок каждого теста ниже, что описывал
исходный compose). Юнит-тесты в этом файле подменяют вызов ``docker`` этими
снимками, чтобы разбор JSON проверялся отдельно от наличия docker на
машине, где запускаются тесты.

Тесты под маркером ``docker`` запускают настоящий docker против проектов в
``tests/fixtures/docker_projects/`` и автоматически пропускаются, если
docker не найден в PATH (см. ``conftest.py``).
"""

import subprocess
from pathlib import Path

import pytest

from deploycli.compose_facts import ComposeScanError, scan_project

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_compose_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> None:
    def fake_run(*args: object, **kwargs: object) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("deploycli.compose_facts.subprocess.run", fake_run)


# Снято с проекта: сервис web (nginx:1.27) публикует 80 и 443 напрямую,
# сидит в networks default+proxy, несёт метку traefik.enable/router-правило
# и посторонюю метку com.example.owner; worker (myapp:worker) без портов и
# меток; cache (redis:7) без явных networks/ports/labels вовсе.
def test_reads_services_networks_ports_images_and_traefik_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = (FIXTURES / "compose_config" / "basic.json").read_text()
    _stub_compose_config(monkeypatch, stdout=payload, returncode=0)

    facts = scan_project(tmp_path)

    by_name = {service.name: service for service in facts.services}
    assert set(by_name) == {"web", "worker", "cache"}

    web = by_name["web"]
    assert web.image == "nginx:1.27"
    assert web.networks == ("default", "proxy")
    assert web.published_ports == (80, 443)
    assert dict(web.traefik_labels) == {
        "traefik.enable": "true",
        "traefik.http.routers.web.rule": "Host(`example.com`)",
    }
    assert "com.example.owner" not in web.traefik_labels
    assert web.publishes_edge_port is True

    worker = by_name["worker"]
    assert worker.image == "myapp:worker"
    assert worker.networks == ("default",)
    assert worker.published_ports == ()
    assert dict(worker.traefik_labels) == {}
    assert worker.publishes_edge_port is False

    cache = by_name["cache"]
    assert cache.image == "redis:7"
    assert cache.networks == ("default",)
    assert cache.published_ports == ()
    assert cache.publishes_edge_port is False


# Снято с проекта: единственный сервис legacy несёт метку Traefik в
# смешанном/верхнем регистре (Traefik читает метки регистронезависимо —
# ADR-003), плюс посторонюю метку com.example.owner.
def test_reads_traefik_labels_regardless_of_key_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = (FIXTURES / "compose_config" / "mixed_case_traefik_label.json").read_text()
    _stub_compose_config(monkeypatch, stdout=payload, returncode=0)

    facts = scan_project(tmp_path)

    assert len(facts.services) == 1
    legacy = facts.services[0]
    # Ключи хранятся дословно, как их написал автор compose: сообщение об
    # отказе генерации должно называть метку ровно так, иначе её не найти
    # поиском по файлу.
    assert dict(legacy.traefik_labels) == {
        "Traefik.enable": "true",
        "TRAEFIK.HTTP.ROUTERS.LEGACY.RULE": "Host(`legacy.example.com`)",
    }
    assert "com.example.owner" not in legacy.traefik_labels


# Снято с проекта: единственный сервис app публикует 3000 хостовым портом
# 8080 (80/443 наружу не заняты никем).
def test_no_service_publishes_edge_port_when_published_ports_are_not_80_or_443(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = (FIXTURES / "compose_config" / "no_edge_port.json").read_text()
    _stub_compose_config(monkeypatch, stdout=payload, returncode=0)

    facts = scan_project(tmp_path)

    assert len(facts.services) == 1
    app = facts.services[0]
    assert app.published_ports == (8080,)
    assert app.publishes_edge_port is False


def test_missing_docker_raises_compose_scan_error_with_os_error_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # PATH указывает на пустой каталог — docker в нём нет физически, поэтому
    # subprocess по-настоящему падает с FileNotFoundError, без подмены наших
    # функций.
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ComposeScanError) as excinfo:
        scan_project(tmp_path)

    assert "docker" in str(excinfo.value).lower()


def test_nonzero_exit_raises_compose_scan_error_with_verbatim_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stderr_text = (
        'service "web" refers to undefined network missing-network: invalid compose project'
    )
    _stub_compose_config(monkeypatch, returncode=1, stderr=stderr_text)

    with pytest.raises(ComposeScanError) as excinfo:
        scan_project(tmp_path)

    assert str(excinfo.value) == stderr_text


@pytest.mark.docker
def test_reads_project_with_yaml_anchor_and_extends() -> None:
    project_dir = FIXTURES / "docker_projects" / "anchors_and_extends"

    facts = scan_project(project_dir)

    by_name = {service.name: service for service in facts.services}
    assert set(by_name) == {"base", "web"}

    base = by_name["base"]
    assert base.image == "myapp:base"
    assert base.networks == ("default",)
    assert base.published_ports == ()
    assert dict(base.traefik_labels) == {}

    web = by_name["web"]
    assert web.image == "myapp:web"
    assert web.networks == ("default", "proxy")
    assert web.published_ports == (8080,)
    assert dict(web.traefik_labels) == {
        "traefik.enable": "true",
        "traefik.http.routers.web.rule": "Host(`example.com`)",
    }
    assert web.publishes_edge_port is False


@pytest.mark.docker
def test_reads_expanded_port_ranges_and_target_only_ports() -> None:
    # docker compose config сам разворачивает короткие формы записи портов
    # (диапазоны, target-only без публикации) до отдельных записей — модуль
    # должен читать именно этот развёрнутый вид, а не гадать по исходному
    # синтаксису compose.
    project_dir = FIXTURES / "docker_projects" / "port_ranges"

    facts = scan_project(project_dir)

    by_name = {service.name: service for service in facts.services}
    assert set(by_name) == {"worker", "edge", "headless"}

    worker = by_name["worker"]
    assert worker.published_ports == (8000, 8001, 8002)
    assert worker.publishes_edge_port is False

    edge = by_name["edge"]
    assert edge.published_ports == (80, 81)
    assert edge.publishes_edge_port is True

    headless = by_name["headless"]
    assert headless.published_ports == ()
    assert headless.publishes_edge_port is False


@pytest.mark.docker
def test_broken_compose_raises_with_verbatim_compose_stderr() -> None:
    project_dir = FIXTURES / "docker_projects" / "broken"

    expected = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert expected.returncode != 0, "фикстура должна быть невалидной"

    with pytest.raises(ComposeScanError) as excinfo:
        scan_project(project_dir)

    assert str(excinfo.value) == expected.stderr
