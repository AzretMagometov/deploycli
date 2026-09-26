"""Край хоста одним шаблоном: подготовка и документация (ADR-002, ADR-003, ADR-006).

Golden-файл ``tests/fixtures/host_edge/compose.yml`` фиксирует байты
compose края хоста. Остальные тесты читают тот же вывод как модель: имена
контракта обязаны стоять в нём подстановкой из :mod:`deploycli.host_edge`,
а не литералом в шаблоне — тогда правка константы меняет разом и то, что
поднимает подготовка, и пример в документации (dpc-tm3.65).
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from deploycli.host_edge import (
    ACME_STORAGE,
    CERT_RESOLVER,
    EDGE_IMAGE,
    EDGE_NETWORK,
    EDGE_PROJECT,
    EDGE_SERVICE,
    ENTRYPOINT_HTTP,
    ENTRYPOINT_HTTPS,
    InvalidAcmeEmailError,
    render_host_edge_compose,
)
from deploycli.traefik_labels import PublicRoute, service_labels

GOLDEN = Path(__file__).parent / "fixtures" / "host_edge" / "compose.yml"
EMAIL = "ops@example.com"


def _edge_model(email: str = EMAIL) -> dict[str, Any]:
    model: dict[str, Any] = yaml.safe_load(render_host_edge_compose(email))
    return model


def _edge_command(email: str = EMAIL) -> tuple[str, ...]:
    return tuple(_edge_model(email)["services"][EDGE_SERVICE]["command"])


def test_rendered_edge_compose_matches_golden() -> None:
    assert render_host_edge_compose(EMAIL) == GOLDEN.read_text()


def test_edge_compose_is_valid_yaml_with_fixed_project_name() -> None:
    model = _edge_model()

    assert model["name"] == EDGE_PROJECT
    assert model["services"][EDGE_SERVICE]["image"] == EDGE_IMAGE


def test_edge_binds_both_entrypoints_of_the_contract() -> None:
    command = _edge_command()

    assert f"--entrypoints.{ENTRYPOINT_HTTP}.address=:80" in command
    assert f"--entrypoints.{ENTRYPOINT_HTTPS}.address=:443" in command
    assert set(_edge_model()["services"][EDGE_SERVICE]["ports"]) == {"80:80", "443:443"}


def test_edge_redirects_on_the_entrypoint_not_by_middleware() -> None:
    # ADR-003: редирект живёт на самой точке входа — одно место на хост, и
    # предполёт проверяет его запросом с хоста.
    command = _edge_command()
    redirect = f"--entrypoints.{ENTRYPOINT_HTTP}.http.redirections.entryPoint"

    assert f"{redirect}.to={ENTRYPOINT_HTTPS}" in command
    assert f"{redirect}.scheme=https" in command
    assert not any("middlewares" in arg for arg in command)


def test_edge_resolver_is_named_by_the_contract_and_takes_the_given_email() -> None:
    command = _edge_command("owner@dpc.test")
    resolver = f"--certificatesresolvers.{CERT_RESOLVER}.acme"

    assert f"{resolver}.email=owner@dpc.test" in command
    assert f"{resolver}.storage={ACME_STORAGE}" in command
    assert f"{resolver}.httpchallenge=true" in command
    assert f"{resolver}.httpchallenge.entrypoint={ENTRYPOINT_HTTP}" in command


def test_acme_storage_lives_on_a_named_volume() -> None:
    # Без тома acme.json уезжает вместе с контейнером, и край запрашивает
    # сертификаты заново на каждом перезапуске — до предела Let's Encrypt.
    model = _edge_model()
    volume_dir = ACME_STORAGE.rsplit("/", 1)[0]
    mounts: list[str] = model["services"][EDGE_SERVICE]["volumes"]
    acme_mounts = [mount for mount in mounts if mount.endswith(f":{volume_dir}")]

    assert len(acme_mounts) == 1
    volume_name = acme_mounts[0].split(":")[0]
    assert not volume_name.startswith("/"), "хранилище ACME — именованный том, не bind"
    assert volume_name in model["volumes"]


def test_edge_joins_the_external_contract_network() -> None:
    model = _edge_model()

    assert model["networks"] == {EDGE_NETWORK: {"external": True}}
    assert model["services"][EDGE_SERVICE]["networks"] == [EDGE_NETWORK]
    assert f"--providers.docker.network={EDGE_NETWORK}" in _edge_command()


def test_edge_does_not_enable_the_file_provider() -> None:
    # ADR-006: каталог файлового провайдера не заводится, провайдер не
    # включается — иначе у предполёта появляется слепое пятно.
    assert not any("providers.file" in arg for arg in _edge_command())


def test_edge_does_not_expose_containers_by_default() -> None:
    assert "--providers.docker.exposedbydefault=false" in _edge_command()


def test_service_labels_speak_the_same_contract_names() -> None:
    # Метки подключённого проекта и край хоста обязаны называть одно и то же:
    # сеть, точку входа и резолвер. Шаблон меток держит их литералами
    # (dpc-tm3.66 — отдельный тикет), поэтому расхождение ловит этот тест.
    labels = service_labels("web", PublicRoute(domains=("example.com",), port=3000))

    assert f"traefik.docker.network={EDGE_NETWORK}" in labels
    assert any(label.endswith(f".entrypoints={ENTRYPOINT_HTTPS}") for label in labels)
    assert any(label.endswith(f".tls.certresolver={CERT_RESOLVER}") for label in labels)


@pytest.mark.parametrize(
    "email",
    ["", "   ", "ops@example.com\n--providers.file.directory=/x", "o'ps@example.com", "ops"],
)
def test_email_that_cannot_be_safely_substituted_is_refused(email: str) -> None:
    # Адрес попадает и в аргумент командной строки Traefik, и в скрипт
    # подготовки: перевод строки или кавычка в нём — чужая строка в чужом
    # файле, а не опечатка.
    with pytest.raises(InvalidAcmeEmailError):
        render_host_edge_compose(email)
