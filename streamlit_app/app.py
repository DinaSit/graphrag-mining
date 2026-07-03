from __future__ import annotations

import os
from typing import Any

import pandas as pd
import requests
import streamlit as st


BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
DEFAULT_QUESTION = "Какие решения подходят для шахтных вод по сульфатам и какие есть источники?"
QUESTION_TEMPLATES = [
    DEFAULT_QUESTION,
    "Что делали со сплавом X при температуре 700-750 °C и как изменялась твёрдость?",
    "Какие технические решения организации циркуляции католита описаны и какая скорость потока считается оптимальной?",
    "Какие способы закачки шахтных вод в глубокие горизонты применялись и каковы их показатели?",
]
UPLOAD_TYPES = ["pdf", "txt", "csv", "md", "json", "docx", "docm", "pptx", "xlsx", "xlsm"]
STATUS_LABELS = {
    "pending_review": "На проверке",
    "approved": "Утверждено",
    "rejected": "Отклонено",
    "completed": "Готово",
    "processing": "В обработке",
    "failed": "Ошибка",
}


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
    </style>
    """,
    unsafe_allow_html=True,
)


def api_get(path: str) -> Any:
    response = requests.get(f"{BACKEND_URL}{path}", timeout=20)
    response.raise_for_status()
    return response.json()


def api_post(path: str, payload: dict[str, Any]) -> Any:
    response = requests.post(f"{BACKEND_URL}{path}", json=payload, timeout=120)
    response.raise_for_status()
    return response.json()


def api_post_empty(path: str) -> Any:
    response = requests.post(f"{BACKEND_URL}{path}", timeout=60)
    response.raise_for_status()
    return response.json()


def upload_file(file) -> Any:
    files = {"file": (file.name, file.getvalue(), file.type or "application/octet-stream")}
    data = {"document_type": file.name.split(".")[-1], "source_label": file.name, "access_level": "uploaded"}
    response = requests.post(f"{BACKEND_URL}/ingest", files=files, data=data, timeout=180)
    response.raise_for_status()
    return response.json()


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
        lines.append(f'{_dot_id(edge["source"])} -> {_dot_id(edge["target"])} [label="{_escape(edge.get("label", ""))}"];')
    lines.append("}")
    return "\n".join(lines)


def _dot_id(value: str) -> str:
    return "n_" + "".join(ch if ch.isalnum() else "_" for ch in value)


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')[:120]


def _status_label(value: Any) -> str:
    raw = value.value if hasattr(value, "value") else str(value)
    return STATUS_LABELS.get(raw, raw)


def _pill(label: str, value: Any) -> str:
    raw = str(value or "unknown")
    css = "status-ok" if raw in {"ok", "enabled", "remote", "completed"} else "status-warn"
    if raw in {"disabled", "failed", "memory", "unknown"}:
        css = "status-bad"
    return f'<span class="status-pill {css}">{label}: {raw}</span>'


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


def _experiments_frame(experiments: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in experiments:
        source = item.get("source") or {}
        rows.append(
            {
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
            "Документ": source.get("document_id"),
            "Версия": source.get("version_id"),
            "Фрагмент": source.get("fragment_id"),
            "Страница": source.get("page"),
            "Секция": source.get("section"),
            "Цитата": source.get("quote"),
        }
        for source in sources
    ]
    return pd.DataFrame(rows)


def _facts_frame(facts: list[dict[str, Any]]) -> pd.DataFrame:
    if not facts:
        return pd.DataFrame()
    frame = pd.DataFrame(facts)
    columns = ["id", "material", "property", "status", "confidence", "source"]
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


def render_list_block(title: str, items: list[str], empty_text: str) -> None:
    with st.container(border=True):
        st.markdown(f"**{title}**")
        values = items or [empty_text]
        for item in values:
            st.markdown(f"- {item}")


def render_answer(answer: dict[str, Any], include_hypotheses: bool) -> None:
    summary_col, metric_col = st.columns([4, 1])
    with summary_col:
        with st.container(border=True):
            st.markdown("**Ответ**")
            st.write(answer.get("summary") or "Ответ не сформирован.")
    with metric_col:
        st.metric("Confidence", f"{float(answer.get('confidence') or 0):.0%}")

    experiments = answer.get("experiments", [])
    sources = answer.get("sources", [])
    graph = answer.get("graph", {})
    node_count = len(graph.get("nodes", []))
    edge_count = len(graph.get("edges", []))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Факты", len(experiments))
    m2.metric("Источники", len(sources))
    m3.metric("Узлы графа", node_count)
    m4.metric("Связи", edge_count)

    if experiments:
        st.markdown("#### Факты и эксперименты")
        st.dataframe(_experiments_frame(experiments), use_container_width=True, hide_index=True)

    left, middle, right = st.columns(3)
    with left:
        render_list_block("Противоречия", answer.get("contradictions", []), "Не выявлены")
    with middle:
        render_list_block("Пробелы", answer.get("gaps", []), "Не выявлены")
    with right:
        hypothesis_text = "Включены в текущую выборку." if include_hypotheses else "Скрыты в текущей выборке."
        render_list_block("Гипотезы", [hypothesis_text], "Не выявлены")

    if sources:
        st.markdown("#### Источники")
        st.dataframe(_sources_frame(sources), use_container_width=True, hide_index=True)
        for index, source in enumerate(sources, start=1):
            title = f"{index}. {source.get('document_id')} · page {source.get('page')} · {source.get('fragment_id')}"
            with st.expander(title):
                st.write(source.get("quote") or "Нет цитаты")

    if node_count:
        st.markdown("#### Цепочка графа")
        st.graphviz_chart(graph_to_dot(graph), use_container_width=True)


def render_upload_panel(key_prefix: str) -> None:
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
            progress = st.progress(0, text="Обработка файлов")
            for index, uploaded in enumerate(uploaded_files, start=1):
                try:
                    result = upload_file(uploaded)
                    document = result["document"]
                    results.append(
                        {
                            "filename": document["filename"],
                            "status": result["status"],
                            "evidence_units": result["evidence_units"],
                            "storage_uri": document.get("storage_uri"),
                        }
                    )
                except Exception as exc:
                    results.append({"filename": uploaded.name, "status": "failed", "error": str(exc)})
                progress.progress(index / max(len(uploaded_files), 1), text=f"Обработано {index}/{len(uploaded_files)}")
            st.session_state[f"{key_prefix}-last-upload"] = results

        if st.session_state.get(f"{key_prefix}-last-upload"):
            st.dataframe(
                _upload_frame(st.session_state[f"{key_prefix}-last-upload"]),
                use_container_width=True,
                hide_index=True,
            )


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
                with st.spinner("Сбор evidence pack"):
                    st.session_state["answer"] = api_post(
                        "/ask",
                        {
                            "question": question,
                            "include_hypotheses": include_hypotheses,
                            "confidence_min": confidence_min,
                        },
                    )
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
    except Exception as exc:
        st.warning(f"Не удалось загрузить /documents: {exc}")

with tab_review:
    status_options = {
        "all": "Все",
        "pending_review": "На проверке",
        "approved": "Утвержденные",
        "rejected": "Отклоненные",
    }
    status_key = st.selectbox("Статус кандидатов", list(status_options), format_func=status_options.get)
    path = "/review/facts" if status_key == "all" else f"/review/facts?status={status_key}"
    try:
        candidates = api_get(path)
        c1, c2, c3 = st.columns(3)
        c1.metric("Всего", len(candidates))
        c2.metric("На проверке", sum(1 for item in candidates if item.get("status") == "pending_review"))
        c3.metric("Утверждено", sum(1 for item in candidates if item.get("status") == "approved"))

        for candidate in candidates[:30]:
            source = candidate.get("source") or {}
            title = f"{_status_label(candidate.get('status'))} · {candidate.get('id')} · {source.get('fragment_id', 'без источника')}"
            with st.expander(title):
                left, right = st.columns([2, 1])
                with left:
                    st.json(candidate.get("payload", {}), expanded=False)
                with right:
                    st.metric("Confidence", f"{float(candidate.get('confidence') or 0):.0%}")
                    st.write(f"Тип: `{candidate.get('type')}`")
                    st.write(f"Источник: `{source.get('document_id')}`")
                    st.write(f"Фрагмент: `{source.get('fragment_id')}`")
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
            f1, f2 = st.columns(2)
            f1.metric("Факты", len(facts))
            f2.metric("Уникальные материалы", frame["Материал"].nunique() if "Материал" in frame else 0)
            st.dataframe(frame, use_container_width=True, hide_index=True)
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
