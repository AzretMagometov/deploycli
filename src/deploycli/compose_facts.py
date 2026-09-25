"""Факты о подключённом проекте, полученные безопасным чтением модели.

deploycli не разбирает базовый compose проекта сам. Он поддерживает якоря,
merge-ключи, `extends`, `include`, профили и интерполяцию с формами `:-` и
`:?`, а короткие формы записи портов и healthcheck — источник расхождений
даже без всего перечисленного. Собственный разборщик обязан повторить всю
эту семантику и разойдётся с docker в первом же нестандартном проекте —
тогда сканер прочитает не тот проект, который потом поедет (ADR-004).

Читается проект одним режимом — безопасным (ADR-011)::

    COMPOSE_PROFILES='*' docker compose config \\
        --no-interpolate --no-env-resolution --format json

Модель в нём отдаёт только имена: `${VAR}`, `${VAR:-умолчание}` и
`${VAR:?}` остаются текстом, `env_file` остаётся путём, профильные сервисы
видны наравне с остальными. Тем же режимом с ключом ``--variables``
снимаются имена переменных и признак обязательности. Второго режима
чтения нет: обычный подставляет значения автора с его машины и вовсе не
читает проект, объявивший обязательную переменную. Запрет держится на
ключах команды, а не на выборе полей в коде.

Плата за режим — три особенности вывода, каждая из которых ловится здесь:

* `labels` приходят в той форме, в какой их написал автор: отображением
  или СПИСКОМ строк ``"ключ=значение"``. Наивная итерация по списку
  возвращает строки целиком и проходит тихо.
* Запись порта, которую docker не разобрал (а плейсхолдер он разобрать не
  может), оставляет весь список портов сервиса строками. Опубликованный
  порт из такой записи неизвестен — это отказ, а не повод угадывать.
* Имена переменных из `env_file` читает сам инструмент: ключ до первого
  знака равенства, остаток строки отброшен.
"""

import json
import os
import re
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

_EDGE_PORTS = frozenset({80, 443})

# Ключи безопасного режима и окружение, раскрывающее профили (ADR-011).
_SAFE_MODE = ("--no-interpolate", "--no-env-resolution")
_ALL_PROFILES = {"COMPOSE_PROFILES": "*"}

# Подстановка переменной в значении: ${VAR}, ${VAR:-умолчание}, $VAR.
# `$$` — экранированный доллар, то есть литерал, а не подстановка, поэтому
# он в выражении назван отдельной ветвью и отсеивается при проверке.
_INTERPOLATION = re.compile(r"\$\$|\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}|\$[A-Za-z_][A-Za-z0-9_]*")

# Хостовые порты записи: число или диапазон "от-до".
_PORT_NUMBERS = re.compile(r"^(\d+)(?:-(\d+))?$")


class ComposeScanError(RuntimeError):
    """Docker недоступен или ``docker compose config`` завершилась с ошибкой.

    Текст исключения — дословный stderr команды (или дословный текст
    ошибки операционной системы, если docker не найден в PATH), без
    попытки разобрать или переформулировать причину: пользователю нужно
    искать в интернете именно этот текст, а не его пересказ.
    """


class ScanRefused(Exception):
    """Проект прочитан, но факты из него не собрать: прогон остановлен.

    Несёт сообщения обо ВСЕХ помехах разом — человек должен узнать полный
    список за один прогон, а не чинить их по одной. Помех две, обе
    названы ADR-011: плейсхолдер там, где факт обязан быть литералом, и
    объявленный обязательным `env_file`, которого на машине нет.
    """

    def __init__(self, messages: tuple[str, ...]) -> None:
        super().__init__("\n".join(messages))
        self.messages = messages


@dataclass(frozen=True, slots=True)
class NetworkFacts:
    """Сеть проекта и признак того, что она заведена снаружи."""

    name: str
    external: bool


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    """Профиль проекта и сервисы, которые он включает."""

    name: str
    services: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VariableFacts:
    """Имя переменной интерполяции и признак её обязательности.

    Обязательна переменная, записанная формой ``${VAR:?}`` — без значения
    проект не запускается. Значения здесь нет и быть не может: безопасное
    чтение их не видит.
    """

    name: str
    required: bool


@dataclass(frozen=True, slots=True)
class ServiceFacts:
    """Факты об одном сервисе подключённого проекта.

    ``builds_here`` и ``image_tag_has_placeholder`` вместе различают три
    формы объявления образа (ADR-011): сборка здесь; готовый образ с
    литеральным тегом; образ без ``build:``, но с плейсхолдером в теге —
    как правило, свой код, который уже собирает свой пайплайн. Третья
    форма сама не классифицируется и обязана попасть в анкету.
    """

    name: str
    image: str | None
    networks: tuple[str, ...]
    published_ports: tuple[int, ...]
    traefik_labels: Mapping[str, str]
    builds_here: bool = False
    image_tag_has_placeholder: bool = False
    profiles: tuple[str, ...] = ()
    named_volumes: tuple[str, ...] = ()
    bind_mounts: tuple[str, ...] = ()

    @property
    def publishes_edge_port(self) -> bool:
        """Сервис публикует наружу порт 80 или 443 — то же, что занимает край хоста."""
        return any(port in _EDGE_PORTS for port in self.published_ports)


@dataclass(frozen=True, slots=True)
class ProjectFacts:
    """Факты о подключённом проекте целиком.

    ``variables`` — имена переменных интерполяции с признаком
    обязательности, ``env_file_variables`` — имена, объявленные в самих
    файлах окружения. Источники разные, поэтому и поля разные: у второго
    источника признака обязательности нет, и выдумывать его нечем.
    """

    name: str
    services: tuple[ServiceFacts, ...] = ()
    networks: tuple[NetworkFacts, ...] = ()
    profiles: tuple[ProfileFacts, ...] = ()
    variables: tuple[VariableFacts, ...] = ()
    env_file_variables: tuple[str, ...] = ()


def scan_project(project_dir: Path) -> ProjectFacts:
    """Прочитать факты о проекте в ``project_dir`` безопасным режимом.

    Поднимает :class:`ComposeScanError`, если docker не найден в PATH или
    команда завершилась ненулевым кодом возврата, и :class:`ScanRefused`,
    если модель прочитана, но факт в ней подменён плейсхолдером или
    обязательный файл окружения отсутствует.
    """
    config = _read_json(project_dir, "--format", "json")
    variables = _read_json(project_dir, "--variables", "--format", "json")

    services = _services_of(config)
    refusals = (*_unreadable_port_refusals(services), *_env_file_refusals(services))
    if refusals:
        raise ScanRefused(refusals)

    return facts_from_config(
        config,
        variables=variables,
        env_file_variables=_env_file_variables(services),
    )


def facts_from_config(
    config: Mapping[str, Any],
    *,
    variables: Mapping[str, Any] | None = None,
    env_file_variables: Sequence[str] = (),
) -> ProjectFacts:
    """Собрать факты из уже прочитанной модели проекта.

    Отделено от :func:`scan_project`, потому что разбор модели не зависит
    ни от запуска docker, ни от файлов на диске: так его проверяют
    снимками вывода, а проверка отрендеренных меток (ADR-003) собирает
    факты из собственного прогона ``docker compose config``.
    """
    services = _services_of(config)
    refusals = _unreadable_port_refusals(services)
    if refusals:
        raise ScanRefused(refusals)

    return ProjectFacts(
        # Имя стека docker называет всегда — из `name:` базового compose
        # или из имени каталога, — поэтому пустым оно остаётся только у
        # модели, собранной не запуском docker.
        name=str(config.get("name", "")),
        services=tuple(_service_facts(name, definition) for name, definition in services.items()),
        networks=_network_facts(config),
        profiles=_profile_facts(services),
        variables=_variable_facts(variables or {}),
        env_file_variables=tuple(env_file_variables),
    )


def _read_json(project_dir: Path, *arguments: str) -> Mapping[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "compose", "config", *_SAFE_MODE, *arguments],
            cwd=project_dir,
            capture_output=True,
            text=True,
            env={**os.environ, **_ALL_PROFILES},
            check=False,
        )
    except OSError as exc:
        raise ComposeScanError(str(exc)) from exc

    if result.returncode != 0:
        raise ComposeScanError(result.stderr)

    payload: Mapping[str, Any] = json.loads(result.stdout)
    return payload


def _services_of(config: Mapping[str, Any]) -> Mapping[str, Any]:
    services: Mapping[str, Any] = config.get("services") or {}
    return services


def _service_facts(name: str, definition: Mapping[str, Any]) -> ServiceFacts:
    image = definition.get("image")
    return ServiceFacts(
        name=name,
        image=image,
        networks=tuple(definition.get("networks") or {}),
        published_ports=_published_ports(definition),
        traefik_labels=_traefik_labels(definition),
        builds_here="build" in definition,
        image_tag_has_placeholder=image is not None and _has_placeholder(_image_tag(image)),
        profiles=tuple(definition.get("profiles") or ()),
        named_volumes=_mount_sources(definition, "volume"),
        bind_mounts=_mount_sources(definition, "bind"),
    )


def _traefik_labels(definition: Mapping[str, Any]) -> Mapping[str, str]:
    # Traefik читает метки регистронезависимо (ADR-003), поэтому фильтр по
    # префиксу обязан игнорировать регистр — иначе метка вида
    # `Traefik.enable` в базовом compose проходит мимо фактов и мимо
    # правила отказа 3, оставаясь незамеченной. Сам ключ в факты попадает
    # дословно, как его написал автор compose (а не приведённым к нижнему
    # регистру): сообщение об отказе называет метку ровно так, как она
    # выглядит у пользователя, чтобы её можно было найти поиском по файлу.
    return MappingProxyType(
        {
            key: value
            for key, value in _label_pairs(definition.get("labels"))
            if key.lower().startswith("traefik.")
        }
    )


def _label_pairs(labels: Any) -> Iterator[tuple[str, str]]:
    if labels is None:
        return
    if isinstance(labels, Mapping):
        yield from ((str(key), str(value)) for key, value in labels.items())
        return
    for entry in labels:
        # Списочная форма: "ключ=значение". Голая форма `- ключ` значения
        # в файле не несёт — его подставляет compose из окружения автора,
        # которого безопасное чтение не видит. Ключ сохраняется (на нём
        # стоит правило отказа 3), значения у такой метки нет.
        key, _, value = str(entry).partition("=")
        yield key, value


def _published_ports(definition: Mapping[str, Any]) -> tuple[int, ...]:
    # `docker compose config` разворачивает короткую форму записи портов,
    # включая диапазоны ("8000-8010:8000-8010"), в отдельные записи с
    # единственным числом в `published`; сервис без публикации порта
    # наружу (`- "80"`, только target) вовсе не несёт ключ `published`.
    # Записи, которые docker оставил нерасшифрованными, сюда не доходят:
    # они отсеяны отказом, поэтому пустой разбор здесь невозможен.
    ports: set[int] = set()
    for entry in definition.get("ports") or ():
        if "published" in entry:
            ports.update(_published_numbers(str(entry["published"])) or ())
    return tuple(sorted(ports))


def _published_numbers(published: str) -> range | None:
    """Хостовые порты записи: одно число, диапазон ``от-до`` или ничего.

    Диапазон в длинной форме записи (``published: "8000-8002"``) docker
    не разворачивает ни в одном режиме — проверено замером Compose
    5.1.1, — но это законная запись, а не нерасшифрованная: сервис
    занимает все порты диапазона. ``None`` означает, что прочитать
    запись нечем.
    """
    match = _PORT_NUMBERS.match(published)
    if match is None:
        return None
    first, last = match.group(1), match.group(2)
    return range(int(first), int(last or first) + 1)


def _unreadable_ports(definition: Mapping[str, Any]) -> tuple[str, ...]:
    """Записи портов сервиса, из которых опубликованный порт не прочитать."""
    unreadable = []
    for entry in definition.get("ports") or ():
        if isinstance(entry, str):
            # Одна неразобранная запись оставляет строками ВЕСЬ список
            # портов сервиса, включая соседние литеральные, — проверено
            # замером Compose 5.1.1.
            unreadable.append(entry)
        elif "published" in entry and _published_numbers(str(entry["published"])) is None:
            unreadable.append(str(entry["published"]))
    return tuple(unreadable)


def _unreadable_port_refusals(services: Mapping[str, Any]) -> tuple[str, ...]:
    messages = []
    for name, definition in sorted(services.items()):
        entries = _unreadable_ports(definition)
        if not entries:
            continue
        listed = ", ".join(f"'{entry}'" for entry in entries)
        if any(_has_placeholder(entry) for entry in entries):
            messages.append(
                f"Сервис '{name}' в базовом compose проекта публикует порт через "
                f"плейсхолдер (ports: {listed}): безопасное чтение проекта значений "
                "переменных не видит, поэтому опубликованный порт неизвестен, а "
                f"угадывать его нечем. Задайте у сервиса '{name}' опубликованные "
                "порты литералами в базовом compose проекта."
            )
        else:
            messages.append(
                f"Сервис '{name}' в базовом compose проекта записал порт, который "
                f"docker не разобрал (ports: {listed}): опубликованный порт из такой "
                f"записи неизвестен. Исправьте запись порта у сервиса '{name}' в "
                "базовом compose проекта."
            )
    return tuple(messages)


def _env_files(definition: Mapping[str, Any]) -> tuple[tuple[str, bool], ...]:
    """Объявленные файлы окружения сервиса: путь и признак обязательности."""
    declared = []
    for entry in definition.get("env_file") or ():
        if isinstance(entry, str):
            declared.append((entry, True))
        else:
            declared.append((str(entry["path"]), bool(entry.get("required", True))))
    return tuple(declared)


def _env_file_refusals(services: Mapping[str, Any]) -> tuple[str, ...]:
    messages = []
    for name, definition in sorted(services.items()):
        for path, required in _env_files(definition):
            if not required:
                continue
            if _has_placeholder(path):
                messages.append(
                    f"Сервис '{name}' в базовом compose проекта задал путь "
                    f"обязательного env_file плейсхолдером ({path}): безопасное "
                    "чтение проекта значений переменных не видит, поэтому файл не "
                    f"найти. Задайте у сервиса '{name}' путь env_file литералом в "
                    "базовом compose проекта."
                )
            elif not Path(path).is_file():
                messages.append(
                    f"Сервис '{name}' в базовом compose проекта объявил обязательный "
                    f"env_file, которого на машине нет ({path}): имена переменных "
                    "deploycli читает из самого файла, и отсутствующий файл имён не "
                    f"даёт. Создайте файл {path} или пометьте его в базовом compose "
                    "необязательным (required: false)."
                )
    return tuple(messages)


def _env_file_variables(services: Mapping[str, Any]) -> tuple[str, ...]:
    # Порядок — объявления: сначала сервисы в порядке модели, внутри
    # сервиса файлы в порядке записи, внутри файла строки сверху вниз.
    # Повтор имени в двух файлах не удваивает его в фактах.
    names: dict[str, None] = {}
    for definition in services.values():
        for path, _ in _env_files(definition):
            env_file = Path(path)
            if not env_file.is_file():
                continue
            for name in _variable_names_of(env_file):
                names[name] = None
    return tuple(names)


def _variable_names_of(env_file: Path) -> Iterator[str]:
    """Имена переменных из файла окружения: ключ до первого знака равенства.

    Единственное место, где deploycli разбирает файл окружения вместо
    docker (ADR-011): безопасное чтение оставляет `env_file` путём.
    Остаток строки отбрасывается, поэтому значение не доходит ни до
    фактов, ни до сгенерированного. Формы взяты у самого compose:
    ``export NAME=1`` объявляет NAME, строка без знака равенства — тоже
    имя (значение compose берёт из окружения), комментарии и пустые
    строки не объявляют ничего.
    """
    with env_file.open(encoding="utf-8") as lines:
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped.removeprefix("export ").strip()
            name = stripped.partition("=")[0].strip()
            if name:
                yield name


def _mount_sources(definition: Mapping[str, Any], mount_type: str) -> tuple[str, ...]:
    # Форму монтирований docker приводит к длинной сам (проверено замером
    # Compose 5.1.1: короткая форма с плейсхолдером в источнике тоже
    # приезжает отображением), поэтому здесь нет ветви на строку.
    # Анонимный том источника не имеет — его нечем назвать.
    return tuple(
        str(mount["source"])
        for mount in definition.get("volumes") or ()
        if mount.get("type") == mount_type and "source" in mount
    )


def _network_facts(config: Mapping[str, Any]) -> tuple[NetworkFacts, ...]:
    return tuple(
        NetworkFacts(name=name, external=bool((definition or {}).get("external")))
        for name, definition in (config.get("networks") or {}).items()
    )


def _profile_facts(services: Mapping[str, Any]) -> tuple[ProfileFacts, ...]:
    # Профили собираются из модели, прочитанной с COMPOSE_PROFILES='*':
    # в ней объявлены все сервисы, поэтому объединение их профилей полно.
    # Отдельный прогон `config --profiles` для того же списка отклонён: он
    # разбирает модель в типизированный вид и падает ровно на проектах с
    # плейсхолдером в типизированном поле — то есть там, где у нас уже
    # есть свой отказ с именем сервиса и поля.
    by_profile: dict[str, list[str]] = {}
    for name, definition in services.items():
        for profile in definition.get("profiles") or ():
            by_profile.setdefault(str(profile), []).append(name)
    return tuple(
        ProfileFacts(name=profile, services=tuple(sorted(names)))
        for profile, names in sorted(by_profile.items())
    )


def _variable_facts(variables: Mapping[str, Any]) -> tuple[VariableFacts, ...]:
    return tuple(
        VariableFacts(name=name, required=bool(definition.get("Required")))
        for name, definition in sorted(variables.items())
    )


def _has_placeholder(text: str) -> bool:
    return any(match.group() != "$$" for match in _INTERPOLATION.finditer(text))


def _image_tag(image: str) -> str:
    """Часть ссылки на образ, где живёт версия: тег или дайджест.

    Двоеточие встречается и в адресе реестра с портом
    (``reg.example.com:5000/app``), и внутри самой подстановки
    (``${APP_TAG:?сообщение}``), поэтому отделитель тега ищется по ссылке
    с замаскированными подстановками, а возвращается кусок исходной
    строки. Ссылка без тега даёт пустую строку — плейсхолдеру там взяться
    неоткуда.
    """
    masked = _INTERPOLATION.sub(lambda match: "x" * len(match.group()), image)
    digest = masked.rfind("@")
    if digest != -1:
        return image[digest + 1 :]
    tag = masked.rfind(":")
    return image[tag + 1 :] if tag > masked.rfind("/") else ""
