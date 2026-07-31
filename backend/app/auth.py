"""Доступ к действиям, меняющим базу знаний.

Закрыты только операции записи: загрузка и удаление документа, повторная
обработка, решения по кандидатам. Чтение — вопросы, поиск, просмотр документов,
состояние системы — остаётся открытым: пароль на каждом шаге чтения мешал бы
работе и ничего не защищал бы дополнительно.

Пара логин-пароль задаётся переменными окружения ADMIN_USER и ADMIN_PASSWORD.
Пустая любая из них выключает проверку целиком — локальная разработка и запуск
без настройки не ломаются, защита включается заданием пароля.

Учётная запись одна на всех, кому разрешено менять базу: реестра пользователей
в системе нет, и заводить его ради одной роли незачем. Следствие: по записи в
базе нельзя установить, кто именно принял решение.
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets

from fastapi import Header, HTTPException


def credentials() -> tuple[str, str] | None:
    """Заданная пара или None, если проверка выключена."""
    user = os.getenv("ADMIN_USER") or ""
    password = os.getenv("ADMIN_PASSWORD") or ""
    return (user, password) if user and password else None


def _matches(header: str, expected: tuple[str, str]) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, password = decoded.partition(":")
    if not sep:
        return False
    # compare_digest вместо ==: обычное сравнение строк завершается на первом
    # несовпавшем символе, и время ответа выдаёт длину верного префикса
    return (secrets.compare_digest(user, expected[0])
            and secrets.compare_digest(password, expected[1]))


def require_editor(authorization: str | None = Header(default=None)) -> None:
    """Пропускает действие или отвечает 401.

    Заголовок WWW-Authenticate намеренно не отправляется: с ним браузер на
    обычном переходе показал бы собственное окно ввода поверх интерфейса.
    Форму рисует интерфейс, ему достаточно кода ответа.
    """
    expected = credentials()
    if expected is None:
        return
    if not authorization or not _matches(authorization, expected):
        raise HTTPException(status_code=401, detail="Требуется вход: действие меняет базу знаний")
