"""Тесты файла ответов ``.deploycli.yml``: схема, чтение, запись, отказы.

ADR-010 (файл ответов — вход генератора, учёт по путям), ADR-011 (состав
ответов), ADR-013 (``auto_contour``), ADR-015 (сравнение версий и порог
читаемости).

Golden-файлы в ``tests/fixtures/answers_file/*.txt`` фиксируют дословный
текст отказов, ``*.yml`` — сами файлы ответов. Подстановки ``{{version}}``,
``{{installed}}`` и ``{{threshold}}`` в golden-файлах заполняются в тесте:
версия установленного инструмента растёт, а текст отказа — нет.
"""

from pathlib import Path

import pytest
from packaging.version import Version

from deploycli.answers_file import (
    ANSWERS_FILE_NAME,
    OLDEST_READABLE_VERSION,
    AnswersFile,
    AnswersFileRefused,
    Forge,
    PublicAnswer,
    ServiceAnswers,
    ServiceCommand,
    WrittenPath,
    check_version,
    content_digest,
    installed_version,
    read_answers,
    write_answers,
)

FIXTURES = Path(__file__).parent / "fixtures" / "answers_file"

WORKFLOW_DIGEST = "8d3fb39ce523c6fc1dafec39102aef44e599f72fc3a22cf4c95612234571a570"
DEPLOY_DIGEST = "c6f1676734b35a3c26274cdcdedc4a34ead3d0b815ab6d06afd4a58a55baf13c"


def _golden_messages(name: str, **substitutions: str) -> tuple[str, ...]:
    return tuple(_golden_text(name, **substitutions).splitlines())


def _golden_text(name: str, **substitutions: str) -> str:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    for key, value in substitutions.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _refusal(name: str) -> tuple[str, ...]:
    with pytest.raises(AnswersFileRefused) as refused:
        read_answers(FIXTURES / name)
    return refused.value.messages


def _full_answers() -> AnswersFile:
    return AnswersFile(
        forge=Forge.FORGEJO,
        contours=("test", "prod"),
        auto_contour="test",
        profiles=("tools",),
        migration=ServiceCommand(service="api", command="alembic upgrade head"),
        backup=ServiceCommand(service="db", command="pg_dump -U app app"),
        services={
            "api": ServiceAnswers(
                public=PublicAnswer(port=8000, domain_variable="API_DOMAIN", path_prefix="/api"),
                built=True,
            ),
            "db": ServiceAnswers(public=None, built=False),
        },
        paths={
            ".forgejo/workflows/deploy.yml": WrittenPath(
                digest=WORKFLOW_DIGEST, version=Version("0.1.0")
            ),
            "deploy/deploy.sh": WrittenPath(digest=DEPLOY_DIGEST, version=Version("0.1.0")),
        },
    )


# Чтение: полный файл и файл без единого ответа.
def test_full_file_is_read_into_answers() -> None:
    assert read_answers(FIXTURES / "full.yml") == _full_answers()


def test_path_record_keeps_path_digest_and_version() -> None:
    answers = read_answers(FIXTURES / "full.yml")

    assert sorted(answers.paths) == [".forgejo/workflows/deploy.yml", "deploy/deploy.sh"]
    assert answers.paths["deploy/deploy.sh"] == WrittenPath(
        digest=DEPLOY_DIGEST, version=Version("0.1.0")
    )


def test_file_with_version_only_leaves_every_answer_absent() -> None:
    assert read_answers(FIXTURES / "minimal.yml") == AnswersFile()


def test_content_digest_is_sha256_in_lowercase_hex() -> None:
    assert content_digest(b"workflow\n") == WORKFLOW_DIGEST


# Отказ 1 (ADR-010): версия файла новее установленной.
def test_version_newer_than_installed_is_refused_naming_both_versions() -> None:
    with pytest.raises(AnswersFileRefused) as refused:
        check_version(Version("9.9.9"), Version("0.1.0"))

    assert refused.value.messages == _golden_messages("version_newer.txt", installed="0.1.0")


def test_file_written_by_newer_version_is_refused_on_read() -> None:
    assert _refusal("version_newer.yml") == _golden_messages(
        "version_newer.txt", installed=str(installed_version())
    )


# Равная версия (ADR-010): прогон разрешён.
def test_equal_version_passes() -> None:
    check_version(installed_version(), installed_version())


def test_file_written_by_installed_version_is_read(tmp_path: Path) -> None:
    target = tmp_path / ANSWERS_FILE_NAME
    write_answers(target, _full_answers())

    assert read_answers(target) == _full_answers()


def test_version_older_than_installed_passes() -> None:
    check_version(OLDEST_READABLE_VERSION, Version("99.0.0"))


# Отказ 3 (ADR-015): версия ниже порога читаемости.
def test_version_below_readability_threshold_is_refused() -> None:
    assert _refusal("version_below_threshold.yml") == _golden_messages(
        "version_below_threshold.txt", threshold=str(OLDEST_READABLE_VERSION)
    )


def test_version_at_readability_threshold_passes() -> None:
    check_version(OLDEST_READABLE_VERSION, installed_version())


# Отсутствующий ключ версии (ADR-015): отказ тем же голосом.
def test_missing_version_key_is_refused() -> None:
    assert _refusal("missing_version.yml") == _golden_messages("missing_version.txt")


def test_version_read_by_yaml_as_number_is_refused() -> None:
    assert _refusal("version_not_a_string.yml") == _golden_messages("version_not_a_string.txt")


def test_version_outside_pep440_is_refused() -> None:
    assert _refusal("version_not_pep440.yml") == _golden_messages("version_not_pep440.txt")


# Отказ 2 (ADR-010): неизвестный ключ — с именем ключа, а не молчаливое игнорирование.
def test_unknown_keys_are_refused_with_their_names() -> None:
    assert _refusal("unknown_keys.yml") == _golden_messages("unknown_keys.txt")


# Схема: типы значений. YAML молча читает 1.10 числом, "8000" строкой.
def test_values_of_wrong_type_are_refused_all_at_once() -> None:
    assert _refusal("bad_types.yml") == _golden_messages("bad_types.txt")


def test_incomplete_service_records_are_refused() -> None:
    assert _refusal("incomplete_services.yml") == _golden_messages("incomplete_services.txt")


def test_records_of_wrong_shape_are_refused() -> None:
    assert _refusal("bad_shapes.yml") == _golden_messages("bad_shapes.txt")


def test_path_records_outside_project_and_broken_fields_are_refused() -> None:
    assert _refusal("bad_paths.yml") == _golden_messages("bad_paths.txt")


# Правка руками может повторить ключ — YAML оставил бы последний молча.
def test_key_repeated_in_one_mapping_is_refused_with_its_name_and_line() -> None:
    assert _refusal("duplicate_key.yml") == _golden_messages("duplicate_key.txt")


# ADR-013: auto_contour обязан называть контур из списка.
def test_auto_contour_outside_contours_is_refused() -> None:
    assert _refusal("auto_contour_unknown.yml") == _golden_messages("auto_contour_unknown.txt")


def test_forge_outside_two_supported_is_refused() -> None:
    assert _refusal("unknown_forge.yml") == _golden_messages("unknown_forge.txt")


def test_file_that_is_not_a_mapping_is_refused() -> None:
    assert _refusal("not_a_mapping.yml") == _golden_messages("not_a_mapping.txt")


def test_empty_file_is_refused() -> None:
    assert _refusal("empty.yml") == _golden_messages("empty.txt")


def test_broken_yaml_is_refused_with_the_parser_message() -> None:
    messages = _refusal("broken.yml")

    assert len(messages) == 1
    assert messages[0].startswith(f"Файл ответов {ANSWERS_FILE_NAME} не разбирается как YAML: ")
    assert "line 4" in messages[0]


def test_missing_file_is_not_a_refusal_but_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_answers(tmp_path / ANSWERS_FILE_NAME)


# Запись: прогон переписывает файл целиком.
def test_write_produces_the_whole_file(tmp_path: Path) -> None:
    target = tmp_path / ANSWERS_FILE_NAME

    write_answers(target, _full_answers())

    assert target.read_text(encoding="utf-8") == _golden_text(
        "written.yml", version=str(installed_version())
    )


def test_write_stamps_the_installed_version_not_the_one_read(tmp_path: Path) -> None:
    target = tmp_path / ANSWERS_FILE_NAME

    write_answers(target, AnswersFile())

    assert target.read_text(encoding="utf-8") == _golden_text(
        "written_minimal.yml", version=str(installed_version())
    )


def test_repeated_write_is_stable_byte_for_byte(tmp_path: Path) -> None:
    target = tmp_path / ANSWERS_FILE_NAME
    write_answers(target, _full_answers())
    first = target.read_bytes()

    write_answers(target, read_answers(target))

    assert target.read_bytes() == first


def test_write_ignores_the_order_in_which_services_and_paths_arrive(tmp_path: Path) -> None:
    answers = _full_answers()
    shuffled = AnswersFile(
        forge=answers.forge,
        contours=answers.contours,
        auto_contour=answers.auto_contour,
        profiles=answers.profiles,
        migration=answers.migration,
        backup=answers.backup,
        services=dict(reversed(list(answers.services.items()))),
        paths=dict(reversed(list(answers.paths.items()))),
    )
    target = tmp_path / ANSWERS_FILE_NAME
    other = tmp_path / "other.yml"

    write_answers(target, answers)
    write_answers(other, shuffled)

    assert other.read_bytes() == target.read_bytes()


def test_contour_order_survives_the_write(tmp_path: Path) -> None:
    target = tmp_path / ANSWERS_FILE_NAME

    write_answers(target, AnswersFile(contours=("prod", "test"), auto_contour="prod"))

    assert read_answers(target).contours == ("prod", "test")


# ADR-010: правка ответа руками — штатный вход следующего прогона.
def test_hand_edited_answer_is_picked_up_without_losing_path_records(tmp_path: Path) -> None:
    target = tmp_path / ANSWERS_FILE_NAME
    write_answers(target, _full_answers())

    target.write_text(
        target.read_text(encoding="utf-8").replace("API_DOMAIN", "PUBLIC_HOST"),
        encoding="utf-8",
    )
    answers = read_answers(target)

    assert answers.services["api"].public == PublicAnswer(
        port=8000, domain_variable="PUBLIC_HOST", path_prefix="/api"
    )
    assert answers.paths == _full_answers().paths

    write_answers(target, answers)
    rewritten = read_answers(target)

    assert rewritten == answers
