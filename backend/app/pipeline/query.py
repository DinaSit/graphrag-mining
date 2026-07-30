"""Оркестратор ответа: сбор доказательной базы и сборка QueryResponse.

Здесь только последовательность шагов и решения о форме ответа. Сами шаги
живут в соседних модулях: маршрутизация — query_routing, отбор фактов —
fact_selection, обход графа — graph_traversal, числа — graph_traversal,
цитаты — citations, аналитика — answer_analysis, evidence pack и вызов
модели — evidence.

Две ветки сбора: быстрая (классический RAG) и полная (граф + планировщик);
обе заканчиваются CollectedEvidence, после чего finalize собирает ответ —
общий путь для /ask и /ask/stream.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.pipeline import answer_analysis, citations, fact_selection, graph_traversal
from app.pipeline import evidence as evidence_module
from app.pipeline.evidence import CollectedEvidence
from app.pipeline.llm_bridge import LLMUnavailableError
from app.pipeline.query_parsing import LLMQuestionParser
from app.pipeline.query_routing import OfftopicRouter, needs_full_pipeline, offtopic_response
from app.schemas import (
    ExperimentRow,
    Fact,
    GraphPayload,
    ParsedQuestion,
    QueryRequest,
    QueryResponse,
    SearchHit,
    SourceRef,
)
from app.storage import ApplicationStore

__all__ = ["CollectedEvidence", "QueryOrchestrator"]


class QueryOrchestrator:
    def __init__(self, store: ApplicationStore, question_parser: LLMQuestionParser | None = None):
        self.store = store
        self.question_parser = question_parser or LLMQuestionParser(normalizer=store.normalizer)
        self._router = OfftopicRouter(store.normalizer)

    # --- Маршрутизация (граница API) ---

    def is_offtopic(self, question: str) -> bool:
        return self._router.is_offtopic(question)

    def offtopic_response(self) -> QueryResponse:
        return offtopic_response(self.store)

    # --- Ответ ---

    async def answer(self, request: QueryRequest) -> QueryResponse:
        evidence = await self.collect_evidence(request)
        llm_answer: dict[str, Any] = {}
        try:
            llm_answer = await self._generate_answer(request.question, evidence.evidence_pack)
        except LLMUnavailableError as error:
            evidence.llm_errors.append(error.human())
        return self.finalize(evidence, llm_answer)

    async def _generate_answer(self, question: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
        """Точка подмены модели в тестах; обычный путь — evidence.generate_answer."""
        return await evidence_module.generate_answer(question, evidence_pack)

    def answer_messages(self, question: str, evidence_pack: dict[str, Any]) -> list[dict[str, str]]:
        """Сообщения генерации: /ask/stream отдаёт их в chat_stream сам."""
        return evidence_module.answer_messages(question, evidence_pack)

    async def collect_evidence(self, request: QueryRequest) -> CollectedEvidence:
        """Всё до генерации LLM: общая ступень для /ask и /ask/stream."""
        if not needs_full_pipeline(request):
            fast = await self._collect_fast(request)
            if fast is not None:
                return fast
        return await self._collect_full(request)

    async def _collect_fast(self, request: QueryRequest) -> CollectedEvidence | None:
        """Быстрая ветка — классический RAG: семантический поиск плюс пословный
        матчинг фактов, без LLM-планировщика и Cypher-обхода графа. Ни хитов,
        ни фактов => None (выполняется переход на полный пайплайн)."""
        try:
            # store.search блокирующий (urllib к сервису эмбеддингов) — в поток
            search_hits = await asyncio.to_thread(self.store.search, request.question, top_k=10)
        except Exception:
            search_hits = []
        facts = fact_selection.match_facts_by_words(self.store, request.question)
        if not search_hits and not facts:
            return None
        self._attach_filenames(search_hits)
        experiments = [fact_selection.row_from_fact(fact) for fact in facts[:20]]
        # Источники: сперва факты, затем поисковые хиты (dedup по фрагменту)
        sources: list[SourceRef] = []
        seen: set[tuple[str, str]] = set()
        for source in [fact.source for fact in facts] + [hit.source for hit in search_hits]:
            key = (source.document_id, source.fragment_id)
            if key not in seen:
                sources.append(source)
                seen.add(key)
        contradictions = answer_analysis.find_contradictions(self.store.normalizer, facts)
        evidence_pack, citation_index = evidence_module.build_evidence_pack(None, facts, [], search_hits, contradictions, [])
        return CollectedEvidence(
            request=request,
            pipeline_mode="fast",
            llm_errors=[],
            parsed=None,
            facts=facts,
            numeric_evidence=[],  # числовые данные обрабатываются только полным пайплайном
            search_hits=search_hits,
            has_direct_facts=bool(facts),
            related_facts=[],
            hypotheses=[],
            experiments=experiments,
            sources=sources,
            contradictions=contradictions,
            gaps=[],
            graph=self.store.get_graph(facts=facts[:20]),
            confidence=fact_selection.mean_confidence(facts),
            evidence_pack=evidence_pack,
            citation_index=citation_index,
        )

    async def _collect_full(self, request: QueryRequest) -> CollectedEvidence:
        llm_errors: list[str] = []

        # Семантический поиск не зависит от LLM и стартует параллельно с разбором
        # вопроса: обе операции сетевые, последовательное выполнение лишь
        # увеличивает задержку. store.search запрашивает эмбеддинг блокирующим
        # urllib-запросом, поэтому выносится в поток — иначе зависший сервис
        # эмбеддингов останавливает весь event loop вместе с /health.
        search_task = asyncio.create_task(
            asyncio.to_thread(self.store.search, request.question, top_k=10)
        )

        # Отказ LLM на любой ступени не скрывается: причина накапливается в
        # llm_errors и показывается пользователю, а ответ собирается из того,
        # что доступно без модели (граф, факты, семантический поиск).
        parsed: ParsedQuestion | None = None
        try:
            parsed = await self.question_parser.parse_question(request.question)
        except LLMUnavailableError as error:
            llm_errors.append(error.human())
        except Exception:
            search_task.cancel()
            raise

        facts: list[Fact] = []
        numeric_evidence: list[dict[str, Any]] = []
        claim_ids: set[str] = set()
        dropped_by_year = 0
        if parsed is not None:
            claim_ids = graph_traversal.graph_traverse(self.store, parsed)
            # Claim'ы из Neo4j сверяются с видимостью: граф при скрытии
            # документа не перестраивается, фильтрует backend
            graph_facts = [
                self.store.facts[cid] for cid in claim_ids
                if cid in self.store.facts and self.store.is_visible_fact(self.store.facts[cid])
            ]
            legacy_facts = fact_selection.filter_facts_legacy(
                self.store.normalizer, self.store.visible_facts(), request, parsed
            )
            facts = fact_selection.merge_unique(graph_facts, legacy_facts)
            facts_before_year = len(facts)
            facts = fact_selection.filter_by_year(self.store, facts, parsed)
            dropped_by_year = facts_before_year - len(facts)
            facts = sorted(facts, key=fact_selection.rank_fact(self.store.normalizer, parsed), reverse=True)
            numeric_evidence = graph_traversal.numeric_condition_matches(self.store, parsed, claim_ids)

        try:
            search_hits = await search_task
        except Exception:
            search_hits = []
        # Срок из вопроса ограничивает ВЕСЬ материал ответа, а не только факты:
        # фрагмент из документа вне диапазона — такой же неподходящий источник
        if parsed is not None:
            search_hits = [
                hit for hit in search_hits
                if fact_selection.in_year_range(self.store, hit.source.document_id, parsed)
            ]
            numeric_evidence = [
                m for m in numeric_evidence
                if m["source"] is None
                or fact_selection.in_year_range(self.store, m["source"].document_id, parsed)
            ]
        self._attach_filenames(search_hits)

        has_direct_facts = bool(facts or numeric_evidence)
        related_facts: list[Fact] = []
        hypotheses: list[str] = []
        if not has_direct_facts and parsed is not None:
            related_facts, hypotheses = fact_selection.indirect_search(self.store, parsed)
            related_facts = fact_selection.filter_by_year(self.store, related_facts, parsed)

        experiments = [fact_selection.row_from_fact(fact) for fact in facts[:20]]
        sources = fact_selection.collect_sources(facts)
        contradictions = answer_analysis.find_contradictions(self.store.normalizer, facts)
        gaps = answer_analysis.find_gaps(parsed, facts, numeric_evidence)
        # Отсев по сроку не должен быть незаметным: иначе ответ выглядит бедным
        # без объяснения, почему часть базы в него не вошла
        if dropped_by_year:
            gaps.append(f"По заданному сроку издания отброшено фактов: {dropped_by_year} "
                        f"(включая источники, у которых год не определён).")
        graph = self.store.get_graph(facts=facts[:20])
        confidence = fact_selection.mean_confidence(facts)
        evidence_pack, citation_index = evidence_module.build_evidence_pack(
            parsed, facts, numeric_evidence, search_hits, contradictions, gaps
        )

        return CollectedEvidence(
            request=request,
            pipeline_mode="full",
            llm_errors=llm_errors,
            parsed=parsed,
            facts=facts,
            numeric_evidence=numeric_evidence,
            search_hits=search_hits,
            has_direct_facts=has_direct_facts,
            related_facts=related_facts,
            hypotheses=hypotheses,
            experiments=experiments,
            sources=sources,
            contradictions=contradictions,
            gaps=gaps,
            graph=graph,
            confidence=confidence,
            evidence_pack=evidence_pack,
            citation_index=citation_index,
        )

    def _attach_filenames(self, search_hits: list[SearchHit]) -> None:
        for hit in search_hits:
            document = self.store.documents.get(hit.source.document_id)
            if document:
                hit.metadata = {**hit.metadata, "filename": document.filename}

    def _scientific_share(self, notes: list[SourceRef]) -> float | None:
        """Научность ответа = научные сноски / все сноски (0..1, 2 знака).

        Как и уверенность, это параметр САМОГО ОТВЕТА: считается один раз по
        его сноскам (процитированным фрагментам), а не по найденной подборке.
        Каждая сноска ссылается на документ — научный или нет; сноска на
        документ без вычисленного признака (is_scientific=None) считается
        не научной, но остаётся в знаменателе. Ответ без сносок — None («—»
        в интерфейсе): доля не определена, а не ноль.
        """
        if not notes:
            return None
        scientific = 0
        for note in notes:
            document = self.store.documents.get(note.document_id)
            if document is not None and document.is_scientific:
                scientific += 1
        return round(scientific / len(notes), 2)

    def finalize(self, evidence: CollectedEvidence, llm_answer: dict[str, Any]) -> QueryResponse:
        """Вся пост-LLM сборка QueryResponse (общая для /ask и /ask/stream)."""
        facts = evidence.facts
        experiments = evidence.experiments
        sources = evidence.sources
        graph = evidence.graph
        confidence = evidence.confidence
        contradictions = evidence.contradictions
        gaps = evidence.gaps
        hypotheses = evidence.hypotheses
        related_facts = evidence.related_facts
        has_direct_facts = evidence.has_direct_facts
        search_hits = evidence.search_hits

        summary = llm_answer.get("summary") or answer_analysis.degraded_summary(
            facts, search_hits, has_direct_facts, bool(evidence.llm_errors)
        )
        contradictions = answer_analysis.merge_texts(contradictions, llm_answer.get("contradictions"))
        gaps = answer_analysis.merge_texts(gaps, llm_answer.get("gaps"))
        hypotheses = answer_analysis.merge_texts(hypotheses, llm_answer.get("hypotheses"))

        # Перевод номерных цитат модели ([3]) в канонические id ([fragment-…])
        # по citation_index — до всего остального. Модель надёжно воспроизводит
        # номера, но искажает длинные id; перевод устраняет ситуацию «фрагмент
        # вне списка» и сохраняет точное соответствие источников сноскам.
        index = evidence.citation_index
        summary = citations.translate_markers(summary, index)
        contradictions = [citations.translate_markers(text, index) for text in contradictions]
        gaps = [citations.translate_markers(text, index) for text in gaps]
        hypotheses = [citations.translate_markers(text, index) for text in hypotheses]
        # Дубль пометки «Косвенно»: бейдж ставит UI, текстовые префиксы модели
        # удаляются; повторный dict.fromkeys — после удаления префиксов разные
        # варианты могли стать одинаковыми
        hypotheses = list(dict.fromkeys(citations.strip_hypothesis_prefix(text) for text in hypotheses))

        # Процитированные фрагменты из ВИДИМЫХ секций (summary, contradictions,
        # gaps, hypotheses). По ним не режется список источников (инфобокс
        # показывает полноту) — они нужны для сносок, научности и сборки
        # смежного графа.
        cited_texts = [summary, *contradictions, *gaps, *hypotheses]
        cited = citations.cited_sources(self.store, cited_texts, sources)

        # Пустой пословный fast: прямота — только по вердикту модели.
        if evidence.pipeline_mode == "fast" and not evidence.facts:
            has_direct_facts = llm_answer.get("sufficient") is True

        # Вердикт модели имеет наивысший приоритет: sufficient=false
        # означает «прямого ответа нет, материал смежный» — и ВСЁ найденное
        # оформляется как смежное, а не выдаётся за прямой ответ:
        # факты (в т.ч. привязанные к процитированным фрагментам) переносятся в
        # «Смежные данные», таблица прямых фактов пустая, основной граф пустой
        # (граф смежных данных отдаёт related_graph — UI подписывает его),
        # уверенность считается по СМЕЖНЫМ фактам — нулевое значение рядом с
        # непустым графом вводило бы в заблуждение. Найденные источники (sources)
        # не очищаются: это перечень использованных материалов, инфобокс
        # показывает полноту.
        if llm_answer.get("sufficient") is False:
            cited_fragment_ids = {source.fragment_id for source in cited}
            linked_facts = [
                fact for fact in self.store.visible_facts()
                if fact.source.fragment_id in cited_fragment_ids
            ]
            related_facts = fact_selection.merge_unique(
                fact_selection.merge_unique(related_facts, facts), linked_facts
            )
            facts = []
            experiments = []
            graph = self.store.get_graph(facts=[])
            has_direct_facts = False
            confidence = fact_selection.mean_confidence(related_facts)

        related_experiments, related_sources, related_graph = self._related_views(related_facts)

        # Сноски ответа в том составе, в котором их видит читатель: цитаты из
        # текста (cited) + сноски строк таблиц «Прямые факты» и «Смежные данные»
        # (UI нумерует и их), дедупликация по фрагменту в порядке появления.
        # По ЭТОМУ списку считается научность
        answer_notes = list(cited)
        noted_fragments = {note.fragment_id for note in answer_notes}
        for row in [*experiments, *related_experiments]:
            if row.source.fragment_id not in noted_fragments:
                noted_fragments.add(row.source.fragment_id)
                answer_notes.append(row.source)
        status = answer_analysis.evidence_status(has_direct_facts, related_facts, search_hits)

        # sources ответа = найденный набор (топ-12) БЕЗ фильтра по цитированию —
        # инфобокс «Об этом ответе» показывает фактическую полноту, а не только
        # процитированное. Правило «источников не больше сносок» относится ТОЛЬКО
        # к списку Примечаний внизу ответа (UI строит его из цитат в тексте).
        # Кроме того, каждый процитированный фрагмент гарантированно включён
        # (даже за пределами топ-12), иначе его сноска в тексте не резолвится и
        # UI покажет «фрагмент вне списка». После понижения ответа до смежного
        # (sufficient=false) sources пуст (cited тоже пуст).
        final_sources = list(sources[:12])
        seen_fragments = {source.fragment_id for source in final_sources}
        for source in cited:
            if source.fragment_id not in seen_fragments:
                seen_fragments.add(source.fragment_id)
                final_sources.append(source)

        # ПРЯМОЙ ответ с пустым графом, но с источниками — строим граф из фактов,
        # привязанных к этим источникам (карта того, на что ответ опирается).
        # Для смежного ответа (sufficient=false) сюда не заходим: его граф уже
        # собран в related_graph, основной остаётся пустым.
        if llm_answer.get("sufficient") is not False and not graph.nodes and final_sources:
            source_fragment_ids = {source.fragment_id for source in final_sources}
            linked_facts = [
                fact for fact in self.store.visible_facts()
                if fact.source.fragment_id in source_fragment_ids
            ]
            if linked_facts:
                graph = self.store.get_graph(facts=linked_facts[:20])

        return QueryResponse(
            summary=summary,
            experiments=experiments,
            sources=final_sources,
            graph=graph,
            contradictions=contradictions,
            gaps=gaps,
            confidence=confidence,
            hypotheses=hypotheses,
            llm_error="; ".join(dict.fromkeys(evidence.llm_errors)) or None,
            search_hits=search_hits[:8],
            has_direct_facts=has_direct_facts,
            related_experiments=related_experiments,
            related_sources=related_sources,
            related_graph=related_graph,
            evidence_status=status,
            pipeline_mode=evidence.pipeline_mode,
            # Научность — параметр ОТВЕТА (как уверенность): научные сноски /
            # все сноски, по списку answer_notes (текст + таблицы) — ровно то,
            # что читатель видит в разделе «Источники»; без сносок — None («—»)
            scientific_share=self._scientific_share(answer_notes),
        )

    def _related_views(self, related_facts: list[Fact]) -> tuple[list[ExperimentRow], list[SourceRef], GraphPayload]:
        """Представления «Смежных данных» (таблица, источники топ-12, граф) —
        одна сборка для финального ответа и SSE-предпросмотра: единая точка
        исключает расхождение встроенных копий (в частности, в месте применения
        ограничения [:12])."""
        return (
            [fact_selection.row_from_fact(fact) for fact in related_facts[:20]],
            fact_selection.collect_sources(related_facts)[:12],
            self.store.get_graph(facts=related_facts[:20]),
        )

    def evidence_preview(self, evidence: CollectedEvidence) -> dict[str, Any]:
        """Полезная нагрузка SSE-события "evidence": всё, что
        готово до генерации, в формате полей QueryResponse."""
        related_experiments, related_sources, related_graph = self._related_views(evidence.related_facts)
        status = answer_analysis.evidence_status(
            evidence.has_direct_facts, evidence.related_facts, evidence.search_hits
        )
        return {
            "experiments": [row.model_dump(mode="json") for row in evidence.experiments],
            "sources": [source.model_dump(mode="json") for source in evidence.sources[:12]],
            "search_hits": [hit.model_dump(mode="json") for hit in evidence.search_hits[:8]],
            "related_experiments": [row.model_dump(mode="json") for row in related_experiments],
            "related_sources": [source.model_dump(mode="json") for source in related_sources[:12]],
            # UI рисует карточки «Узлы графа/Связи» уже на событии evidence
            "graph": evidence.graph.model_dump(mode="json"),
            "related_graph": related_graph.model_dump(mode="json"),
            "contradictions": evidence.contradictions,
            "gaps": evidence.gaps,
            # Гипотезы косвенного поиска готовы до генерации (indirect_search) —
            # ждать модель, чтобы их показать, незачем; её собственные
            # добавляются к этим в finalize
            "hypotheses": evidence.hypotheses,
            "confidence": evidence.confidence,
            "has_direct_facts": evidence.has_direct_facts,
            "evidence_status": status,
            "pipeline_mode": evidence.pipeline_mode,
            # Научность считается по СНОСКАМ ответа, а сносок до генерации не
            # существует — предпросмотр возвращает None («—»), значение заполняет
            # финальный ответ. Доля по найденной подборке не используется: она
            # расходилась бы с финальным значением между стримом и финалом.
            "scientific_share": None,
        }
