"""Офлайн-перевод запроса RU→EN для научного поиска (зона ML-A).

Научные API (arXiv, Crossref, Semantic Scholar) — англоязычные и на русский
запрос возвращают нерелевантную выдачу. Запрос переводится перед обращением
к ним. Перевод локальный (argos-translate, модель в образе) — текст запроса
не покидает сервис. Любой сбой (пакет/модель недоступны, ошибка перевода)
деградирует к исходному тексту: поиск не прерывается, снижается лишь
релевантность.
"""
import logging
import re
import threading

log = logging.getLogger(__name__)

_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)

# Доменные поправки к машинному переводу: argos — общая модель и искажает
# металлургические термины — «ложных друзей переводчика». «штейн» (copper/nickel
# matte — сульфидный расплав) переводится как «stein» — фамилия из математических
# статей (Stein Variational Gradient Flow), которая добавляет в научную выдачу
# нерелевантные результаты. Замена по целому слову (\b) после перевода. Ключ —
# в нижнем регистре; сравнение регистронезависимое.
_GLOSSARY = {
    "stein": "matte",
}
_GLOSSARY_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _GLOSSARY) + r")\b", re.IGNORECASE
)


def _apply_glossary(text: str) -> str:
    """Правит доменные ошибки перевода по словарю (целые слова, любой регистр)."""
    return _GLOSSARY_RE.sub(lambda m: _GLOSSARY[m.group(0).lower()], text)

# Ленивая инициализация: модель ресурсоёмкая, загружается один раз при первом
# переводе. None — инициализация не выполнялась; False — модель недоступна
# (повторные попытки не выполняются); объект — модель готова
_translation = None
_lock = threading.Lock()


def _get_translation():
    global _translation
    if _translation is not None:
        return _translation or None
    with _lock:
        if _translation is not None:
            return _translation or None
        try:
            from argostranslate import translate as _argos

            languages = _argos.get_installed_languages()
            ru = next((lang for lang in languages if lang.code == "ru"), None)
            en = next((lang for lang in languages if lang.code == "en"), None)
            _translation = ru.get_translation(en) if ru and en else False
            if not _translation:
                log.warning("Перевод RU→EN недоступен: языковой пакет ru/en не установлен")
        except Exception as exc:  # пакет argostranslate отсутствует или повреждён
            log.warning("Перевод RU→EN недоступен: %s", exc)
            _translation = False
    return _translation or None


def to_english(text: str) -> str:
    """Переводит русский текст на английский для научного поиска. Текст без
    кириллицы (уже английский/формулы) и любой сбой перевода — возвращаются
    как есть."""
    if not text or not _CYRILLIC_RE.search(text):
        return text
    translation = _get_translation()
    if translation is None:
        return text
    try:
        result = translation.translate(text).strip()
        return _apply_glossary(result) if result else text
    except Exception as exc:
        log.warning("Сбой перевода RU→EN, ищем по оригиналу: %s", exc)
        return text
