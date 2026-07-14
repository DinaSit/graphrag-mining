from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from itertools import combinations
from statistics import mean
from typing import Any

from app.pipeline.normalization import canonical_text, direction_label, normalize_effect_direction
from app.pipeline.query_parsing import LLMQuestionParser
from app.pipeline.validation import normalize_for_quantity, normalize_quantity
from app.schemas import (
    CandidateStatus,
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

# LLM доступна из backend только по HTTP через сервис ml-extraction
from app.pipeline.llm_bridge import LLMUnavailableError, chat_json

GRAPH_LIMIT = 40

# Маркеры сравнения в вопросе — единственный текстовый признак полного
# пайплайна помимо цифр (см. _needs_full_pipeline; дополнительные rule-шаблоны
# намеренно не вводятся)
_COMPARE_MARKERS_RE = re.compile(r"сравн|отлич| vs |против")

# Цитаты-иды в тексте LLM: [fragment-…] / [claim-…] / (fragment-…), в т.ч.
# списком через запятую — тот же формат, что подсвечивает UI (CITE_RE в index.html)
_CITATION_RE = re.compile(
    r"[\[(]\s*((?:fragment|claim)-[\w./-]+(?:\s*,\s*(?:fragment|claim)-[\w./-]+)*)\s*[\])]"
)

# Номерные цитаты модели: [3] / [3, 7] / диапазон [5–10] (тире/дефис — модель
# иногда сжимает подряд идущие номера вопреки промпту). Переводятся в
# канонические [fragment-…] по citation_index; скобки, где ни один номер
# не резолвится (напр. год [2023] или диапазон лет), не трогаются.
_MARKER_RE = re.compile(
    r"\[\s*(\d+(?:\s*[–—-]\s*\d+)?(?:\s*,\s*\d+(?:\s*[–—-]\s*\d+)?)*)\s*\]"
)
_MARKER_RANGE_RE = re.compile(r"^(\d+)\s*[–—-]\s*(\d+)$")

# Максимальная ширина разворачиваемого диапазона [a–b]: защита от некорректных
# значений — диапазона лет [1998–2005] или галлюцинации [1–999]
# (evidence pack ≤ 25 позиций)
_MARKER_RANGE_CAP = 25

# Служебные префиксы гипотез («Непрямая гипотеза:», «Гипотеза (косвенная):»,
# «Косвенно:», «Гипотеза:», …): пометку «Косвенно» ставит UI, текстовый дубль
# от модели срезается в _finalize (вторая линия защиты после ANSWER_SYSTEM_PROMPT)
_HYPOTHESIS_PREFIX_RE = re.compile(
    r"^\s*(?:(?:(?:не)?прямая|косвенная)\s+гипотеза"
    r"|гипотеза(?:\s*\(\s*(?:(?:не)?прямая|косвенная)\s*\))?"
    r"|косвенно)\s*:\s*",
    re.IGNORECASE,
)


def _strip_hypothesis_prefix(text: str) -> str:
    """Срезает у гипотезы ведущий служебный префикс и переводит первую букву в
    верхний регистр; текст без префикса (или состоящий из одного префикса)
    возвращается без изменений."""
    stripped, count = _HYPOTHESIS_PREFIX_RE.subn("", text or "", count=1)
    stripped = stripped.lstrip()
    if not count or not stripped:
        return text or ""
    return stripped[0].upper() + stripped[1:]

ANSWER_SYSTEM_PROMPT = """Ты формируешь ответ пользователю строго на основе предоставленного evidence pack.
Тебе ЗАПРЕЩЕНО использовать любые знания вне evidence pack. Если данных недостаточно — прямо скажи об этом.
Ответь JSON без пояснений. Ключ "summary" ОБЯЗАН идти ПЕРВЫМ ключом объекта:
{
  "summary": "2-4 предложения с ответом по существу, с указанием диапазонов значений и источников по id",
  "sufficient": true|false — хватает ли evidence pack для ПРЯМОГО ответа на вопрос (false, если данные лишь смежные или их нет),
  "confirmed": ["короткий подтверждённый вывод", ...],
  "contradictions": ["описание противоречия между источниками", ...],
  "gaps": ["чего не хватает в данных", ...],
  "hypotheses": ["текст гипотезы на основе косвенных данных БЕЗ пометок вроде «непрямая/косвенная гипотеза:» — пометку ставит интерфейс", ...]
}
Правила цитирования (СТРОГО):
- у каждого элемента evidence pack (facts и search_hits) есть числовой номер в поле "n";
- ссылка на источник — ТОЛЬКО этот номер в квадратных скобках: [3];
- несколько источников — в одних скобках через запятую: [3, 7]; подряд идущие тоже ТОЛЬКО через запятую [5, 6, 7] — диапазоны вида [5–10] ЗАПРЕЩЕНЫ;
- используй ТОЛЬКО номера, реально присутствующие в evidence pack; НЕ придумывай номера;
- НЕ пиши id фрагментов, названия файлов или текстовые ссылки — только номер;
- каждый вывод в confirmed и каждое противоречие в contradictions заканчивай такой ссылкой."""


@dataclass
class CollectedEvidence:
    """Всё, что собрано ДО генерации LLM: общая ступень для /ask и /ask/stream.

    llm_errors мутируется дальше по конвейеру: сюда дописывается отказ
    генерации, чтобы _finalize показал причину пользователю.
    """

    request: QueryRequest
    pipeline_mode: str  # "fast" — классический RAG, "full" — граф + планировщик
    llm_errors: list[str]
    parsed: ParsedQuestion | None
    facts: list[Fact]
    numeric_evidence: list[dict[str, Any]]
    search_hits: list[SearchHit]
    has_direct_facts: bool
    related_facts: list[Fact]
    hypotheses: list[str]
    experiments: list[ExperimentRow]
    sources: list[SourceRef]
    contradictions: list[str]
    gaps: list[str]
    graph: GraphPayload
    confidence: float
    evidence_pack: dict[str, Any]
    # Карта номерных цитат, показанных модели, → id фрагмента: номер "3" в
    # ответе LLM (см. ANSWER_SYSTEM_PROMPT) переводится в канонический
    # [fragment-…] на этапе _finalize. Номера надёжно воспроизводятся моделью,
    # тогда как длинные id она искажает (дописывает несуществующие суффиксы -N).
    citation_index: dict[str, str] = field(default_factory=dict)


def _index_token(token: str, exact: set[str], prefixes: set[str]) -> None:
    """Единое правило индексации термина: токен до 4 букв — в точные совпадения,
    длиннее — 5-буквенный префикс (покрывает морфологические формы). Общая точка
    для словаря доменной лексики и пословного матчинга фактов — правила обязаны
    совпадать, иначе маршрутизация вопросов вне предметной области и матчинг
    перестанут быть согласованными."""
    if len(token) <= 4:
        exact.add(token)
    else:
        prefixes.add(token[:5])


class QueryOrchestrator:
    # Стемы базовой R&D-лексики: словарь синонимов покрывает термины домена,
    # стемы добавляют морфологию и общеисследовательские слова, которых в нём нет
    _DOMAIN_STEMS = frozenset({
        "метод", "экспер", "исслед", "публика", "отчет", "лаборат", "температур",
        "концентрац", "скорост", "давлен", "расход", "материал", "процесс",
        "оборудован", "установк", "технолог", "параметр", "режим", "источник",
        "статья", "патент", "вывод", "эффект", "практик", "раствор", "очистк",
        "вода", "воды", "руда", "руды", "металл", "сплав", "шлак", "штейн",
    })

    def __init__(self, store: ApplicationStore, question_parser: LLMQuestionParser | None = None):
        self.store = store
        self.question_parser = question_parser or LLMQuestionParser(normalizer=store.normalizer)
        self._domain_terms: tuple[set[str], set[str]] | None = None

    def _domain_vocabulary(self) -> tuple[set[str], set[str]]:
        """Термины домена из словаря синонимов: (короткие — точное совпадение,
        5-буквенные префиксы длинных — матчат морфологические формы)."""
        if self._domain_terms is None:
            exact: set[str] = set()
            prefixes: set[str] = set()
            for alias, canonical in getattr(self.store.normalizer, "aliases", {}).items():
                for term in (alias, canonical):
                    for word in re.findall(r"[a-zа-я0-9]+", term.lower().replace("ё", "е")):
                        _index_token(word, exact, prefixes)
            self._domain_terms = (exact, prefixes)
        return self._domain_terms

    def is_offtopic(self, question: str) -> bool:
        """Разговорные вопросы и вопросы вне предметной области («как дела?»)
        не обрабатываются полным пайплайном.

        Проверяется на границе API (/ask), а не внутри answer(): пайплайн
        остаётся полным для прямых вызовов. Эвристика ошибается только в
        безопасную сторону: цифры, единицы, длинный текст или любой доменный
        термин отправляют вопрос в пайплайн.
        """
        text = question.strip().lower().replace("ё", "е")
        if not text:
            return True
        if any(ch.isdigit() for ch in text):
            return False
        if len(text) > 80:
            return False
        exact, prefixes = self._domain_vocabulary()
        for token in re.findall(r"[a-zа-я0-9]+", text):
            if len(token) <= 4:
                if token in exact:
                    return False
            elif token[:5] in prefixes or any(token.startswith(stem) for stem in self._DOMAIN_STEMS):
                return False
        return True

    def offtopic_response(self) -> QueryResponse:
        empty_graph = self.store.get_graph(facts=[])
        return QueryResponse(
            summary=(
                "Вопрос не похож на запрос к базе знаний, поэтому полный поиск не запускался. "
                "Я отвечаю на вопросы по горно-металлургическим R&D-материалам: методы, материалы, "
                "процессы, параметры, эксперименты, источники. Например: «Какие методы обессоливания "
                "воды подходят, если сульфаты 200–300 мг/л, а требуемый сухой остаток ≤1000 мг/дм³?»"
            ),
            experiments=[],
            sources=[],
            graph=empty_graph,
            contradictions=[],
            gaps=[],
            confidence=0.0,
            evidence_status="none",
            offtopic=True,
        )

    async def answer(self, request: QueryRequest) -> QueryResponse:
        evidence = await self._collect_evidence(request)
        llm_answer: dict[str, Any] = {}
        try:
            llm_answer = await self._generate_answer(request.question, evidence.evidence_pack)
        except LLMUnavailableError as error:
            evidence.llm_errors.append(error.human())
        return self._finalize(evidence, llm_answer)

    def _needs_full_pipeline(self, request: QueryRequest) -> bool:
        """Единственный признак маршрутизации fast/full (без rule-шаблонов):
        цифры, маркеры сравнения или явные фильтры => полный пайплайн."""
        text = request.question.lower().replace("ё", "е")
        if any(ch.isdigit() for ch in text):
            return True
        if _COMPARE_MARKERS_RE.search(text):
            return True
        filters = request.filters
        return bool(filters.materials or filters.properties or filters.laboratories or filters.confidence_min > 0)

    async def _collect_evidence(self, request: QueryRequest) -> CollectedEvidence:
        """Всё до генерации LLM: общая ступень для /ask и /ask/stream."""
        if not self._needs_full_pipeline(request):
            fast = await self._collect_fast(request)
            if fast is not None:
                return fast
        return await self._collect_full(request)

    def _match_facts_by_words(self, question: str) -> list[Fact]:
        """Пословный матчинг фактов без LLM-планировщика (быстрая ветка):
        токены вопроса ищутся напрямую по вершинам фактов
        (material/process/property). Короткие токены (<=4) — точное совпадение,
        длинные — по 5-буквенному префиксу (покрывает морфологию); токены из
        словаря синонимов раскрываются каноном, чтобы «electrowinning» находил
        факты про электроэкстракцию."""
        aliases = {
            canonical_text(alias): canonical
            for alias, canonical in getattr(self.store.normalizer, "aliases", {}).items()
        }
        exact: set[str] = set()
        prefixes: set[str] = set()
        text = question.lower().replace("ё", "е")
        for token in re.findall(r"[a-zа-я0-9]+", text):
            canonical = aliases.get(token)
            # Одно-двухбуквенные токены — шум («в», «на»), кроме аббревиатур
            # из словаря синонимов (например, «ВП»)
            if len(token) <= 2 and canonical is None:
                continue
            _index_token(token, exact, prefixes)
            if canonical is not None:
                for word in re.findall(r"[a-zа-я0-9]+", canonical.lower().replace("ё", "е")):
                    _index_token(word, exact, prefixes)

        if not exact and not prefixes:
            return []
        matched: list[Fact] = []
        # visible_facts: скрытые документы не участвуют в матчинге
        for fact in self.store.visible_facts():
            haystack = f"{fact.material} {fact.process} {fact.property}".lower().replace("ё", "е")
            for token in re.findall(r"[a-zа-я0-9]+", haystack):
                if token in exact or token[:5] in prefixes:
                    matched.append(fact)
                    break
        matched.sort(key=lambda fact: fact.confidence, reverse=True)
        return matched[:30]

    async def _collect_fast(self, request: QueryRequest) -> CollectedEvidence | None:
        """Быстрая ветка — классический RAG: семантический поиск плюс пословный
        матчинг фактов, без LLM-планировщика и Cypher-обхода графа. Ни хитов,
        ни фактов => None (выполняется переход на полный пайплайн)."""
        try:
            # store.search блокирующий (urllib к сервису эмбеддингов) — в поток
            search_hits = await asyncio.to_thread(self.store.search, request.question, top_k=10)
        except Exception:
            search_hits = []
        facts = self._match_facts_by_words(request.question)
        if not search_hits and not facts:
            return None
        self._attach_filenames(search_hits)
        experiments = [self._row_from_fact(fact) for fact in facts[:20]]
        # Источники: сперва факты, затем поисковые хиты (dedup по фрагменту)
        sources: list[SourceRef] = []
        seen: set[tuple[str, str]] = set()
        for source in [fact.source for fact in facts] + [hit.source for hit in search_hits]:
            key = (source.document_id, source.fragment_id)
            if key not in seen:
                sources.append(source)
                seen.add(key)
        contradictions = self._find_contradictions(facts)
        evidence_pack, citation_index = self._build_evidence_pack(None, facts, [], search_hits, contradictions, [])
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
            confidence=_mean_confidence(facts),
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
        if parsed is not None:
            claim_ids = self._graph_traverse(parsed)
            # Claim'ы из Neo4j сверяются с видимостью: граф при скрытии
            # документа не перестраивается, фильтрует backend
            graph_facts = [
                self.store.facts[cid] for cid in claim_ids
                if cid in self.store.facts and self.store.is_visible_fact(self.store.facts[cid])
            ]
            legacy_facts = self._filter_facts_legacy(self.store.visible_facts(), request, parsed)
            facts = self._merge_unique(graph_facts, legacy_facts)
            facts = sorted(facts, key=self._rank_fact(parsed), reverse=True)
            numeric_evidence = self._numeric_condition_matches(parsed, claim_ids)

        try:
            search_hits = await search_task
        except Exception:
            search_hits = []
        self._attach_filenames(search_hits)

        has_direct_facts = bool(facts or numeric_evidence)
        related_facts: list[Fact] = []
        hypotheses: list[str] = []
        if not has_direct_facts and parsed is not None:
            related_facts, hypotheses = self._indirect_search(parsed)

        experiments = [self._row_from_fact(fact) for fact in facts[:20]]
        sources = self._collect_sources(facts)
        contradictions = self._find_contradictions(facts)
        gaps = self._find_gaps(parsed, facts, numeric_evidence)
        graph = self.store.get_graph(facts=facts[:20])
        confidence = _mean_confidence(facts)
        evidence_pack, citation_index = self._build_evidence_pack(
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

    def _finalize(self, evidence: CollectedEvidence, llm_answer: dict[str, Any]) -> QueryResponse:
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

        summary = llm_answer.get("summary") or self._degraded_summary(
            facts, search_hits, has_direct_facts, bool(evidence.llm_errors)
        )
        contradictions = _merge_texts(contradictions, llm_answer.get("contradictions"))
        gaps = _merge_texts(gaps, llm_answer.get("gaps"))
        hypotheses = _merge_texts(hypotheses, llm_answer.get("hypotheses"))

        # Перевод номерных цитат модели ([3]) в канонические id ([fragment-…])
        # по citation_index — до всего остального. Модель надёжно воспроизводит
        # номера, но искажает длинные id; перевод устраняет ситуацию «фрагмент
        # вне списка» и сохраняет точное соответствие источников сноскам.
        index = evidence.citation_index
        summary = self._translate_markers(summary, index)
        contradictions = [self._translate_markers(text, index) for text in contradictions]
        gaps = [self._translate_markers(text, index) for text in gaps]
        hypotheses = [self._translate_markers(text, index) for text in hypotheses]
        # Дубль пометки «Косвенно»: бейдж ставит UI, текстовые префиксы модели
        # удаляются; повторный dict.fromkeys — после удаления префиксов разные
        # варианты могли стать одинаковыми
        hypotheses = list(dict.fromkeys(_strip_hypothesis_prefix(text) for text in hypotheses))

        # Процитированные фрагменты из ВИДИМЫХ секций (summary/contradictions/gaps/
        # hypotheses; confirmed UI не показывает). По ним не режется список
        # источников (инфобокс показывает полноту) — они нужны для сносок,
        # научности и сборки смежного графа.
        cited_texts = [summary, *contradictions, *gaps, *hypotheses]
        cited = self._cited_sources(cited_texts, sources)

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
            related_facts = self._merge_unique(self._merge_unique(related_facts, facts), linked_facts)
            facts = []
            experiments = []
            graph = self.store.get_graph(facts=[])
            has_direct_facts = False
            confidence = _mean_confidence(related_facts)

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
        evidence_status = _evidence_status(has_direct_facts, related_facts, search_hits)

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
            evidence_status=evidence_status,
            pipeline_mode=evidence.pipeline_mode,
            # Научность — параметр ОТВЕТА (как уверенность): научные сноски /
            # все сноски, по списку answer_notes (текст + таблицы) — ровно то,
            # что читатель видит в разделе «Источники»; без сносок — None («—»)
            scientific_share=self._scientific_share(answer_notes),
        )

    def _translate_markers(self, text: str, index: dict[str, str]) -> str:
        """Переводит номерные цитаты модели ([3] / [3, 7] / [5–10]) в канонические
        id фрагментов ([fragment-…]) по citation_index. Диапазон разворачивается
        в номера (5, 6, …, 10) с капом ширины _MARKER_RANGE_CAP. Номера, которых
        нет в индексе, отбрасываются; если в скобках не резолвится ни один номер —
        скобка остаётся как есть (это может быть год [2023] или диапазон лет)."""
        if not text or not index:
            return text or ""

        def expand(token: str) -> list[str]:
            bounds = _MARKER_RANGE_RE.match(token)
            if bounds is None:
                return [token]
            start, end = int(bounds.group(1)), int(bounds.group(2))
            if end < start or end - start > _MARKER_RANGE_CAP:
                return []
            return [str(num) for num in range(start, end + 1)]

        def repl(match: re.Match) -> str:
            resolved: list[str] = []
            for token in re.split(r"\s*,\s*", match.group(1)):
                for num in expand(token.strip()):
                    fragment_id = index.get(num)
                    if fragment_id and fragment_id not in resolved:
                        resolved.append(fragment_id)
            if not resolved:
                return match.group(0)
            return "[" + ", ".join(resolved) + "]"

        return _MARKER_RE.sub(repl, text)

    def _source_for_citation(self, cited_id: str) -> SourceRef | None:
        """SourceRef по цитате из текста LLM: fragment-… ищется в store.fragments
        напрямую, claim-… — через факт (его source указывает на фрагмент)."""
        fragment_id = cited_id
        fallback: SourceRef | None = None
        if cited_id.startswith("claim-"):
            fact = self.store.facts.get(cited_id)
            if fact is None:
                return None
            fragment_id = fact.source.fragment_id
            fallback = fact.source
        fragment = self.store.fragments.get(fragment_id)
        if fragment is None:
            # Фрагмент не в сторе (например, цитата через claim из старых данных) —
            # источник факта уже содержит нужную ссылку
            return fallback
        return SourceRef(
            document_id=fragment.document_id,
            version_id=fragment.version_id,
            fragment_id=fragment.id,
            page=fragment.page,
            section=fragment.section,
            quote=fragment.text[:200],
        )

    def _cited_sources(self, texts: list[str], sources: list[SourceRef]) -> list[SourceRef]:
        """Строго процитированные фрагменты в порядке первого появления: «источников
        не больше, чем сносок в тексте». Каждая цитата резолвится (fragment напрямую;
        claim-… через store.facts; отсутствующий в сторе фрагмент пропускается, а не
        выдумывается). Ничего сверх процитированного. Пустой список = цитат нет
        (поведение при их отсутствии определяет вызывающий _finalize)."""
        by_fragment = {source.fragment_id: source for source in sources}
        cited: list[SourceRef] = []
        seen: set[str] = set()
        for text in texts:
            for match in _CITATION_RE.finditer(text or ""):
                for cited_id in re.split(r"\s*,\s*", match.group(1)):
                    source = by_fragment.get(cited_id) or self._source_for_citation(cited_id)
                    if source is None or source.fragment_id in seen:
                        continue
                    seen.add(source.fragment_id)
                    cited.append(source)
        return cited

    def _related_views(self, related_facts: list[Fact]) -> tuple[list[ExperimentRow], list[SourceRef], GraphPayload]:
        """Представления «Смежных данных» (таблица, источники топ-12, граф) —
        одна сборка для финального ответа и SSE-предпросмотра: единая точка
        исключает расхождение встроенных копий (в частности, в месте применения
        ограничения [:12])."""
        return (
            [self._row_from_fact(fact) for fact in related_facts[:20]],
            self._collect_sources(related_facts)[:12],
            self.store.get_graph(facts=related_facts[:20]),
        )

    def evidence_preview(self, evidence: CollectedEvidence) -> dict[str, Any]:
        """Полезная нагрузка SSE-события "evidence" (контракт К1): всё, что
        готово до генерации, в формате полей QueryResponse."""
        related_experiments, related_sources, related_graph = self._related_views(evidence.related_facts)
        evidence_status = _evidence_status(evidence.has_direct_facts, evidence.related_facts, evidence.search_hits)
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
            "confidence": evidence.confidence,
            "has_direct_facts": evidence.has_direct_facts,
            "evidence_status": evidence_status,
            "pipeline_mode": evidence.pipeline_mode,
            # Научность считается по СНОСКАМ ответа, а сносок до генерации не
            # существует — предпросмотр возвращает None («—»), значение заполняет
            # финальный ответ. Доля по найденной подборке не используется: она
            # расходилась бы с финальным значением между стримом и финалом.
            "scientific_share": None,
        }

    def _graph_traverse(self, parsed: ParsedQuestion) -> set[str]:
        """Обход Neo4j по плану вопроса тремя Cypher-шаблонами (сущности,
        числовые параметры, регион): объединение id найденных Claim-узлов.
        Без подключённого графового стока — пустое множество."""
        if not self.store.graph_sink or not self.store.graph_sink.enabled:
            return set()
        claim_ids: set[str] = set()

        terms = [e.name for e in parsed.entities]
        for value in (parsed.material, parsed.process, parsed.equipment):
            if value:
                terms.append(value)
        if terms:
            claim_ids |= self._template_entity_neighbors(terms)

        for condition in parsed.conditions + ([parsed.target] if parsed.target else []):
            claim_ids |= self._template_numeric_parameter(condition.parameter)

        if parsed.region:
            claim_ids |= self._template_region(parsed.region)

        return claim_ids

    def _template_entity_neighbors(self, terms: list[str]) -> set[str]:
        """Шаблон 1: сущность (любого типа онтологии) -> Claim'ы, которые её упоминают."""
        query = """
        UNWIND $terms AS term
        MATCH (n) WHERE toLower(n.name) CONTAINS toLower(term)
        MATCH (c:Claim)-[:MENTIONS]->(n)
        RETURN DISTINCT c.id AS claim_id
        LIMIT $limit
        """
        rows = self.store.graph_sink.run_read(query, {"terms": terms, "limit": GRAPH_LIMIT})
        return {row["claim_id"] for row in rows if row.get("claim_id")}

    def _template_numeric_parameter(self, parameter_name: str) -> set[str]:
        """Шаблон 2: NumericParameter/Condition по имени -> связанные Experiment/Claim.

        Само значение параметра в узле графа сейчас не хранится (только name),
        поэтому фильтрация по value_min/value_max делается позже в Python
        по Fact/candidate.payload — см. _numeric_condition_matches.
        """
        query = """
        MATCH (p) WHERE (p:NumericParameter OR p:Condition) AND toLower(p.name) CONTAINS toLower($parameter)
        OPTIONAL MATCH (c:Claim)-[:MENTIONS]->(p)
        OPTIONAL MATCH (p)<-[:measured_parameter|operates_at_condition]-(e:Experiment)<-[:BASED_ON]-(c2:Claim)
        RETURN DISTINCT c.id AS claim_id, c2.id AS claim_id_2
        LIMIT $limit
        """
        rows = self.store.graph_sink.run_read(query, {"parameter": parameter_name, "limit": GRAPH_LIMIT})
        result: set[str] = set()
        for row in rows:
            if row.get("claim_id"):
                result.add(row["claim_id"])
            if row.get("claim_id_2"):
                result.add(row["claim_id_2"])
        return result

    def _template_region(self, region_name: str) -> set[str]:
        """Шаблон 3: Region -> Claim'ы, упоминающие решения/публикации в этом регионе."""
        query = """
        MATCH (r:Region) WHERE toLower(r.name) CONTAINS toLower($region)
        MATCH (c:Claim)-[:MENTIONS]->(r)
        RETURN DISTINCT c.id AS claim_id
        LIMIT $limit
        """
        rows = self.store.graph_sink.run_read(query, {"region": region_name, "limit": GRAPH_LIMIT})
        return {row["claim_id"] for row in rows if row.get("claim_id")}

    def _numeric_condition_matches(self, parsed: ParsedQuestion, claim_ids: set[str]) -> list[dict[str, Any]]:
        if not parsed.conditions and not parsed.target:
            return []
        wanted = list(parsed.conditions) + ([parsed.target] if parsed.target else [])
        matches: list[dict[str, Any]] = []
        mapped = [
            self.store.candidates[f"candidate-{cid.replace('claim-', '')}"] for cid in claim_ids
            if f"candidate-{cid.replace('claim-', '')}" in self.store.candidates
        ]
        # Числовые условия подтверждаются только утверждёнными кандидатами:
        # rejected/pending не могут становиться доказательствами;
        # кандидаты скрытых документов не участвуют
        hidden = self.store.hidden_document_ids()
        candidate_pool = [
            candidate for candidate in (mapped or list(self.store.candidates.values()))
            if candidate.status == CandidateStatus.approved
            and (candidate.source is None or candidate.source.document_id not in hidden)
        ]
        for candidate in candidate_pool:
            payload = candidate.payload
            numeric_params = payload.get("numeric_parameters") or payload.get("parameters") or []
            for condition in wanted:
                for item in numeric_params:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("type") or item.get("parameter") or item.get("name") or "").lower()
                    # Безымянный параметр не считается совпадением:
                    # пустая строка — подстрока любого условия
                    if not name.strip():
                        continue
                    if condition.parameter.lower() not in name and name not in condition.parameter.lower():
                        continue
                    if self._value_in_range(item, condition):
                        matches.append({"candidate_id": candidate.id, "source": candidate.source, "parameter": item})
        return matches

    @staticmethod
    def _value_in_range(item: dict[str, Any], condition) -> bool:
        """Пересекается ли значение/диапазон параметра кандидата с условием вопроса.

        Fail-open по замыслу: без чисел или с неконвертируемыми значениями
        возвращается True — что нельзя проверить, то не отбраковывается.
        Обе стороны сравнения приводятся к базовой единице величины условия,
        если единица распознана (normalize_for_quantity).
        """
        value = item.get("value")
        value_min = item.get("value_min", value)
        value_max = item.get("value_max", value)
        if value_min is None and value_max is None:
            return True
        try:
            value_min = float(value_min) if value_min is not None else None
            value_max = float(value_max) if value_max is not None else None
        except (TypeError, ValueError):
            return True
        quantity = normalize_quantity(1.0, condition.unit)[0] if condition.unit else None
        if quantity == "unknown":
            quantity = None
        item_unit = item.get("unit")
        if quantity is not None:
            if value_min is not None:
                converted = normalize_for_quantity(value_min, item_unit, quantity)
                value_min = converted[0] if converted is not None else value_min
            if value_max is not None:
                converted = normalize_for_quantity(value_max, item_unit, quantity)
                value_max = converted[0] if converted is not None else value_max
            if condition.value_min is not None:
                converted = normalize_for_quantity(condition.value_min, condition.unit, quantity)
                condition_min = converted[0] if converted is not None else condition.value_min
            else:
                condition_min = None
            if condition.value_max is not None:
                converted = normalize_for_quantity(condition.value_max, condition.unit, quantity)
                condition_max = converted[0] if converted is not None else condition.value_max
            else:
                condition_max = None
        else:
            condition_min = condition.value_min
            condition_max = condition.value_max
        if condition_min is not None and value_max is not None and value_max < condition_min:
            return False
        if condition_max is not None and value_min is not None and value_min > condition_max:
            return False
        return True

    def _indirect_search(self, parsed: ParsedQuestion) -> tuple[list[Fact], list[str]]:
        """Прямых данных нет: ищем по одному ослабленному признаку за раз (материал ИЛИ процесс)."""
        hypotheses: list[str] = []
        loose_terms = [parsed.material, parsed.process, parsed.equipment] + [e.name for e in parsed.entities]
        loose_terms = [t for t in loose_terms if t]
        found: list[Fact] = []
        # visible_facts: скрытые документы не дают и косвенных кейсов;
        # выборка одна на все ослабленные признаки, а не в каждой итерации
        visible = self.store.visible_facts()
        for term in loose_terms:
            normalized = self.store.normalizer.normalize_entity(term) or term
            partial = [f for f in visible if normalized.lower() in f.material.lower() or normalized.lower() in f.process.lower()]
            if partial:
                found.extend(partial)
                hypotheses.append(
                    f"Прямых данных по полной комбинации не найдено. Найдены косвенные кейсы по «{term}» "
                    f"({len(partial)} факт(ов)) — не подтверждённый вывод, гипотеза для проверки."
                )
        unique = self._merge_unique(found)
        # Косвенные находки помечаются гипотезами (копии — базу не трогаем)
        unique = [f.model_copy(update={"is_hypothesis": True}) for f in unique]
        return unique, hypotheses

    def _build_evidence_pack(
        self, parsed, facts, numeric_evidence, search_hits, contradictions, gaps
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Возвращает (pack, citation_index). Каждому цитируемому элементу
        (facts, затем search_hits) присваивается сквозной номер "n" — модель
        цитирует ИМ, а _finalize переводит номер в канонический id фрагмента.
        citation_index: "номер" → fragment_id."""
        citation_index: dict[str, str] = {}
        marker = 0
        fact_items: list[dict[str, Any]] = []
        for f in facts[:15]:
            marker += 1
            citation_index[str(marker)] = f.source.fragment_id
            fact_items.append({
                "n": marker,
                "material": f.material,
                "process": f.process,
                "property": f.property,
                "effect": f.effect_direction,
                "value": f.effect_value,
                "unit": f.effect_unit,
                "status": f.status,
                "confidence": f.confidence,
                "source": f.source.model_dump(mode="json"),
            })
        hit_items: list[dict[str, Any]] = []
        for h in search_hits[:10]:
            marker += 1
            citation_index[str(marker)] = h.fragment_id
            hit_items.append({
                "n": marker,
                "text": h.text[:400],
                "score": h.score,
                "source": h.source.model_dump(mode="json"),
            })
        pack = {
            "question_plan": parsed.model_dump(mode="json") if parsed is not None else None,
            "facts": fact_items,
            "numeric_matches": [
                {"candidate_id": m["candidate_id"], "parameter": m["parameter"],
                 "source": m["source"].model_dump(mode="json") if m["source"] else None}
                for m in numeric_evidence[:15]
            ],
            "search_hits": hit_items,
            "known_contradictions": contradictions,
            "known_gaps": gaps,
        }
        return pack, citation_index

    def _answer_messages(self, question: str, evidence_pack: dict[str, Any]) -> list[dict[str, str]]:
        """Сообщения генерации ответа: одни и те же для /ask (chat_json)
        и /ask/stream (chat_stream)."""
        return [
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                {"question": question, "evidence_pack": evidence_pack}, ensure_ascii=False
            )},
        ]

    async def _generate_answer(self, question: str, evidence_pack: dict[str, Any]) -> dict[str, Any]:
        # Отказ LLM пробрасывается наверх (LLMUnavailableError) и показывается
        # пользователю; прочие сбои приводятся к тому же типу
        try:
            return await chat_json(messages=self._answer_messages(question, evidence_pack))
        except LLMUnavailableError:
            raise
        except Exception as error:
            raise LLMUnavailableError("bad_response", str(error)) from error

    def _entity_key(self, value: str | None) -> str | None:
        """Ключ сравнения сущностей: канонизация нормалайзером + casefold + ё→е.

        Применяется к ОБЕИМ сторонам сравнения: «медный концентрат» из вопроса
        должен совпасть с «Медный концентрат» из документа и без synonyms.csv.
        """
        if not value:
            return None
        return canonical_text(self.store.normalizer.normalize_entity(value) or value)

    def _filter_facts_legacy(self, facts: list[Fact], request: QueryRequest, parsed: ParsedQuestion) -> list[Fact]:
        confidence_min = max(request.confidence_min, request.filters.confidence_min)
        result: list[Fact] = []
        material_key = self._entity_key(parsed.material)
        property_key = self._entity_key(parsed.property)
        filter_materials = {self._entity_key(item) for item in request.filters.materials if item}
        filter_properties = {self._entity_key(item) for item in request.filters.properties if item}
        filter_labs = set(request.filters.laboratories)
        for fact in facts:
            if fact.confidence < confidence_min:
                continue
            if fact.is_hypothesis and not request.include_hypotheses:
                continue
            if material_key and self._entity_key(fact.material) != material_key:
                continue
            if property_key and self._entity_key(fact.property) != property_key:
                continue
            if filter_materials and self._entity_key(fact.material) not in filter_materials:
                continue
            if filter_properties and self._entity_key(fact.property) not in filter_properties:
                continue
            if filter_labs and fact.lab not in filter_labs:
                continue
            result.append(fact)
        return result

    @staticmethod
    def _merge_unique(*fact_lists: list[Fact]) -> list[Fact]:
        seen: set[str] = set()
        merged: list[Fact] = []
        for facts in fact_lists:
            for fact in facts:
                if fact.id not in seen:
                    merged.append(fact)
                    seen.add(fact.id)
        return merged

    def _rank_fact(self, parsed: ParsedQuestion):
        material_key = self._entity_key(parsed.material)
        property_key = self._entity_key(parsed.property)

        def rank(fact: Fact) -> float:
            score = fact.confidence
            if material_key and self._entity_key(fact.material) == material_key:
                score += 0.3
            if property_key and self._entity_key(fact.property) == property_key:
                score += 0.25
            if parsed.process and parsed.process.lower() in fact.process.lower():
                score += 0.2
            return score
        return rank

    def _row_from_fact(self, fact: Fact) -> ExperimentRow:
        value = direction_label(fact.effect_direction)
        if fact.effect_value is not None:
            value += f" на {fact.effect_value:g}{fact.effect_unit or ''}"
        return ExperimentRow(
            experiment_id=fact.experiment_id, material=fact.material, sample=fact.sample,
            process=fact.process, temperature_c=fact.temperature_c, duration_h=fact.duration_h,
            property=fact.property, effect=value, lab=fact.lab, confidence=fact.confidence, source=fact.source,
        )

    def _collect_sources(self, facts: list[Fact]) -> list[SourceRef]:
        """Источники фактов без дублей (по паре документ+фрагмент), в порядке фактов."""
        sources: list[SourceRef] = []
        seen: set[tuple[str, str]] = set()
        for fact in facts:
            key = (fact.source.document_id, fact.source.fragment_id)
            if key not in seen:
                sources.append(fact.source)
                seen.add(key)
        return sources

    def _degraded_summary(self, facts: list[Fact], search_hits, has_direct_facts: bool, llm_failed: bool) -> str:
        """Человекочитаемое summary без LLM: что реально нашлось в базе.

        Используется и когда LLM недоступна (llm_failed), и когда модель
        не вернула summary.
        """
        if has_direct_facts:
            body = f"В базе знаний найдено {len(facts)} факт(ов) по запросу — см. таблицу фактов и источники."
        elif facts:
            body = (f"Прямых фактов не найдено; есть {len(facts)} косвенных кейс(ов) по смежным понятиям — "
                    "см. гипотезы.")
        elif search_hits:
            top = [
                f"«{hit.metadata.get('filename', hit.source.document_id)}» — {hit.text[:160].strip()}…"
                for hit in search_hits[:3]
            ]
            body = (f"Фактов в графе не найдено, но семантический поиск дал {len(search_hits)} "
                    "релевантных фрагментов:\n- " + "\n- ".join(top))
        else:
            body = "В базе знаний ничего не найдено по этому запросу."
        if llm_failed:
            return "Ответ собран без языковой модели (см. причину выше). " + body
        return body

    def _find_contradictions(self, facts: list[Fact]) -> list[str]:
        groups: dict[tuple[str, str], list[Fact]] = {}
        labels: dict[tuple[str, str], tuple[str, str]] = {}
        for fact in facts:
            material = self.store.normalizer.normalize_entity(fact.material) or fact.material
            property_name = self.store.normalizer.normalize_entity(fact.property) or fact.property
            key = (canonical_text(material), canonical_text(property_name))
            labels.setdefault(key, (material, property_name))
            groups.setdefault(key, []).append(fact)
        contradictions: list[str] = []
        seen_messages: set[str] = set()
        for key, group in groups.items():
            for first, second in combinations(group, 2):
                directions = {
                    normalize_effect_direction(first.effect_direction),
                    normalize_effect_direction(second.effect_direction),
                }
                if directions != {"increase", "decrease"}:
                    continue
                if not _comparable_conditions(first, second):
                    continue
                material, property_name = labels[key]
                message = (
                    f"{material}, {property_name}: разные источники показывают противоположный эффект "
                    f"при сопоставимых условиях; лаборатории: {', '.join(sorted({first.lab, second.lab}))}."
                )
                if message not in seen_messages:
                    seen_messages.add(message)
                    contradictions.append(message)
        return contradictions

    def _find_gaps(self, parsed, facts: list[Fact], numeric_evidence: list[dict[str, Any]]) -> list[str]:
        gaps: list[str] = []
        if not facts and not numeric_evidence:
            return ["Нет подтверждённых фактов для заданной комбинации условий."]
        if facts:
            labs = {fact.lab for fact in facts}
            if len(labs) < 2:
                gaps.append("Результаты подтверждены менее чем двумя независимыми источниками.")
        # Факты по теме могут найтись, а численное подтверждение целевого
        # показателя — нет: это и есть пробел
        if parsed is not None and parsed.target and not numeric_evidence:
            gaps.append(f"Нет данных, напрямую подтверждающих целевой показатель «{parsed.target.parameter}».")
        return gaps


def _mean_confidence(facts: list[Fact]) -> float:
    """Уверенность ответа: средняя уверенность фактов (3 знака), пустой список — 0.0.
    Единственная копия формулы для быстрой ветки, полного пайплайна и демоута."""
    return round(mean([fact.confidence for fact in facts]), 3) if facts else 0.0


def _merge_texts(base: list[str], extra: list[str] | None) -> list[str]:
    """Дописывает списки модели к собранным конвейером: дубли убираются,
    порядок первого появления сохраняется."""
    return list(dict.fromkeys(base + extra)) if extra else base


def _evidence_status(has_direct_facts: bool, related_facts, search_hits) -> str:
    """Статус доказательной базы: прямые факты / смежные-поиск / пусто.
    Единая точка для финального ответа и SSE-предпросмотра (контракт К1):
    единственная реализация исключает расхождение встроенных копий условия."""
    if has_direct_facts:
        return "direct"
    return "partial" if related_facts or search_hits else "none"


def _comparable_conditions(first: Fact, second: Fact, temperature_tolerance_c: float = 5.0) -> bool:
    """Противоположные эффекты при разных условиях — не противоречие:
    рост твёрдости при 705 °C и падение при 790 °C физически согласованы
    (пик старения). Неуказанная температура считается «любой» и пересекается
    со всем; разные процессы делают пару несопоставимой.
    """
    if first.process and second.process and canonical_text(first.process) != canonical_text(second.process):
        return False
    if (
        first.temperature_c is not None
        and second.temperature_c is not None
        and abs(first.temperature_c - second.temperature_c) > temperature_tolerance_c
    ):
        return False
    return True
