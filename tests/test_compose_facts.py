"""Тесты безопасного чтения проекта (ADR-011, ADR-004, dpc-tm3.48).

Фикстуры в ``tests/fixtures/compose_config/*.json`` — дословные снимки,
снятые безопасным режимом
``COMPOSE_PROFILES='*' docker compose config --no-interpolate
--no-env-resolution --format json`` с реальных временных проектов (что
описывал исходный compose, сказано в заголовке каждого теста). Снимок
``safe_read_variables.json`` снят тем же режимом с ключом ``--variables``.

Абсолютные пути в снимках (каталог сборки, `env_file`, bind-монтирование)
заменены на ``/projects/app``: машина, где снимали, в фикстуре не
хранится. Тесты, которым нужен настоящий каталог, подставляют вместо
этого корня ``tmp_path``.

Тесты под маркером ``docker`` запускают настоящий docker против проектов
в ``tests/fixtures/docker_projects/`` и автоматически пропускаются, если
docker не найден в PATH (см. ``conftest.py``).
"""

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from deploycli.compose_facts import (
    ComposeScanError,
    ProjectFacts,
    ScanRefused,
    ServiceFacts,
    scan_project,
)

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_ROOT = "/projects/app"
SAFE_READ_PROJECT = FIXTURES / "docker_projects" / "safe_read"


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _ComposeStub:
    """Подмена ``subprocess.run``: отдаёт снимки вместо запуска docker.

    Безопасное чтение зовёт ``docker compose config`` дважды — за моделью
    и за переменными, — поэтому подмена обязана различать прогоны по
    аргументам, а не отдавать один и тот же ответ на оба.
    """

    def __init__(
        self,
        *,
        config: str = "{}",
        variables: str = "{}",
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        self._config = config
        self._variables = variables
        self._returncode = returncode
        self._stderr = stderr
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, args: Sequence[str], **kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append((tuple(args), kwargs))
        if self._returncode != 0:
            return _FakeCompletedProcess(self._returncode, stderr=self._stderr)
        stdout = self._variables if "--variables" in args else self._config
        return _FakeCompletedProcess(0, stdout=stdout)

    def args_of(self, *, variables: bool) -> tuple[str, ...]:
        return next(args for args, _ in self.calls if ("--variables" in args) is variables)

    def kwargs_of(self, *, variables: bool) -> dict[str, Any]:
        return next(kwargs for args, kwargs in self.calls if ("--variables" in args) is variables)


def _snapshot(name: str, project_dir: Path | None = None) -> str:
    text = (FIXTURES / "compose_config" / f"{name}.json").read_text()
    return text if project_dir is None else text.replace(SNAPSHOT_ROOT, str(project_dir))


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub: _ComposeStub) -> _ComposeStub:
    monkeypatch.setattr("deploycli.compose_facts.subprocess.run", stub)
    return stub


def _scan_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    project_dir: Path,
    *,
    variables: str = "{}",
) -> ProjectFacts:
    _install_stub(
        monkeypatch,
        _ComposeStub(config=_snapshot(name, project_dir), variables=variables),
    )
    return scan_project(project_dir)


def _materialize_env_file(project_dir: Path) -> None:
    """Кладёт в каталог прогона файл окружения, объявленный снимком ``safe_read``."""
    shutil.copy(SAFE_READ_PROJECT / "app.env", project_dir / "app.env")


@pytest.fixture
def safe_read_facts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ProjectFacts:
    """Факты по снимку ``safe_read``: сервисы web (build), backend, cache (профиль).

    Снято с проекта: web собирается здесь, несёт метки СПИСКОМ строк,
    публикует 8080, сидит в default+proxy, держит именованный том и
    bind-монтирование, объявляет обязательный и необязательный
    ``env_file``; backend объявлен готовым образом с плейсхолдерами в
    реестре и теге и метками ОТОБРАЖЕНИЕМ; cache — готовый образ
    ``redis:7`` под профилем ``tools``.
    """
    _materialize_env_file(tmp_path)
    return _scan_snapshot(
        monkeypatch,
        "safe_read",
        tmp_path,
        variables=_snapshot("safe_read_variables"),
    )


def _service(facts: ProjectFacts, name: str) -> ServiceFacts:
    return next(service for service in facts.services if service.name == name)


# --- режим чтения -----------------------------------------------------------


def test_project_is_read_by_safe_mode_with_all_profiles_expanded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ADR-011: единственный режим чтения — безопасный, и профили в нём
    # раскрыты. Значения окружения не должны попадать в факты ни при каком
    # прогоне, а это свойство команды, а не выбора полей в коде: поэтому
    # тест проверяет именно аргументы и окружение запуска.
    stub = _install_stub(monkeypatch, _ComposeStub(config=_snapshot("basic")))

    scan_project(tmp_path)

    model_args = stub.args_of(variables=False)
    assert model_args == (
        "docker",
        "compose",
        "config",
        "--no-interpolate",
        "--no-env-resolution",
        "--format",
        "json",
    )
    assert stub.args_of(variables=True) == (
        "docker",
        "compose",
        "config",
        "--no-interpolate",
        "--no-env-resolution",
        "--variables",
        "--format",
        "json",
    )
    for variables in (False, True):
        kwargs = stub.kwargs_of(variables=variables)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"]["COMPOSE_PROFILES"] == "*"


def test_safe_mode_environment_keeps_the_rest_of_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # COMPOSE_PROFILES добавляется к окружению прогона, а не заменяет его:
    # docker без PATH и DOCKER_HOST не найдёт ни себя, ни демона.
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    stub = _install_stub(monkeypatch, _ComposeStub(config=_snapshot("basic")))

    scan_project(tmp_path)

    env = stub.kwargs_of(variables=False)["env"]
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert os.environ.get("COMPOSE_PROFILES") != "*"


# --- метки ------------------------------------------------------------------


def test_reads_traefik_labels_from_list_form(safe_read_facts: ProjectFacts) -> None:
    # Безопасный режим отдаёт метки в той форме, в какой их написал автор:
    # у web это СПИСОК строк "ключ=значение". Наивная итерация по такому
    # списку возвращает строки целиком и проходит тихо (ADR-011), поэтому
    # тест на списочную форму обязателен.
    web = _service(safe_read_facts, "web")

    assert dict(web.traefik_labels) == {
        "traefik.enable": "true",
        "traefik.http.routers.web.rule": "Host(`${WEB_DOMAIN}`)",
    }
    assert "com.example.owner" not in web.traefik_labels


def test_reads_traefik_labels_from_mapping_form(safe_read_facts: ProjectFacts) -> None:
    backend = _service(safe_read_facts, "backend")

    assert dict(backend.traefik_labels) == {"traefik.enable": "false"}
    assert "com.example.owner" not in backend.traefik_labels


# Снято с проекта: единственный сервис legacy несёт метки списком, причём
# traefik.enable записан голой формой — без знака равенства.
def test_label_without_value_keeps_key_with_empty_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Голая форма `- traefik.enable` значения в файле не несёт: его
    # подставляет compose из окружения автора, которого безопасное чтение
    # не видит. Ключ обязан дойти до фактов — на ключе стоит правило
    # отказа 3 (ADR-003), — а значения у такой метки нет.
    facts = _scan_snapshot(monkeypatch, "bare_label", tmp_path)

    legacy = _service(facts, "legacy")
    assert dict(legacy.traefik_labels) == {
        "traefik.enable": "",
        "traefik.http.routers.legacy.rule": "Host(`legacy.example.com`)",
    }


# Снято с проекта: единственный сервис legacy несёт метку Traefik в
# смешанном/верхнем регистре (Traefik читает метки регистронезависимо —
# ADR-003), плюс постороннюю метку com.example.owner.
def test_reads_traefik_labels_regardless_of_key_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    facts = _scan_snapshot(monkeypatch, "mixed_case_traefik_label", tmp_path)

    legacy = _service(facts, "legacy")
    # Ключи хранятся дословно, как их написал автор compose: сообщение об
    # отказе генерации должно называть метку ровно так, иначе её не найти
    # поиском по файлу.
    assert dict(legacy.traefik_labels) == {
        "Traefik.enable": "true",
        "TRAEFIK.HTTP.ROUTERS.LEGACY.RULE": "Host(`legacy.example.com`)",
    }
    assert "com.example.owner" not in legacy.traefik_labels


# --- три формы образа -------------------------------------------------------


def test_service_with_build_is_marked_as_built_here(safe_read_facts: ProjectFacts) -> None:
    web = _service(safe_read_facts, "web")

    assert web.builds_here is True
    assert web.image is None
    assert web.image_tag_has_placeholder is False


def test_service_with_literal_image_carries_neither_sign(safe_read_facts: ProjectFacts) -> None:
    cache = _service(safe_read_facts, "cache")

    assert cache.image == "redis:7"
    assert cache.builds_here is False
    assert cache.image_tag_has_placeholder is False


def test_service_without_build_and_with_placeholder_tag_is_marked(
    safe_read_facts: ProjectFacts,
) -> None:
    # Третья форма образа (ADR-011): своё, собираемое своим пайплайном, но
    # без build: в базовом compose. Сама она не классифицируется — признак
    # обязан дойти до анкеты (dpc-tm3.51).
    backend = _service(safe_read_facts, "backend")

    assert backend.builds_here is False
    assert backend.image_tag_has_placeholder is True


# Снято с проекта: шесть сервисов, по одному на каждую форму ссылки на образ.
@pytest.mark.parametrize(
    ("service_name", "has_placeholder"),
    [
        ("registry-placeholder", False),
        ("tag-placeholder", True),
        ("required-tag-placeholder", True),
        ("digest", False),
        ("no-tag", False),
        ("registry-with-port", False),
    ],
)
def test_placeholder_is_looked_for_in_the_tag_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service_name: str, has_placeholder: bool
) -> None:
    # Плейсхолдер в адресе реестра (зеркало) тегу не признак: образ всё
    # равно прибит к версии. Двоеточие живёт и внутри плейсхолдера
    # (${APP_TAG:?...}), и в адресе реестра с портом (reg:5000/app) —
    # разбор обязан не спутать их с отделителем тега.
    facts = _scan_snapshot(monkeypatch, "image_forms", tmp_path)

    service = _service(facts, service_name)
    assert service.image_tag_has_placeholder is has_placeholder


# --- порты ------------------------------------------------------------------


def test_reads_published_ports(safe_read_facts: ProjectFacts) -> None:
    web = _service(safe_read_facts, "web")

    assert web.published_ports == (8080,)
    assert web.publishes_edge_port is False


# Снято с проекта: web (nginx:1.27) публикует 80 и 443 напрямую, сидит в
# networks default+proxy, несёт метки отображением; worker и cache — без
# портов.
def test_reads_edge_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    facts = _scan_snapshot(monkeypatch, "basic", tmp_path)

    web = _service(facts, "web")
    assert web.published_ports == (80, 443)
    assert web.publishes_edge_port is True
    assert _service(facts, "worker").published_ports == ()
    assert _service(facts, "cache").publishes_edge_port is False


# Снято с проекта: единственный сервис app записал порты длинной формой с
# диапазоном в published ("8000-8002" при target 8000).
def test_reads_published_range_written_in_long_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Короткую форму диапазона docker разворачивает сам, а в длинной
    # оставляет диапазон строкой — в обоих режимах, проверено замером
    # Compose 5.1.1. Это законная запись, а не нерасшифрованная: сервис
    # занимает все порты диапазона, и все они обязаны попасть в факты.
    facts = _scan_snapshot(monkeypatch, "long_form_port_range", tmp_path)

    assert _service(facts, "app").published_ports == (8000, 8001, 8002)


# Снято с проекта: единственный сервис app публикует 3000 хостовым портом
# 8080 (80/443 наружу не заняты никем).
def test_no_edge_port_when_published_ports_are_not_80_or_443(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    facts = _scan_snapshot(monkeypatch, "no_edge_port", tmp_path)

    app = _service(facts, "app")
    assert app.published_ports == (8080,)
    assert app.publishes_edge_port is False


# Снято с проекта: web записал порт короткой формой с плейсхолдером
# ("${DEV_PORT}:3000") — тогда docker оставляет ВЕСЬ список портов этого
# сервиса строками, включая соседний литеральный "8080:80"; api записал
# порт длинной формой, и плейсхолдер остался в одном поле published.
def test_placeholder_in_published_port_refuses_naming_service_and_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ADR-011: плейсхолдер в опубликованном порте останавливает прогон.
    # Значения переменной при безопасном чтении нет, а угадывать порт,
    # который поедет на прод, нечем.
    _install_stub(monkeypatch, _ComposeStub(config=_snapshot("placeholder_port", tmp_path)))

    with pytest.raises(ScanRefused) as excinfo:
        scan_project(tmp_path)

    messages = excinfo.value.messages
    assert len(messages) == 2
    api_message, web_message = messages
    assert "'api'" in api_message
    assert "ports" in api_message
    assert "${API_PORT}" in api_message
    assert "'web'" in web_message
    assert "${DEV_PORT}:3000" in web_message


# Снято с проекта: единственный сервис web записал порт "abc:80" —
# docker такую запись не разобрал и оставил строкой, хотя плейсхолдера в
# ней нет.
def test_port_entry_docker_left_unparsed_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_stub(monkeypatch, _ComposeStub(config=_snapshot("unparsed_port", tmp_path)))

    with pytest.raises(ScanRefused) as excinfo:
        scan_project(tmp_path)

    (message,) = excinfo.value.messages
    assert "'web'" in message
    assert "abc:80" in message


# --- профили, сети, тома, имя стека ----------------------------------------


def test_reads_profiles_and_their_services(safe_read_facts: ProjectFacts) -> None:
    # ADR-011: профили раскрываются, а не обходятся — профильный сервис
    # проходит анкету наравне с остальными, поэтому он обязан быть и в
    # списке сервисов, и в профиле.
    assert [service.name for service in safe_read_facts.services] == ["backend", "cache", "web"]
    assert [(profile.name, profile.services) for profile in safe_read_facts.profiles] == [
        ("tools", ("cache",))
    ]
    assert _service(safe_read_facts, "cache").profiles == ("tools",)
    assert _service(safe_read_facts, "web").profiles == ()


def test_reads_networks_with_external_sign(safe_read_facts: ProjectFacts) -> None:
    assert [(network.name, network.external) for network in safe_read_facts.networks] == [
        ("default", False),
        ("proxy", True),
    ]
    assert _service(safe_read_facts, "web").networks == ("default", "proxy")


def test_reads_named_volumes_and_bind_mounts(safe_read_facts: ProjectFacts, tmp_path: Path) -> None:
    web = _service(safe_read_facts, "web")

    assert web.named_volumes == ("dbdata",)
    assert web.bind_mounts == (str(tmp_path / "landing"),)
    assert _service(safe_read_facts, "cache").named_volumes == ()


def test_reads_stack_name(safe_read_facts: ProjectFacts) -> None:
    assert safe_read_facts.name == "safe-read-fixture"


# --- переменные -------------------------------------------------------------


def test_reads_variable_names_with_required_sign(safe_read_facts: ProjectFacts) -> None:
    assert [(variable.name, variable.required) for variable in safe_read_facts.variables] == [
        ("BACKEND_TAG", True),
        ("IMAGE_REGISTRY", False),
        ("WEB_DOMAIN", False),
    ]


def test_reads_variable_names_from_env_file_itself(safe_read_facts: ProjectFacts) -> None:
    # Безопасный режим оставляет env_file путём, поэтому имена оттуда
    # читает сам инструмент: ключ до первого знака равенства, остаток
    # строки отброшен (ADR-011). Формы взяты из поведения самого compose:
    # `export NAME=1` даёт NAME, строка без знака равенства — тоже имя.
    assert safe_read_facts.env_file_variables == (
        "SENTINEL_VALUE",
        "APP_MODE",
        "EXPORTED_NAME",
        "BARE_NAME",
        "WITH_EQUALS",
    )


def test_values_from_env_file_never_reach_facts(safe_read_facts: ProjectFacts) -> None:
    assert "do-not-leak-me" not in repr(safe_read_facts)
    assert "a=b=c" not in repr(safe_read_facts)


def test_missing_optional_env_file_is_not_a_refusal(
    safe_read_facts: ProjectFacts, tmp_path: Path
) -> None:
    # optional.env снимок объявляет необязательным, и на машине его нет:
    # это штатная форма compose, а не отказ — факты прочитаны целиком, а
    # имён отсутствующий файл просто не дал.
    assert not (tmp_path / "optional.env").exists()
    assert [service.name for service in safe_read_facts.services] == ["backend", "cache", "web"]


# Снято с проекта: единственный сервис app объявил обязательный env_file,
# которого на машине нет, и рядом необязательный — тоже отсутствующий.
def test_missing_required_env_file_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_stub(monkeypatch, _ComposeStub(config=_snapshot("missing_env_file", tmp_path)))

    with pytest.raises(ScanRefused) as excinfo:
        scan_project(tmp_path)

    (message,) = excinfo.value.messages
    assert "'app'" in message
    assert str(tmp_path / "absent.env") in message
    assert "optional-absent.env" not in message


# Снято с проекта: web держит плейсхолдер в опубликованном порте, app —
# обязательный env_file, которого нет.
def test_all_refusals_are_collected_in_one_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Человек должен узнать полный список нарушений за один прогон, а не
    # чинить их по одному (dpc-tm3.25).
    _install_stub(monkeypatch, _ComposeStub(config=_snapshot("refusals_together", tmp_path)))

    with pytest.raises(ScanRefused) as excinfo:
        scan_project(tmp_path)

    messages = excinfo.value.messages
    assert len(messages) == 2
    assert any("${DEV_PORT}:3000" in message for message in messages)
    assert any("absent.env" in message for message in messages)


# --- падения docker ---------------------------------------------------------


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
    _install_stub(monkeypatch, _ComposeStub(returncode=1, stderr=stderr_text))

    with pytest.raises(ComposeScanError) as excinfo:
        scan_project(tmp_path)

    assert str(excinfo.value) == stderr_text


# --- настоящий docker -------------------------------------------------------


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
def test_project_with_required_variables_is_read_without_their_values() -> None:
    # Проект, объявивший обязательную переменную в теге образа (так
    # устроен ai-i-direct), безопасным режимом читается без единого
    # значения на машине. Обычный режим на нём падает — это и есть довод
    # ADR-011, поэтому он проверяется настоящим docker, а не пересказом.
    project_dir = SAFE_READ_PROJECT

    facts = scan_project(project_dir)

    backend = _service(facts, "backend")
    assert backend.image_tag_has_placeholder is True
    assert ("BACKEND_TAG", True) in [(v.name, v.required) for v in facts.variables]

    interpolated = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert interpolated.returncode != 0
    assert "BACKEND_TAG" in interpolated.stderr


@pytest.mark.docker
def test_values_of_env_file_never_reach_facts_read_by_real_docker() -> None:
    # Центральное обещание ADR-011: инструмент не способен напечатать
    # значение — он его не читает. Обычный режим на том же проекте
    # подставляет содержимое env_file в environment целиком.
    facts = scan_project(SAFE_READ_PROJECT)

    assert "SENTINEL_VALUE" in facts.env_file_variables
    assert "do-not-leak-me" not in repr(facts)


@pytest.mark.docker
def test_profiled_service_is_visible_to_real_docker_read() -> None:
    # config без COMPOSE_PROFILES молча скрывает сервисы неактивных
    # профилей, поэтому скан обязан раскрывать профили сам (ADR-011).
    facts = scan_project(SAFE_READ_PROJECT)

    assert _service(facts, "cache").profiles == ("tools",)
    assert [(profile.name, profile.services) for profile in facts.profiles] == [
        ("tools", ("cache",))
    ]


@pytest.mark.docker
def test_placeholder_port_project_is_refused_by_real_docker_read() -> None:
    project_dir = FIXTURES / "docker_projects" / "placeholder_port"

    with pytest.raises(ScanRefused) as excinfo:
        scan_project(project_dir)

    assert len(excinfo.value.messages) == 2


@pytest.mark.docker
def test_missing_required_env_file_is_refused_by_real_docker_read() -> None:
    project_dir = FIXTURES / "docker_projects" / "missing_env_file"

    with pytest.raises(ScanRefused) as excinfo:
        scan_project(project_dir)

    (message,) = excinfo.value.messages
    assert str(project_dir / "absent.env") in message


@pytest.mark.docker
def test_broken_compose_raises_with_verbatim_compose_stderr() -> None:
    # Фикстура битая синтаксисом YAML, а не ссылкой на несуществующую
    # сеть: безопасный режим проверку связности модели не делает
    # (замер Compose 5.1.1: `--no-interpolate` её отключает), поэтому
    # такой проект отказом сканирования не становится.
    project_dir = FIXTURES / "docker_projects" / "broken"

    expected = subprocess.run(
        [
            "docker",
            "compose",
            "config",
            "--no-interpolate",
            "--no-env-resolution",
            "--format",
            "json",
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env={**os.environ, "COMPOSE_PROFILES": "*"},
        check=False,
    )
    assert expected.returncode != 0, "фикстура должна быть невалидной"

    with pytest.raises(ComposeScanError) as excinfo:
        scan_project(project_dir)

    assert str(excinfo.value) == expected.stderr


@pytest.mark.docker
def test_snapshots_still_match_what_docker_returns(tmp_path: Path) -> None:
    # Фикстуры юнит-тестов — снимки настоящего вывода. Этот тест ловит
    # расхождение снимка с новой версией compose: иначе юнит-тесты
    # остались бы зелёными на форме, которой docker уже не отдаёт.
    project_dir = tmp_path / "safe_read"
    shutil.copytree(SAFE_READ_PROJECT, project_dir)

    fresh = subprocess.run(
        [
            "docker",
            "compose",
            "config",
            "--no-interpolate",
            "--no-env-resolution",
            "--format",
            "json",
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env={**os.environ, "COMPOSE_PROFILES": "*"},
        check=True,
    )

    assert json.loads(fresh.stdout.replace(str(project_dir), SNAPSHOT_ROOT)) == json.loads(
        _snapshot("safe_read")
    )
