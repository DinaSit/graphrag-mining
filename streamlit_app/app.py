from __future__ import annotations

import os
import html
import re
import time
from typing import Any

import pandas as pd
import requests
import streamlit as st


BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
DEFAULT_QUESTION = "Какие методы очистки шахтных вод описаны и какие результаты они дают?"
QUESTION_TEMPLATES = [
    DEFAULT_QUESTION,
    "Что известно про никелевые катоды и качество осадка?",
    "Какие методы извлечения благородных металлов из шламов и шлаков применяются?",
    "Обзор способов удаления SO2 из отходящих газов металлургических предприятий мира",
]
UPLOAD_TYPES = ["pdf", "txt", "csv", "md", "json", "docx", "docm", "pptx", "xlsx", "xlsm"]
STATUS_LABELS = {
    "pending_review": "На проверке",
    "approved": "Утверждено",
    "rejected": "Отклонено",
    "conflicting": "⚠️ спорный",
    "queued": "В очереди",
    "completed": "Готово",
    "processing": "В обработке",
    "failed": "Ошибка",
}
EDGE_LABELS = {
    "ABOUT": "о материале",
    "BASED_ON": "на основании",
    "MEASURES": "измеряет",
    "PRODUCED": "дал эффект",
    "SUPPORTS": "подтверждает",
    "CONDUCTED_BY": "кто проводил",
    "MENTIONS": "упоминает",
    "applies_to": "применяется к",
    "uses_material": "использует материал",
    "operates_at_condition": "при условии",
    "measured_parameter": "измерял параметр",
    "produced_effect": "дал эффект",
    "validated_by": "подтверждён",
    "contradicts": "противоречит",
    "authored_by": "автор",
    "expert_in": "эксперт в",
    "applied_in": "применялось в",
    "worked_on": "работал над",
    "described_in": "описан в",
    "researched": "исследовала",
    "validated": "подтвердила",
}
# Модель цитирует по-разному: [fragment-…], (claim-fragment-…, …), сокращённо
# [doc-xxxx-p29] и порядковыми номерами [1]. Ловим все формы одним проходом:
# группа ids — скобки (любые) с id-подобным содержимым, группа nums — голые числа.
REFERENCE_RE = re.compile(r"\b(?:claim-)?(?:fragment-)?doc-[0-9a-f]{4,}[A-Za-z0-9_-]*")
CITATION_TOKEN_RE = re.compile(
    r"[\[(]\s*(?P<ids>[^\[\]()]*?(?:claim-|fragment-|doc-)[^\[\]()]*?)\s*[\])]"
    r"|\[\s*(?P<nums>\d{1,2}(?:\s*,\s*\d{1,2})*)\s*\]"
)
_DOC_HASH_RE = re.compile(r"(doc-[0-9a-f]{4,})")


st.set_page_config(page_title="Nornickel GraphRAG", layout="wide")
st.markdown(
    """
    <style>
      #MainMenu,
      footer,
      header,
      [data-testid="stHeader"],
      [data-testid="stToolbar"],
      [data-testid="stDecoration"],
      [data-testid="stStatusWidget"],
      [data-testid="stDeployButton"],
      [data-testid="stAppDeployButton"],
      .stDeployButton,
      .viewerBadge_container__1QSob,
      .viewerBadge_link__1S137 {
        display: none !important;
        visibility: hidden !important;
        height: 0 !important;
      }
      [data-testid="stAppViewContainer"] {
        background: #ffffff;
      }
      .block-container { padding-top: 1.25rem; padding-bottom: 2rem; }
      [data-testid="stMetric"] {
        background: #f8fafc;
        border: 1px solid #e5e7eb;
        border-radius: 6px;
        padding: 12px;
      }
      div[data-testid="stExpander"] {
        border: 1px solid #e5e7eb;
        border-radius: 6px;
      }
      .status-pill {
        display: inline-block;
        padding: 4px 9px;
        margin: 0 6px 6px 0;
        border-radius: 999px;
        font-size: 0.86rem;
        border: 1px solid transparent;
      }
      .status-ok { background: #ecfdf5; color: #065f46; border-color: #a7f3d0; }
      .status-warn { background: #fffbeb; color: #92400e; border-color: #fde68a; }
      .status-bad { background: #fef2f2; color: #991b1b; border-color: #fecaca; }
      .muted { color: #64748b; font-size: 0.92rem; }
      .answer-article {
        line-height: 1.62;
      }
      .answer-article p {
        margin: 0 0 0.75rem 0;
      }
      .citation-note {
        color: #2563eb;
        font-size: 0.72em;
        font-weight: 500;
        line-height: 0;
        padding-left: 1px;
        text-decoration: none;
        vertical-align: super;
        cursor: help;
        white-space: nowrap;
      }
      .citation-note:hover { color: #1d4ed8; text-decoration: underline; }
    </style>
    """,
    unsafe_allow_html=True,
)


def api_get(path: str) -> Any:
    response = requests.get(f"{BACKEND_URL}{path}", timeout=20)
    response.raise_for_status()
    return response.json()


def api_post(path: str, payload: dict[str, Any], timeout: float = 120) -> Any:
    response = requests.post(f"{BACKEND_URL}{path}", json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def api_post_empty(path: str) -> Any:
    response = requests.post(f"{BACKEND_URL}{path}", timeout=60)
    response.raise_for_status()
    return response.json()


def api_delete(path: str) -> Any:
    response = requests.delete(f"{BACKEND_URL}{path}", timeout=120)
    response.raise_for_status()
    return response.json()


def upload_file(file) -> Any:
    files = {"file": (file.name, file.getvalue(), file.type or "application/octet-stream")}
    data = {"document_type": file.name.split(".")[-1], "source_label": file.name, "access_level": "uploaded"}
    response = requests.post(f"{BACKEND_URL}/ingest", files=files, data=data, timeout=180)
    response.raise_for_status()
    return response.json()


def _api_error_text(exc: Exception) -> str:
    """Суть сбоя API человеческим языком — вместо сырого трейсбека."""
    if isinstance(exc, requests.Timeout):
        return "backend не ответил вовремя; повторите попытку позже"
    if isinstance(exc, requests.ConnectionError):
        return "backend недоступен (возможно, перезапускается)"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        detail = ""
        try:
            body = exc.response.json()
            detail = str(body.get("detail") or "") if isinstance(body, dict) else ""
        except ValueError:
            detail = (exc.response.text or "")[:200]
        suffix = f": {detail}" if detail else ""
        return f"backend вернул ошибку {exc.response.status_code}{suffix}"
    return str(exc)


def graph_to_dot(graph: dict[str, Any]) -> str:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    lines = [
        "digraph G {",
        "rankdir=LR;",
        'graph [bgcolor="transparent", pad="0.15"];',
        'node [shape=box, style="rounded,filled", fillcolor="#f8fafc", color="#94a3b8", fontname="Arial"];',
        'edge [color="#64748b", fontname="Arial"];',
    ]
    for node in nodes[:80]:
        node_id = _dot_id(node["id"])
        label = f"{node.get('type', 'Entity')}: {node.get('label', node.get('id'))}"
        lines.append(f'{node_id} [label="{_escape(label)}"];')
    for edge in edges[:120]:
        label = EDGE_LABELS.get(edge.get("label", ""), edge.get("label", ""))
        lines.append(f'{_dot_id(edge["source"])} -> {_dot_id(edge["target"])} [label="{_escape(label)}"];')
    lines.append("}")
    return "\n".join(lines)


def _dot_id(value: str) -> str:
    return "n_" + "".join(ch if ch.isalnum() else "_" for ch in value)


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')[:120]


TYPE_LABELS = {
    "Claim": "Утверждение",
    "Entity": "Сущность",
    "Relation": "Связь",
}


def _status_label(value: Any) -> str:
    raw = value.value if hasattr(value, "value") else str(value)
    return STATUS_LABELS.get(raw, raw)


@st.cache_data(ttl=30)
def _document_names() -> dict[str, str]:
    """id документа → имя файла; для человекочитаемых источников."""
    try:
        return {doc["id"]: doc["filename"] for doc in api_get("/documents")}
    except Exception:
        return {}


def _source_name(document_id: str | None) -> str:
    if not document_id:
        return "?"
    return _document_names().get(document_id, document_id)


def _place_label(source: dict[str, Any]) -> str:
    """Человекочитаемый адрес фрагмента: у PDF — страница, у PPTX — слайд,
    у DOCX/таблиц страниц нет — сквозной номер блока."""
    section = source.get("section") or ""
    page = source.get("page")
    if not page:
        return ""
    if section.startswith("PDF"):
        return f"Страница: {page}"
    if section.startswith("PPTX"):
        return f"Слайд: {page}"
    return f"Блок: {page}"


# Служебные маркеры парсера (parsers.py): это не разделы документа
_SERVICE_SECTIONS = {"Uploaded text", "Uploaded CSV", "Parser adapter boundary"}


def _place_section(source: dict[str, Any]) -> str:
    """Раздел документа, если парсер его распознал (заголовок главы в DOCX)."""
    section = source.get("section") or ""
    if not section or section in _SERVICE_SECTIONS:
        return ""
    if section.startswith(("PDF", "PPTX", "DOCX", "XLSX", "CSV")):
        return ""
    return section


def _pill(label: str, value: Any) -> str:
    raw = str(value or "unknown")
    css = "status-ok" if raw in {"ok", "enabled", "remote", "completed"} else "status-warn"
    if raw in {"disabled", "failed", "memory", "unknown"}:
        css = "status-bad"
    return f'<span class="status-pill {css}">{label}: {raw}</span>'


def _short_model_name(model: Any) -> str:
    raw = str(model or "").strip()
    if not raw:
        return "none"
    if raw.startswith("gpt://"):
        parts = [part for part in raw.split("/") if part]
        if len(parts) >= 2:
            name = "/".join(parts[-2:])
            return name.removesuffix("/latest")
    return raw.removesuffix(":cloud")


def _llm_pill(health: dict[str, Any]) -> str:
    status = str(health.get("answer_llm_status") or "unknown")
    provider = str(health.get("answer_llm_provider") or "unknown")
    model = _short_model_name(health.get("answer_llm_model"))
    provider_label = {
        "yandex": "Yandex",
        "fallback": "Ollama",
        "none": "none",
    }.get(provider, provider)
    css = "status-ok" if status == "available" else "status-warn"
    if status in {"unavailable", "failed", "unknown"} or provider == "none":
        css = "status-bad"
    suffix = "" if status == "available" else f" ({status})"
    title = f"provider={provider}; model={health.get('answer_llm_model') or 'none'}; status={status}"
    if health.get("answer_llm_error"):
        title += f"; error={health['answer_llm_error']}"
    return f'<span class="status-pill {css}" title="{_escape(title)}">LLM: {provider_label} {model}{suffix}</span>'


def _docs_frame(documents: list[dict[str, Any]]) -> pd.DataFrame:
    if not documents:
        return pd.DataFrame()
    frame = pd.DataFrame(documents)
    columns = ["id", "filename", "document_type", "source_label", "status", "element_count", "storage_uri", "created_at"]
    frame = frame[[column for column in columns if column in frame.columns]].copy()
    rename = {
        "id": "ID",
        "filename": "Файл",
        "document_type": "Тип",
        "source_label": "Источник",
        "status": "Статус",
        "element_count": "Evidence units",
        "storage_uri": "MinIO URI",
        "created_at": "Создан",
    }
    frame.rename(columns=rename, inplace=True)
    if "Статус" in frame:
        frame["Статус"] = frame["Статус"].map(_status_label)
    return frame


def _upload_frame(results: list[dict[str, Any]]) -> pd.DataFrame:
    if not results:
        return pd.DataFrame()
    frame = pd.DataFrame(results)
    # job_id — служебное поле поллинга, в таблицу не выводится
    columns = ["filename", "status", "evidence_units", "storage_uri", "error"]
    frame = frame[[column for column in columns if column in frame.columns]].copy()
    frame.rename(
        columns={
            "filename": "Файл",
            "status": "Статус",
            "evidence_units": "Evidence units",
            "storage_uri": "MinIO URI",
            "error": "Ошибка",
        },
        inplace=True,
    )
    if "Статус" in frame:
        frame["Статус"] = frame["Статус"].map(_status_label)
    return frame


def _experiments_frame(
    experiments: list[dict[str, Any]],
    citation_numbers: dict[tuple[str | None, str | None], int] | None = None,
    source_section_keys: set[tuple[str | None, str | None]] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    citation_numbers = citation_numbers or {}
    source_section_keys = source_section_keys or set()
    for item in experiments:
        source = item.get("source") or {}
        key = _source_key(source)
        footnote = citation_numbers.get(key) if key not in source_section_keys else None
        rows.append(
            {
                "Сноска": f"[{footnote}]" if footnote else "",
                "Эксперимент": item.get("experiment_id"),
                "Материал": item.get("material"),
                "Образец": item.get("sample"),
                "Процесс": item.get("process"),
                "Температура, °C": item.get("temperature_c"),
                "Длительность, ч": item.get("duration_h"),
                "Свойство": item.get("property"),
                "Эффект": item.get("effect"),
                "Лаборатория": item.get("lab"),
                "Confidence": item.get("confidence"),
                "Документ": source.get("document_id"),
                "Фрагмент": source.get("fragment_id"),
            }
        )
    return pd.DataFrame(rows)


def _sources_frame(sources: list[dict[str, Any]]) -> pd.DataFrame:
    rows = [
        {
            "Файл": _source_name(source.get("document_id")),
            "Место": _place_label(source),
            "Раздел": _place_section(source),
            "Цитата": source.get("quote"),
        }
        for source in sources
    ]
    return pd.DataFrame(rows)


def _reference_fragment_candidates(reference: str) -> list[str]:
    reference = reference.removeprefix("claim-")
    candidates = [reference]
    if not reference.startswith("fragment-"):
        candidates.append(f"fragment-{reference}")  # сокращение вида doc-xxxx-docx-p282
    stripped = re.sub(r"-\d+$", "", reference)  # хвост -0/-1 у id кандидата
    if stripped != reference:
        candidates.append(stripped)
        if not stripped.startswith("fragment-"):
            candidates.append(f"fragment-{stripped}")
    return candidates


def _source_for_reference(
    reference: str,
    by_fragment: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for fragment_id in _reference_fragment_candidates(reference):
        if fragment_id in by_fragment:
            return by_fragment[fragment_id]
    # Сокращённая ссылка: doc-хэш (+ хвост места: p29 / docx-p282 / pptx-s6-2).
    # Ищем фрагмент этого документа с тем же хвостом, иначе — первый источник документа.
    match = _DOC_HASH_RE.search(reference)
    if not match:
        return None
    doc_id = match.group(1)
    tail = reference.split(doc_id, 1)[-1].strip("-")
    doc_sources = [s for s in sources if (s.get("document_id") or "").startswith(doc_id)]
    if tail:
        for source in doc_sources:
            fragment_id = source.get("fragment_id") or ""
            if fragment_id.endswith(f"-{tail}") or f"-{tail}-" in fragment_id:
                return source
    return doc_sources[0] if doc_sources else None


def _source_key(source: dict[str, Any]) -> tuple[str | None, str | None]:
    return source.get("document_id"), source.get("fragment_id")


def _merge_answer_sources(answer: dict[str, Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for source in answer.get("sources") or []:
        key = _source_key(source)
        if key not in seen:
            merged.append(source)
            seen.add(key)
    for item in answer.get("experiments") or []:
        source = item.get("source") or {}
        key = _source_key(source)
        if source and key not in seen:
            merged.append(source)
            seen.add(key)
    return merged


class CitationRenderer:
    """Единый реестр сносок на весь ответ: текст, противоречия, таблицы, «Источники».

    Любая форма ссылки модели превращается в википедийную сноску [n] с
    подсказкой (title), в каком разделе искать; нерезолвнутые id-обрывки
    вычищаются из текста, чтобы не торчали в ответе.
    """

    def __init__(
        self,
        sources: list[dict[str, Any]],
        source_section_keys: set[tuple[str | None, str | None]],
    ):
        self.sources = sources
        self.by_fragment = {s.get("fragment_id"): s for s in sources if s.get("fragment_id")}
        self.source_section_keys = source_section_keys
        self.numbers: dict[tuple[str | None, str | None], int] = {}
        self.locations: dict[tuple[str | None, str | None], str] = {}
        self.cited_keys: set[tuple[str | None, str | None]] = set()

    def _register(self, source: dict[str, Any]) -> tuple[int, str]:
        key = _source_key(source)
        if key not in self.numbers:
            self.numbers[key] = len(self.numbers) + 1
            self.locations[key] = (
                "Источники" if key in self.source_section_keys else "Прямые факты и эксперименты"
            )
        self.cited_keys.add(key)
        return self.numbers[key], self.locations[key]

    def _sup(self, number: int, location: str, source: dict[str, Any]) -> str:
        name = _source_name(source.get("document_id"))
        tooltip = f"Сноска [{number}] · раздел «{location}» · {name}"
        return f'<sup class="citation-note" title="{html.escape(tooltip)}">[{number}]</sup>'

    def _notes_for_ids(self, group_text: str) -> list[str]:
        sups: list[str] = []
        seen: set[int] = set()
        for reference in REFERENCE_RE.findall(group_text):
            source = _source_for_reference(reference, self.by_fragment, self.sources)
            if source is None:
                continue
            number, location = self._register(source)
            if number not in seen:
                seen.add(number)
                sups.append(self._sup(number, location, source))
        return sups

    def _render(self, text: str) -> str:
        # Голые номера [1] конвертируются, только если модель не дала ни одной
        # id-ссылки: иначе две системы нумерации перепутаются — обрывки убираем.
        has_id_citations = any(m.group("ids") for m in CITATION_TOKEN_RE.finditer(text))
        chunks: list[str] = []
        last = 0
        for match in CITATION_TOKEN_RE.finditer(text):
            replacement = ""
            if match.group("ids"):
                replacement = "".join(self._notes_for_ids(match.group("ids")))
            elif not has_id_citations:
                sups = []
                for raw_number in match.group("nums").split(","):
                    index = int(raw_number.strip()) - 1
                    if 0 <= index < len(self.sources):
                        source = self.sources[index]
                        number, location = self._register(source)
                        sups.append(self._sup(number, location, source))
                replacement = "".join(sups)
            chunk = html.escape(text[last:match.start()])
            if not replacement:
                chunk = chunk.rstrip(" ")  # мусорная ссылка удалена — подчищаем пробел
            chunks.append(chunk)
            chunks.append(replacement)
            last = match.end()
        chunks.append(html.escape(text[last:]))
        return "".join(chunks)

    def render_article(self, text: str) -> str:
        body = self._render(text).replace("\n\n", "</p><p>").replace("\n", "<br>")
        return f'<div class="answer-article"><p>{body}</p></div>'

    def render_inline(self, text: str) -> str:
        return self._render(text)


def _source_title(source: dict[str, Any], number: int | None) -> str:
    prefix = f"[{number}] " if number else ""
    filename = _source_name(source.get("document_id"))
    page_label = _place_label(source)
    section = _place_section(source)
    parts = [part for part in [f"{prefix}{filename}", page_label, section] if part]
    return " · ".join(parts)


def _facts_frame(facts: list[dict[str, Any]]) -> pd.DataFrame:
    if not facts:
        return pd.DataFrame()
    # Оппонент спорного факта: материал/свойство и документ-источник
    by_id = {fact.get("id"): fact for fact in facts}

    def _opponents(fact: dict[str, Any]) -> str:
        parts = []
        for opponent_id in fact.get("conflicts_with") or []:
            opponent = by_id.get(opponent_id)
            if opponent:
                document = _source_name((opponent.get("source") or {}).get("document_id"))
                parts.append(f"{opponent.get('material')} · {opponent.get('property')} ({document})")
            else:
                parts.append(opponent_id)
        return "; ".join(parts)

    frame = pd.DataFrame(facts)
    frame["Спорит с"] = [_opponents(fact) for fact in facts]
    # Имя файла-источника — предпоследняя колонка; по ней же работает фильтр на вкладке
    frame["Файл"] = [_source_name((fact.get("source") or {}).get("document_id")) for fact in facts]
    columns = ["id", "material", "property", "status", "Спорит с", "confidence", "Файл", "source"]
    frame = frame[[column for column in columns if column in frame.columns]].copy()
    frame.rename(
        columns={
            "id": "ID",
            "material": "Материал",
            "property": "Свойство",
            "status": "Статус",
            "confidence": "Confidence",
            "source": "Источник",
        },
        inplace=True,
    )
    if "Статус" in frame:
        frame["Статус"] = frame["Статус"].map(_status_label)
    return frame


def render_list_block(title: str, items: list[str], empty_text: str, rendered: bool = False) -> None:
    """rendered=True — элементы уже прошли CitationRenderer и содержат html-сноски."""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        values = items or [html.escape(empty_text) if rendered else empty_text]
        for item in values:
            if rendered:
                st.markdown(f'<div class="answer-article">•&nbsp;{item}</div>', unsafe_allow_html=True)
            else:
                st.markdown(f"- {item}")


def render_answer(answer: dict[str, Any], include_hypotheses: bool) -> None:
    # Отказ LLM показывается прямо: пользователь знает, что ответ собран без модели
    if answer.get("llm_error"):
        st.error(f"⚠️ Проблема с LLM: {answer['llm_error']}. Ниже — данные, доступные без модели.")

    experiments = answer.get("experiments", [])
    related_experiments = answer.get("related_experiments", [])
    source_cards = answer.get("sources", [])
    sources = _merge_answer_sources(answer)
    source_section_keys = {_source_key(source) for source in source_cards}
    graph = answer.get("graph", {})
    node_count = len(graph.get("nodes", []))
    edge_count = len(graph.get("edges", []))
    # Один реестр сносок на весь ответ: номера в тексте, списках, таблице
    # фактов и разделе «Источники» совпадают. Списки рендерятся до таблицы,
    # чтобы их сноски уже были в реестре.
    renderer = CitationRenderer(sources, source_section_keys)
    summary_html = renderer.render_article(answer.get("summary") or "Ответ не сформирован.")
    contradictions_html = [renderer.render_inline(item) for item in answer.get("contradictions", [])]
    gaps_html = [renderer.render_inline(item) for item in answer.get("gaps", [])]
    hypotheses_html = [renderer.render_inline(item) for item in answer.get("hypotheses") or []]
    citation_numbers, cited_keys = renderer.numbers, renderer.cited_keys

    summary_col, metric_col = st.columns([4, 1])
    with summary_col:
        with st.container(border=True):
            st.markdown("**Ответ**")
            st.markdown(summary_html, unsafe_allow_html=True)
    with metric_col:
        st.metric("Confidence", f"{float(answer.get('confidence') or 0):.0%}")

    web_answer = answer.get("web_answer") or {}
    if web_answer.get("answer") or web_answer.get("snippets"):
        with st.container(border=True):
            st.markdown("**🌐 Ответ из внешних источников** · _не верифицировано, в базу знаний не записано_")
            if web_answer.get("answer"):
                st.write(web_answer["answer"])
                if web_answer.get("url"):
                    st.markdown(f"Источник: {web_answer['url']}")
            else:
                # LLM не ответила: показываем сырые результаты веб-поиска
                if web_answer.get("llm_error"):
                    st.caption("Связный ответ недоступен (LLM не ответила) — сырые результаты поиска:")
                for item in web_answer.get("snippets", []):
                    st.markdown(f"- [{item.get('title') or item.get('url')}]({item.get('url')}) — {item.get('snippet', '')}")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Прямые факты", len(experiments))
    m2.metric("Источники", len(source_cards))
    m3.metric("Узлы графа", node_count)
    m4.metric("Связи", edge_count)

    if experiments:
        st.markdown("#### Прямые факты и эксперименты")
        st.dataframe(
            _experiments_frame(experiments, citation_numbers, source_section_keys),
            use_container_width=True,
            hide_index=True,
        )

    if related_experiments:
        with st.expander(f"Смежные факты и гипотезы ({len(related_experiments)})", expanded=not experiments):
            st.caption("Это контекст по похожим материалам, процессам или источникам; он не считается прямым ответом.")
            st.dataframe(_experiments_frame(related_experiments), use_container_width=True, hide_index=True)
            related_sources = answer.get("related_sources") or []
            if related_sources:
                st.markdown("**Источники смежных фактов**")
                st.dataframe(_sources_frame(related_sources), use_container_width=True, hide_index=True)

    # Семантический поиск bge-m3: работает и при недоступной LLM;
    # раскрыт по умолчанию, когда фактов нет — это основной результат
    search_hits = answer.get("search_hits") or []
    if search_hits:
        with st.expander(f"🔎 Найдено по смыслу ({len(search_hits)} фрагментов)", expanded=not experiments):
            for hit in search_hits:
                meta = hit.get("metadata") or {}
                source = hit.get("source") or {}
                name = meta.get("filename") or _source_name(source.get("document_id"))
                place = _place_label(source)
                st.markdown(f"**{name}**{' · ' + place if place else ''} · релевантность {hit.get('score', 0):.2f}")
                st.caption((hit.get("text") or "")[:400])

    left, middle, right = st.columns(3)
    with left:
        render_list_block("Противоречия", contradictions_html, "Не выявлены", rendered=True)
    with middle:
        render_list_block("Пробелы", gaps_html, "Не выявлены", rendered=True)
    with right:
        # Реальные гипотезы из режима косвенного поиска; заглушка — только если их нет
        if not hypotheses_html and not include_hypotheses:
            hypotheses_html = [html.escape("Скрыты в текущей выборке.")]
        render_list_block("Гипотезы", hypotheses_html, "Не выявлены", rendered=True)

    if source_cards:
        st.markdown("#### Источники")
        # Сквозная нумерация: процитированные карточки — под своими номерами
        # из текста, остальные продолжают счёт. Номер виден в заголовке карточки.
        cited_source_cards = [s for s in source_cards if _source_key(s) in cited_keys]
        cited_source_cards.sort(key=lambda source: citation_numbers.get(_source_key(source), 10**9))
        uncited_source_cards = [s for s in source_cards if _source_key(s) not in cited_keys]
        next_number = len(citation_numbers) + 1
        display_numbers = dict(citation_numbers)
        for source in uncited_source_cards:
            display_numbers.setdefault(_source_key(source), next_number)
            next_number = max(display_numbers.values()) + 1
        for source in cited_source_cards + uncited_source_cards:
            title = _source_title(source, display_numbers.get(_source_key(source)))
            with st.expander(title):
                st.write(source.get("quote") or "Нет цитаты")
                st.caption(source.get("fragment_id") or "fragment не указан")

    if node_count:
        st.markdown("#### Цепочка графа")
        st.graphviz_chart(graph_to_dot(graph), use_container_width=True)


# Реальное извлечение фактов идёт минуты: статус job-а поллится с запасом,
# после таймаута остаётся ручная кнопка «Обновить статус»
JOB_POLL_INTERVAL_S = 4
JOB_POLL_TIMEOUT_S = 15 * 60
_ACTIVE_JOB_STATUSES = {"queued", "processing"}


def _refresh_job_statuses(results: list[dict[str, Any]]) -> None:
    """Одна волна опроса GET /jobs/{job_id} для незавершённых загрузок."""
    for row in results:
        if not row.get("job_id") or row.get("status") not in _ACTIVE_JOB_STATUSES:
            continue
        try:
            job = api_get(f"/jobs/{row['job_id']}")
        except Exception:
            continue  # временный сбой сети — статус уточнится следующей волной
        row["status"] = job.get("status") or row["status"]
        if job.get("error"):
            row["error"] = job["error"]


def _has_active_jobs(results: list[dict[str, Any]]) -> bool:
    return any(row.get("job_id") and row.get("status") in _ACTIVE_JOB_STATUSES for row in results)


def render_upload_panel(key_prefix: str) -> None:
    state_key = f"{key_prefix}-last-upload"
    wait_key = f"{key_prefix}-await-jobs"
    with st.container(border=True):
        st.markdown("**Загрузка документов**")
        uploaded_files = st.file_uploader(
            "Файлы",
            type=UPLOAD_TYPES,
            accept_multiple_files=True,
            key=f"{key_prefix}-file-uploader",
        )
        run_ingest = st.button(
            "Запустить ingest",
            type="primary",
            use_container_width=True,
            disabled=not uploaded_files,
            key=f"{key_prefix}-run-ingest",
        )
        if run_ingest:
            results: list[dict[str, Any]] = []
            for uploaded in uploaded_files:
                try:
                    result = upload_file(uploaded)
                except Exception as exc:
                    results.append({"filename": uploaded.name, "status": "failed", "error": _api_error_text(exc)})
                    continue
                if result.get("job_id"):
                    # Файл принят в очередь: дальше статус отдаёт GET /jobs/{job_id}
                    results.append(
                        {
                            "filename": result.get("filename") or uploaded.name,
                            "status": result.get("status") or "queued",
                            "job_id": result["job_id"],
                        }
                    )
                else:
                    # Дубликат или синхронный режим (INGEST_WORKERS=0): документ уже в базе
                    document = result.get("document") or {}
                    results.append(
                        {
                            "filename": document.get("filename") or uploaded.name,
                            "status": result.get("status") or "completed",
                            "evidence_units": result.get("evidence_units"),
                            "storage_uri": document.get("storage_uri"),
                        }
                    )
            st.session_state[state_key] = results
            st.session_state[wait_key] = _has_active_jobs(results)

        results = st.session_state.get(state_key) or []
        if _has_active_jobs(results):
            if st.session_state.pop(wait_key, False):
                # Автоожидание сразу после загрузки; клики по «Обновить статус»
                # дают по одной волне опроса без нового долгого цикла
                with st.spinner("Документ в обработке: извлечение фактов может занять несколько минут"):
                    deadline = time.monotonic() + JOB_POLL_TIMEOUT_S
                    while time.monotonic() < deadline:
                        _refresh_job_statuses(results)
                        if not _has_active_jobs(results):
                            break
                        time.sleep(JOB_POLL_INTERVAL_S)
            else:
                _refresh_job_statuses(results)
            st.session_state[state_key] = results
            if not _has_active_jobs(results):
                # Обработка завершилась — имена документов в списках устарели
                _document_names.clear()

        if _has_active_jobs(results):
            st.info("Документы ещё в обработке — статус можно проверить вручную.")
            st.button("Обновить статус", key=f"{key_prefix}-refresh-jobs")

        if results:
            st.dataframe(_upload_frame(results), use_container_width=True, hide_index=True)


def render_health() -> None:
    try:
        health = api_get("/health")
        st.markdown(
            " ".join(
                [
                    _pill("Backend", health.get("status")),
                    _pill("Extraction", health.get("extraction")),
                    _pill("PostgreSQL", health.get("postgres")),
                    _pill("Neo4j", health.get("neo4j")),
                    _pill("MinIO", health.get("minio")),
                    _llm_pill(health),
                ]
            ),
            unsafe_allow_html=True,
        )
    except Exception as exc:
        st.error(f"Backend недоступен: {exc}")


st.title("R&D GraphRAG: Норникель")
render_health()

with st.sidebar:
    st.header("Параметры запроса")
    selected_question = st.selectbox("Шаблон вопроса", QUESTION_TEMPLATES)
    include_hypotheses = st.toggle("Показывать гипотезы", value=True)
    confidence_min = st.slider("Минимальная уверенность", 0.0, 1.0, 0.0, 0.05)
    st.divider()
    st.caption(f"Backend: {BACKEND_URL}")

tab_query, tab_documents, tab_review, tab_facts, tab_graph = st.tabs(
    ["Запрос", "Документы", "Review", "Факты", "Граф"]
)

with tab_query:
    query_col, upload_col = st.columns([1.4, 1])
    with query_col:
        with st.container(border=True):
            st.markdown("**Вопрос**")
            question = st.text_area("Текст запроса", value=selected_question, height=120)
            ask_clicked = st.button("Получить ответ", type="primary", use_container_width=True)
            if ask_clicked:
                try:
                    with st.spinner("Сбор evidence pack"):
                        st.session_state["answer"] = api_post(
                            "/ask",
                            {
                                "question": question,
                                "include_hypotheses": include_hypotheses,
                                "confidence_min": confidence_min,
                            },
                            # Каскад LLM + веб-поиск на стороне backend занимает до минут
                            timeout=180,
                        )
                except Exception as exc:
                    # Предыдущий ответ в session_state не затирается
                    st.error(f"Не удалось получить ответ: {_api_error_text(exc)}")
    with upload_col:
        render_upload_panel("query")

    answer = st.session_state.get("answer")
    if answer:
        st.divider()
        render_answer(answer, include_hypotheses)

with tab_documents:
    render_upload_panel("documents")
    st.markdown("#### Загруженные документы")
    try:
        documents = api_get("/documents")
        frame = _docs_frame(documents)
        if frame.empty:
            st.info("Документы не найдены.")
        else:
            st.dataframe(frame, use_container_width=True, hide_index=True)
            st.markdown("#### Удаление документа")
            st.caption("Документ хранится, пока его не удалить здесь. Удаляется всё извлечённое: "
                       "фрагменты, факты, узлы графа; общие сущности остаются, если на них "
                       "ссылаются другие документы.")
            names = {f"{doc['filename']} ({doc['id']})": doc["id"] for doc in documents}
            selected = st.selectbox("Документ", list(names), key="delete-doc-select")
            confirm = st.checkbox("Подтверждаю удаление со всеми связями", key="delete-doc-confirm")
            if st.button("Удалить", type="primary", disabled=not confirm, key="delete-doc-btn"):
                result = api_delete(f"/documents/{names[selected]}")
                st.success(f"Удалено: фрагментов {result['fragments']}, "
                           f"кандидатов {result['candidates']}, фактов {result['facts']}")
                _document_names.clear()
                st.rerun()
    except Exception as exc:
        st.warning(f"Не удалось загрузить /documents: {exc}")

with tab_review:
    status_options = {
        "all": "Все",
        "pending_review": "На проверке",
        "approved": "Утвержденные",
        "rejected": "Отклоненные",
    }
    status_key = st.selectbox(
        "Статус кандидатов",
        list(status_options),
        index=list(status_options).index("pending_review"),
        format_func=status_options.get,
    )
    path = "/review/facts" if status_key == "all" else f"/review/facts?status={status_key}"
    try:
        candidates = api_get(path)
        c1, c2, c3 = st.columns(3)
        c1.metric("Всего", len(candidates))
        c2.metric("На проверке", sum(1 for item in candidates if item.get("status") == "pending_review"))
        c3.metric("Утверждено", sum(1 for item in candidates if item.get("status") == "approved"))

        for candidate in candidates[:30]:
            source = candidate.get("source") or {}
            payload = candidate.get("payload", {})
            summary = " · ".join(
                str(payload.get(key))
                for key in ("material", "process", "property")
                if payload.get(key) and payload.get(key) != "не указано"
            )
            title = f"{_status_label(candidate.get('status'))} · {summary or candidate.get('id')}"
            with st.expander(title):
                left, right = st.columns([2, 1])
                with left:
                    if source.get("quote"):
                        st.markdown(f"**Цитата из документа:**\n> {source['quote']}")
                    st.json(payload, expanded=False)
                with right:
                    st.metric("Confidence", f"{float(candidate.get('confidence') or 0):.0%}")
                    st.write(f"Тип: {TYPE_LABELS.get(candidate.get('type'), candidate.get('type'))}")
                    st.write(f"Файл: {_source_name(source.get('document_id'))}")
                    place = _place_label(source)
                    if place:
                        st.write(place)
                    section = _place_section(source)
                    if section:
                        st.write(f"Раздел: {section}")
                    if candidate.get("status") == "pending_review":
                        approve_col, reject_col = st.columns(2)
                        with approve_col:
                            if st.button("Approve", key=f"approve-{candidate.get('id')}", use_container_width=True):
                                api_post_empty(f"/review/facts/{candidate.get('id')}/approve")
                                st.rerun()
                        with reject_col:
                            if st.button("Reject", key=f"reject-{candidate.get('id')}", use_container_width=True):
                                api_post_empty(f"/review/facts/{candidate.get('id')}/reject")
                                st.rerun()
    except Exception as exc:
        st.warning(f"Не удалось загрузить /review/facts: {exc}")

with tab_facts:
    try:
        facts = api_get("/facts").get("facts", [])
        frame = _facts_frame(facts)
        if frame.empty:
            st.info("Факты не найдены.")
        else:
            filter_materials, filter_files = st.columns(2)
            selected_materials = filter_materials.multiselect(
                "Материалы",
                sorted(frame["Материал"].dropna().unique().tolist()) if "Материал" in frame else [],
                placeholder="Все материалы",
            )
            selected_files = filter_files.multiselect(
                "Файлы",
                sorted(frame["Файл"].dropna().unique().tolist()) if "Файл" in frame else [],
                placeholder="Все файлы",
            )
            filtered = frame
            if selected_materials:
                filtered = filtered[filtered["Материал"].isin(selected_materials)]
            if selected_files:
                filtered = filtered[filtered["Файл"].isin(selected_files)]
            f1, f2 = st.columns(2)
            f1.metric("Факты", len(filtered))
            f2.metric("Уникальные материалы", filtered["Материал"].nunique() if "Материал" in filtered else 0)
            st.dataframe(filtered, use_container_width=True, hide_index=True)
    except Exception as exc:
        st.warning(f"Не удалось загрузить /facts: {exc}")

with tab_graph:
    try:
        graph = api_get("/graph")
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        g1, g2 = st.columns(2)
        g1.metric("Узлы", len(nodes))
        g2.metric("Связи", len(edges))
        if nodes:
            st.graphviz_chart(graph_to_dot(graph), use_container_width=True)
            with st.expander("Graph payload"):
                st.json(graph, expanded=False)
        else:
            st.info("Граф пуст.")
    except Exception as exc:
        st.warning(f"Не удалось загрузить /graph: {exc}")
