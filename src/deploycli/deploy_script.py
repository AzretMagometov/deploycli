"""Каркас скрипта деплоя ``deploy/deploy.sh``: замок, порядок шагов, обёртка compose.

См. ADR-008, ADR-009 и ADR-012 в decisions.md. Этот модуль владеет файлом
целиком, но пишет в него только то, что принадлежит каркасу: заголовок с
контрактом запуска, строгие флаги оболочки, разбор аргументов, замок на
весь скрипт, единственную обёртку вызова compose и заголовки секций в
решённом порядке. Команды самих шагов — предполёта, копии, миграции,
гейта, записи деплоя, очистки — пишут свои тикеты; точки вставки
размечены в шаблоне комментариями Jinja и в вывод не попадают.

Команд реестра (``docker login``, ``docker pull``, ``docker logout``) в
этом файле нет вовсе: образы тянет отдельное подключение ssh до сессии
деплоя, потому что у одного вызова ssh один stdin, а здесь он занят
содержимым файла окружения (ADR-012).

Вывод собирается шаблоном Jinja2 (ADR-004), а не склейкой строк: шаблон
отвечает за раскладку строк, модуль — за проверку входа и за то, какие
секции в выводе есть.
"""

from dataclasses import dataclass

from jinja2 import Environment, PackageLoader, StrictUndefined

from deploycli.traefik_names import validate_segment

_ENV = Environment(
    loader=PackageLoader("deploycli", "templates"),
    autoescape=False,  # вывод не HTML: кавычки и `${}` должны пройти как есть
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
_TEMPLATE = _ENV.get_template("deploy_sh.j2")


@dataclass(frozen=True, slots=True)
class MigrationStep:
    """Шаг миграции в скрипте и то, снимается ли перед ним резервная копия.

    Копия существует ровно ради следующего шага (ADR-008), поэтому она
    описана внутри шага миграции, а не рядом с ним: деплоя без миграции
    копия не касается, и состояния «копия есть, миграции нет» в модели
    не существует.
    """

    backup: bool = False


@dataclass(frozen=True, slots=True)
class DeployPlan:
    """Что генератор знает о скрипте деплоя к моменту его записи.

    ``stack`` — имя стека: оно становится сегментом пути ``/opt/<стек>``,
    который стоит в каждом вызове compose (ADR-012). ``migration`` пустая
    означает, что ответа про мигрирующий сервис нет: шага миграции в
    файле тогда нет вовсе, не пустого, а никакого (ADR-008).
    """

    stack: str
    migration: MigrationStep | None = None


def render_deploy_script(plan: DeployPlan) -> str:
    """Собирает текст ``deploy/deploy.sh`` по плану деплоя.

    Имя стека проверяется по тому же алфавиту ``^[a-z][a-z0-9-]*$``, что и
    имена в пространстве имён стека (:mod:`deploycli.traefik_names`):
    имя вне алфавита уехало бы сегментом пути в каждую команду скрипта.
    """
    validate_segment(plan.stack)
    return _TEMPLATE.render(stack=plan.stack, migration=plan.migration)
