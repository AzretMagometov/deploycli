"""Файл ответов ``.deploycli.yml``: схема, чтение, запись и отказы на нём.

См. ADR-010, ADR-011, ADR-013 и ADR-015 в decisions.md. Файл ответов —
вход генератора, а не его выход: он хранит версию последнего успешного
прогона, ответы анкеты и запись на каждый написанный путь. Править его
руками законно — это штатный способ поменять ответ, не проходя опрос,
поэтому хеша самого себя файл не хранит и заморозке не подлежит.

Схема (все ответы необязательны, обязательна только версия — отсутствующий
ответ спрашивает анкета, а в неинтерактивном прогоне он становится её
отказом, не отказом чтения)::

    version: 0.1.0            # версия deploycli, написавшая файл
    forge: forgejo            # github или forgejo
    contours: [test, prod]    # порядок значим: тем же порядком идут
    auto_contour: test        #   варианты ручного запуска (ADR-013)
    profiles: [tools]         # профили, участвующие в деплое
    migration: {service: api, command: alembic upgrade head}
    backup: {service: db, command: pg_dump -U app app}
    services:
      api:
        public: {port: 8000, domain_variable: API_DOMAIN, path_prefix: /api}
        built: true
      db:
        public: false
        built: false
    paths:
      deploy/deploy.sh: {sha256: <64 знака 0-9a-f>, version: 0.1.0}

Три решения схемы, у которых были живые альтернативы:

1. **Запись сервиса целиком означает «о сервисе отвечено»**: ключи
   ``public`` и ``built`` внутри записи обязательны. Иначе отсутствие
   ``public`` означало бы сразу и «непубличный», и «ещё не спросили» —
   различить их было бы нечем. Чтобы ответы о сервисе спросили заново,
   запись сервиса удаляют целиком.
2. **``public`` — либо ``false``, либо сам маршрут**, а не флаг рядом с
   маршрутом: пара «флаг и маршрут» допускает противоречие (``false`` с
   портом) и потребовала бы правила отказа на него, а union делает
   противоречие непредставимым. Тот же довод, которым ADR-013 выбрал
   скаляр ``auto_contour`` вместо флага у каждого контура.
3. **Мигрирующий сервис и сервис резервной копии живут в корне файла**
   записью ``{service, command}``, а не флагом внутри записи сервиса:
   ADR-008 разрешает по одному на проект, и в корне это инвариант
   структуры, а не ещё одно правило отказа. Ответ при этом остаётся
   привязанным к сервису: исчез сервис — запись про него удаляет
   dpc-tm3.45 (она же сверяет имя сервиса с моделью проекта — здесь
   проекта нет, есть только файл).

Отказов на файле ответов четыре, и ни один не молчит: версия новее
установленной, версия ниже порога читаемости, отсутствующий ключ версии
(все три — ADR-015) и неизвестный ключ (ADR-010). Версия проверяется
первой и в одиночку: у файла чужой схемы нельзя осмысленно разобрать
ключи. Всё остальное проверяется разом, как и отказы генерации, — человек
должен узнать полный список за один прогон.

Что этот модуль намеренно НЕ проверяет: грамматику имени переменной
домена (dpc-tm3.50), грамматику имён контуров (dpc-tm3.60), существование
названных сервисов в проекте (dpc-tm3.45 — у чтения нет модели проекта) и
состояние файлов по записанным путям (dpc-tm3.42 и dpc-tm3.43).
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import version as _distribution_version
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final

import yaml
from packaging.version import InvalidVersion, Version

ANSWERS_FILE_NAME: Final = ".deploycli.yml"

OLDEST_READABLE_VERSION: Final = Version("0.1.0")
"""Порог читаемости: самая старая версия файла ответов, которую читает инструмент.

ADR-015: порог поднимают только вместе с минорной версией и отдельной
строкой в changelog. Второго счётчика схемы у файла ответов нет, кода
миграции старых файлов — тоже.
"""

_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}")

_HEADER: Final = (
    "# Файл ответов deploycli: вход генератора, а не его выход.\n"
    "# Править руками законно — это штатный способ поменять ответ.\n"
    "# Прогон переписывает файл целиком: комментарии и порядок ключей не сохраняются.\n"
)

_ROOT_KEYS: Final = frozenset(
    {
        "version",
        "forge",
        "contours",
        "auto_contour",
        "profiles",
        "migration",
        "backup",
        "services",
        "paths",
    }
)
_SERVICE_KEYS: Final = frozenset({"public", "built"})
_ROUTE_KEYS: Final = frozenset({"port", "domain_variable", "path_prefix"})
_COMMAND_KEYS: Final = frozenset({"service", "command"})
_PATH_KEYS: Final = frozenset({"sha256", "version"})

_ROOT: Final = f"в корне файла ответов {ANSWERS_FILE_NAME}"


class Forge(StrEnum):
    """Форжа подключённого проекта. В границах первой версии их две (ADR-001)."""

    GITHUB = "github"
    FORGEJO = "forgejo"


@dataclass(frozen=True, slots=True)
class PublicAnswer:
    """Маршрут публичного сервиса: внутренний порт, переменная домена, префикс пути.

    Домен здесь — ИМЯ переменной окружения, а не значение (ADR-011):
    значение живёт в файле окружения контура и в репозиторий не попадает.
    Пустой ``path_prefix`` означает отсутствие префикса.
    """

    port: int
    domain_variable: str
    path_prefix: str = ""


@dataclass(frozen=True, slots=True)
class ServiceAnswers:
    """Ответы об одном сервисе: публичность и участие в сборке пайплайном.

    ``public is None`` — сервис отвечен как непубличный, а не «не спрошен»:
    неспрошенного сервиса в файле ответов нет вовсе.
    """

    public: PublicAnswer | None
    built: bool


@dataclass(frozen=True, slots=True)
class ServiceCommand:
    """Сервис и команда: шаг миграции или снятие резервной копии (ADR-008)."""

    service: str
    command: str


@dataclass(frozen=True, slots=True)
class WrittenPath:
    """Запись написанного пути: хеш содержимого и версия, которой путь написан.

    Отдельного списка написанных путей нет — записи и есть этот список
    (ADR-010). ``digest`` — sha256 содержимого в нижнем регистре.
    """

    digest: str
    version: Version


@dataclass(frozen=True, slots=True)
class AnswersFile:
    """Содержимое файла ответов без версии, которой он написан.

    Версии в модели нет нарочно: записывает её всегда :func:`write_answers`
    из версии установленного инструмента, поэтому «файл несёт версию того
    прогона, который его написал» — свойство устройства, а не дисциплины
    вызывающего кода. Прочитанная версия участвует только в сравнении при
    чтении (:func:`check_version`); поштучное происхождение живёт в записях
    путей.
    """

    forge: Forge | None = None
    contours: tuple[str, ...] = ()
    auto_contour: str | None = None
    profiles: tuple[str, ...] = ()
    migration: ServiceCommand | None = None
    backup: ServiceCommand | None = None
    services: Mapping[str, ServiceAnswers] = field(default_factory=dict)
    paths: Mapping[str, WrittenPath] = field(default_factory=dict)


class AnswersFileRefused(Exception):
    """Файл ответов не читается: несёт сообщения обо всех сработавших отказах.

    Отказ по версии приходит один: у файла чужой схемы разбирать ключи
    нечем. Отказы по схеме собираются все сразу.
    """

    def __init__(self, messages: tuple[str, ...]) -> None:
        super().__init__("\n".join(messages))
        self.messages = messages


def installed_version() -> Version:
    """Версия установленного deploycli — единственный источник правды (ADR-015)."""
    return Version(_distribution_version("deploycli"))


def content_digest(content: bytes) -> str:
    """Хеш содержимого владеемого пути для записи в файл ответов."""
    return hashlib.sha256(content).hexdigest()


def check_version(recorded: Version, installed: Version) -> None:
    """Сверяет версию из файла с установленной по порядку PEP 440 (ADR-010, ADR-015).

    Записанная новее установленной — отказ: пути понижения нет. Ниже
    порога читаемости — отказ: схема с тех пор сменилась. Равная и более
    старая проходят, равная обязана дать тот же результат байт в байт.
    """
    if recorded > installed:
        raise AnswersFileRefused(
            (
                f"Файл ответов {ANSWERS_FILE_NAME} написан версией deploycli {recorded}, "
                f"а установлена {installed}: пути понижения нет. Поставьте deploycli "
                f"не ниже {recorded} и повторите прогон.",
            )
        )
    if recorded < OLDEST_READABLE_VERSION:
        raise AnswersFileRefused(
            (
                f"Файл ответов {ANSWERS_FILE_NAME} написан версией deploycli {recorded}, "
                f"а самая старая читаемая версия файла ответов — {OLDEST_READABLE_VERSION}: "
                "схема файла ответов с тех пор сменилась. Приведите файл к текущей схеме "
                "руками — кода миграции у инструмента нет.",
            )
        )


def read_answers(path: Path) -> AnswersFile:
    """Читает файл ответов, проверяя версию и схему.

    Поднимает :class:`AnswersFileRefused` на любом из отказов и
    ``FileNotFoundError``, если файла нет: отсутствие файла — не отказ, а
    первый прогон, и решает это вызывающий.
    """
    text = path.read_text(encoding="utf-8")
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise AnswersFileRefused(
            (f"Файл ответов {ANSWERS_FILE_NAME} не разбирается как YAML: {error}",)
        ) from error

    if payload is None:
        raise AnswersFileRefused(
            (f"Файл ответов {ANSWERS_FILE_NAME} пуст: в нём нет ни версии, ни ответов.",)
        )
    if not isinstance(payload, Mapping):
        raise AnswersFileRefused(
            (
                f"Файл ответов {ANSWERS_FILE_NAME} обязан быть отображением ключей, "
                f"а записан как {_type_name(payload)}.",
            )
        )

    check_version(_recorded_version(payload), installed_version())

    refusals: list[str] = []
    answers = _answers(payload, refusals)
    if refusals:
        raise AnswersFileRefused(tuple(refusals))
    return answers


def write_answers(path: Path, answers: AnswersFile) -> None:
    """Переписывает файл ответов целиком, помечая его установленной версией.

    Вывод детерминирован: порядок ключей задан схемой, сервисы и записи
    путей идут по возрастанию имени, порядок контуров и профилей взят из
    ответов (в нём же генератор собирает варианты ручного запуска).
    Комментарии и порядок ключей прошлого файла не сохраняются.
    """
    payload: dict[str, Any] = {"version": str(installed_version())}
    if answers.forge is not None:
        payload["forge"] = answers.forge.value
    if answers.contours:
        payload["contours"] = list(answers.contours)
    if answers.auto_contour is not None:
        payload["auto_contour"] = answers.auto_contour
    if answers.profiles:
        payload["profiles"] = list(answers.profiles)
    if answers.migration is not None:
        payload["migration"] = _command_payload(answers.migration)
    if answers.backup is not None:
        payload["backup"] = _command_payload(answers.backup)
    payload["services"] = {
        name: _service_payload(answers.services[name]) for name in sorted(answers.services)
    }
    payload["paths"] = {
        recorded: _path_payload(answers.paths[recorded]) for recorded in sorted(answers.paths)
    }

    body = yaml.dump(
        payload,
        Dumper=_IndentedDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=4096,
    )
    path.write_text(_HEADER + body, encoding="utf-8")


class _IndentedDumper(yaml.SafeDumper):
    """SafeDumper, который делает отступ элементам списка.

    PyYAML по умолчанию печатает ``- test`` вплотную к левому краю под
    ключом. Файл ответов читают и правят руками, поэтому отступ здесь —
    не украшение, а та же раскладка, в которой список видят в любом
    другом compose-подобном файле проекта.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _command_payload(command: ServiceCommand) -> dict[str, Any]:
    return {"service": command.service, "command": command.command}


def _service_payload(service: ServiceAnswers) -> dict[str, Any]:
    public: Any = False
    if service.public is not None:
        public = {"port": service.public.port, "domain_variable": service.public.domain_variable}
        if service.public.path_prefix:
            public["path_prefix"] = service.public.path_prefix
    return {"public": public, "built": service.built}


def _path_payload(record: WrittenPath) -> dict[str, Any]:
    return {"sha256": record.digest, "version": str(record.version)}


def _recorded_version(payload: Mapping[str, Any]) -> Version:
    raw = payload.get("version")
    if raw is None:
        raise AnswersFileRefused(
            (
                f"Файл ответов {ANSWERS_FILE_NAME} не называет версию: ключ version "
                "обязателен. Файл без версии не читается — подставить версию "
                "установленного инструмента молча нельзя. Верните ключ version со "
                "значением версии, которой файл написан.",
            )
        )
    if not isinstance(raw, str):
        raise AnswersFileRefused(
            (
                f"Ключ 'version' {_ROOT} обязан быть строкой, а записан как "
                f"{_type_name(raw)}: {raw!r}. YAML читает 1.10 как число — запишите "
                'версию в кавычках, например version: "1.10".',
            )
        )
    try:
        return Version(raw)
    except InvalidVersion as error:
        raise AnswersFileRefused(
            (
                f"Файл ответов {ANSWERS_FILE_NAME} называет версию '{raw}', которая не "
                "разбирается как версия по PEP 440: запишите версию вида 0.1.0.",
            )
        ) from error


def _answers(payload: Mapping[str, Any], refusals: list[str]) -> AnswersFile:
    # Порядок проверок — порядок ключей схемы: при нескольких нарушениях
    # сразу список отказов читается сверху вниз вместе с самим файлом.
    _unknown_keys(payload, _ROOT_KEYS, _ROOT, refusals)
    forge = _forge(payload, refusals)
    contours = _string_list(payload, "contours", _ROOT, refusals)
    auto_contour = _auto_contour(payload, contours, refusals)
    profiles = _string_list(payload, "profiles", _ROOT, refusals)
    migration = _command(payload, "migration", refusals)
    backup = _command(payload, "backup", refusals)
    services = _services(payload, refusals)
    paths = _paths(payload, refusals)
    return AnswersFile(
        forge=forge,
        contours=contours,
        auto_contour=auto_contour,
        profiles=profiles,
        migration=migration,
        backup=backup,
        services=services,
        paths=paths,
    )


def _forge(payload: Mapping[str, Any], refusals: list[str]) -> Forge | None:
    raw = _string(payload, "forge", _ROOT, refusals)
    if raw is None:
        return None
    try:
        return Forge(raw)
    except ValueError:
        known = ", ".join(sorted(forge.value for forge in Forge))
        refusals.append(
            f"Ключ 'forge' {_ROOT} называет форжу '{raw}', которой инструмент не "
            f"знает: в границах первой версии их две — {known}."
        )
        return None


def _auto_contour(
    payload: Mapping[str, Any], contours: tuple[str, ...], refusals: list[str]
) -> str | None:
    name = _string(payload, "auto_contour", _ROOT, refusals)
    if name is None:
        return None
    if name not in contours:
        listed = ", ".join(contours) if contours else "список пуст"
        refusals.append(
            f"Ключ 'auto_contour' {_ROOT} называет контур '{name}', которого нет в "
            f"списке контуров ({listed}): исправьте имя контура или добавьте "
            f"'{name}' в contours."
        )
        return None
    return name


def _command(payload: Mapping[str, Any], key: str, refusals: list[str]) -> ServiceCommand | None:
    raw = _mapping(payload, key, _ROOT, refusals)
    if raw is None:
        return None

    where = f"в записи {key} файла ответов {ANSWERS_FILE_NAME}"
    _unknown_keys(raw, _COMMAND_KEYS, where, refusals)
    service = _string(raw, "service", where, refusals)
    if raw.get("service") is None:
        refusals.append(
            f"Запись {key} в файле ответов {ANSWERS_FILE_NAME} не называет сервис: "
            "ключ service обязателен."
        )
    command = _string(raw, "command", where, refusals)
    if raw.get("command") is None:
        refusals.append(
            f"Запись {key} в файле ответов {ANSWERS_FILE_NAME} не называет команду: "
            "ключ command обязателен."
        )
    if service is None or command is None:
        return None
    return ServiceCommand(service=service, command=command)


def _services(payload: Mapping[str, Any], refusals: list[str]) -> Mapping[str, ServiceAnswers]:
    raw = _mapping(payload, "services", _ROOT, refusals)
    if raw is None:
        return MappingProxyType({})

    services: dict[str, ServiceAnswers] = {}
    for key, entry in sorted(raw.items(), key=lambda item: str(item[0])):
        name = str(key)
        if not isinstance(entry, Mapping):
            refusals.append(
                f"Запись сервиса '{name}' в файле ответов {ANSWERS_FILE_NAME} обязана "
                f"быть отображением с ключами public и built, а записана как "
                f"{_type_name(entry)}: {entry!r}."
            )
            continue
        broken = len(refusals)
        _unknown_keys(entry, _SERVICE_KEYS, _service_where(name), refusals)
        public = _public(entry, name, refusals)
        built = _built(entry, name, refusals)
        if len(refusals) == broken and built is not None:
            services[name] = ServiceAnswers(public=public, built=built)
    return MappingProxyType(services)


def _public(entry: Mapping[str, Any], name: str, refusals: list[str]) -> PublicAnswer | None:
    raw = entry.get("public")
    if raw is None:
        refusals.append(
            f"Запись сервиса '{name}' в файле ответов {ANSWERS_FILE_NAME} не называет "
            "публичность: ключ public обязателен — false у непубличного сервиса или "
            "маршрут с ключами port и domain_variable. Чтобы спросить заново, удалите "
            "запись сервиса целиком."
        )
        return None
    if raw is False:
        return None
    if raw is True:
        refusals.append(
            f"Публичность сервиса '{name}' в файле ответов {ANSWERS_FILE_NAME} задана "
            "как true без маршрута: у публичного сервиса обязаны быть внутренний порт "
            "и переменная домена. Запишите маршрут ключами port и domain_variable или "
            "поставьте false."
        )
        return None
    if not isinstance(raw, Mapping):
        refusals.append(
            f"Ключ 'public' {_service_where(name)} обязан быть false или маршрутом с "
            f"ключами port и domain_variable, а записан как {_type_name(raw)}: {raw!r}."
        )
        return None

    where = f"в маршруте сервиса '{name}' файла ответов {ANSWERS_FILE_NAME}"
    _unknown_keys(raw, _ROUTE_KEYS, where, refusals)
    port = _integer(raw, "port", where, refusals)
    if raw.get("port") is None:
        refusals.append(
            f"Маршрут сервиса '{name}' в файле ответов {ANSWERS_FILE_NAME} не называет "
            "внутренний порт: ключ port обязателен."
        )
    domain_variable = _string(raw, "domain_variable", where, refusals)
    if raw.get("domain_variable") is None:
        refusals.append(
            f"Маршрут сервиса '{name}' в файле ответов {ANSWERS_FILE_NAME} не называет "
            "переменную домена: ключ domain_variable обязателен."
        )
    path_prefix = _string(raw, "path_prefix", where, refusals)
    if port is None or domain_variable is None:
        return None
    return PublicAnswer(port=port, domain_variable=domain_variable, path_prefix=path_prefix or "")


def _built(entry: Mapping[str, Any], name: str, refusals: list[str]) -> bool | None:
    if entry.get("built") is None:
        refusals.append(
            f"Запись сервиса '{name}' в файле ответов {ANSWERS_FILE_NAME} не называет, "
            "собирает ли этот сервис пайплайн: ключ built обязателен, true или false. "
            "Чтобы спросить заново, удалите запись сервиса целиком."
        )
        return None
    return _boolean(entry, "built", _service_where(name), refusals)


def _paths(payload: Mapping[str, Any], refusals: list[str]) -> Mapping[str, WrittenPath]:
    raw = _mapping(payload, "paths", _ROOT, refusals)
    if raw is None:
        return MappingProxyType({})

    records: dict[str, WrittenPath] = {}
    for key, entry in sorted(raw.items(), key=lambda item: str(item[0])):
        recorded = str(key)
        if not _inside_project(recorded):
            refusals.append(
                f"Запись пути '{recorded}' в файле ответов {ANSWERS_FILE_NAME} не "
                "является относительным путём внутри проекта: по этим путям инструмент "
                "перезаписывает и удаляет файлы, поэтому абсолютный путь и '..' в "
                "записи — отказ."
            )
            continue
        where = f"в записи пути '{recorded}' файла ответов {ANSWERS_FILE_NAME}"
        if not isinstance(entry, Mapping):
            refusals.append(
                f"Запись пути '{recorded}' в файле ответов {ANSWERS_FILE_NAME} обязана "
                f"быть отображением с ключами sha256 и version, а записана как "
                f"{_type_name(entry)}: {entry!r}."
            )
            continue
        _unknown_keys(entry, _PATH_KEYS, where, refusals)
        digest = _digest(entry, recorded, where, refusals)
        version = _path_version(entry, recorded, where, refusals)
        if digest is not None and version is not None:
            records[recorded] = WrittenPath(digest=digest, version=version)
    return MappingProxyType(records)


def _inside_project(recorded: str) -> bool:
    if not recorded:
        return False
    path = PurePosixPath(recorded)
    return not path.is_absolute() and ".." not in path.parts


def _digest(entry: Mapping[str, Any], recorded: str, where: str, refusals: list[str]) -> str | None:
    if entry.get("sha256") is None:
        refusals.append(
            f"Запись пути '{recorded}' в файле ответов {ANSWERS_FILE_NAME} не называет "
            "хеш содержимого: ключ sha256 обязателен."
        )
        return None
    digest = _string(entry, "sha256", where, refusals)
    if digest is None:
        return None
    if not _DIGEST_PATTERN.fullmatch(digest):
        refusals.append(
            f"Ключ 'sha256' {where} обязан быть хешем sha256 из 64 знаков 0-9a-f, "
            f"а записан как '{digest}'."
        )
        return None
    return digest


def _path_version(
    entry: Mapping[str, Any], recorded: str, where: str, refusals: list[str]
) -> Version | None:
    if entry.get("version") is None:
        refusals.append(
            f"Запись пути '{recorded}' в файле ответов {ANSWERS_FILE_NAME} не называет "
            "версию, которой путь написан: ключ version обязателен."
        )
        return None
    raw = _string(entry, "version", where, refusals)
    if raw is None:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        refusals.append(
            f"Ключ 'version' {where} обязан быть версией по PEP 440, а записан как '{raw}'."
        )
        return None


def _unknown_keys(
    payload: Mapping[str, Any], allowed: frozenset[str], where: str, refusals: list[str]
) -> None:
    listed = ", ".join(sorted(allowed))
    for key in sorted(str(key) for key in payload if str(key) not in allowed):
        refusals.append(
            f"Неизвестный ключ '{key}' {where}: допустимые ключи — {listed}. "
            "Уберите ключ или исправьте опечатку в его имени."
        )


def _string(payload: Mapping[str, Any], key: str, where: str, refusals: list[str]) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        refusals.append(_wrong_type(key, where, "строкой", value))
        return None
    return value


def _integer(payload: Mapping[str, Any], key: str, where: str, refusals: list[str]) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        refusals.append(_wrong_type(key, where, "целым числом", value))
        return None
    return value


def _boolean(payload: Mapping[str, Any], key: str, where: str, refusals: list[str]) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        refusals.append(_wrong_type(key, where, "логическим значением", value))
        return None
    return value


def _string_list(
    payload: Mapping[str, Any], key: str, where: str, refusals: list[str]
) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        refusals.append(_wrong_type(key, where, "списком строк", value))
        return ()
    return tuple(value)


def _mapping(
    payload: Mapping[str, Any], key: str, where: str, refusals: list[str]
) -> Mapping[str, Any] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        refusals.append(_wrong_type(key, where, "отображением", value))
        return None
    return value


def _service_where(name: str) -> str:
    return f"в записи сервиса '{name}' файла ответов {ANSWERS_FILE_NAME}"


def _wrong_type(key: str, where: str, expected: str, value: object) -> str:
    return (
        f"Ключ '{key}' {where} обязан быть {expected}, а записан как "
        f"{_type_name(value)}: {value!r}."
    )


def _type_name(value: object) -> str:
    if value is None:
        return "пусто"
    if isinstance(value, bool):
        return "логическое значение"
    if isinstance(value, int):
        return "целое число"
    if isinstance(value, float):
        return "дробное число"
    if isinstance(value, str):
        return "строка"
    if isinstance(value, Mapping):
        return "отображение"
    if isinstance(value, list):
        return "список"
    return type(value).__name__
