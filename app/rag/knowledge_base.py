from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Dict
from pathlib import Path
from typing import List, Optional, Set

from app.core.llm_client import get_llm_client

BASE_DIR = Path(__file__).resolve().parents[2]
KNOWLEDGE_PATH = BASE_DIR / "knowledge.jsonl"


@dataclass
class KnowledgeChunk:
    url: str
    title: str
    paragraph: str


IMPORTANT_TERMS = (
    "jahanje",
    "jahamo",
    "ponij",
    "bunka",
    "marmelad",
    "salama",
    "klobasa",
    "liker",
)


def _split_into_paragraphs(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    for raw in normalized.split("\n"):
        chunk = raw.strip()
        if not chunk:
            continue
        lowered = chunk.lower()
        # kratke vrstice obdržimo, če imajo pomembne izraze (jahanje, bunka, salama …)
        if len(chunk) < 40 and not any(term in lowered for term in IMPORTANT_TERMS):
            continue
        paragraphs.append(chunk)
    return paragraphs


def load_knowledge_chunks() -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    if not KNOWLEDGE_PATH.exists():
        print(f"[knowledge_base] Datoteka {KNOWLEDGE_PATH} ne obstaja. Vračam prazen seznam.")
        return chunks

    with KNOWLEDGE_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = record.get("url", "") or ""
            title = record.get("title", "") or ""
            content = record.get("content", "") or ""
            if not (url or title or content):
                continue
            for paragraph in _split_into_paragraphs(content):
                chunks.append(KnowledgeChunk(url=url, title=title, paragraph=paragraph))

    print(f"[knowledge_base] Naloženih {len(chunks)} odstavkov")
    return chunks


KNOWLEDGE_CHUNKS: List[KnowledgeChunk] = load_knowledge_chunks()

CONTACT = {
    "phone": "02 700 12 34, 031 777 888",
    "email": "info@kmetijapodgoro.si",
}


def _tokenize(text: str) -> Set[str]:
    lowered = text.lower()
    cleaned = re.sub(r"[^\w]+", " ", lowered)
    return {token for token in cleaned.split() if len(token) >= 3}


def _bm25_tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-zČŠŽčšžĐđĆć0-9]+", text.lower())
    return [t for t in tokens if len(t) >= 3]


BM25_K1 = 1.6
BM25_B = 0.75
EMBEDDING_MODEL = "text-embedding-3-small"
HYBRID_BM25_WEIGHT = 0.65
HYBRID_VECTOR_WEIGHT = 0.35
HYBRID_BM25_CANDIDATES = 20
RERANK_TOP_K = 6

BM25_DOC_TF: list[Dict[str, int]] = []
BM25_DOC_LEN: list[int] = []
BM25_IDF: Dict[str, float] = {}
BM25_AVGDL = 0.0
EMBEDDING_CACHE: Dict[str, list[float]] = {}


def _build_bm25_index(chunks: list[KnowledgeChunk]) -> None:
    global BM25_DOC_TF, BM25_DOC_LEN, BM25_IDF, BM25_AVGDL
    doc_tfs: list[Dict[str, int]] = []
    doc_lens: list[int] = []
    df: Dict[str, int] = {}
    for chunk in chunks:
        tokens = _bm25_tokenize(f"{chunk.title} {chunk.paragraph}")
        tf: Dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        doc_tfs.append(tf)
        doc_lens.append(len(tokens))
        for token in tf.keys():
            df[token] = df.get(token, 0) + 1
    BM25_DOC_TF = doc_tfs
    BM25_DOC_LEN = doc_lens
    BM25_AVGDL = (sum(doc_lens) / len(doc_lens)) if doc_lens else 0.0
    BM25_IDF = {}
    n_docs = len(doc_lens)
    for token, freq in df.items():
        BM25_IDF[token] = math.log(1.0 + (n_docs - freq + 0.5) / (freq + 0.5))


def _bm25_score(query_tokens: list[str], doc_index: int) -> float:
    if not query_tokens or not BM25_DOC_TF:
        return 0.0
    tf = BM25_DOC_TF[doc_index]
    doc_len = BM25_DOC_LEN[doc_index] if doc_index < len(BM25_DOC_LEN) else 0
    score = 0.0
    for token in query_tokens:
        if token not in tf:
            continue
        idf = BM25_IDF.get(token, 0.0)
        freq = tf[token]
        denom = freq + BM25_K1 * (1.0 - BM25_B + BM25_B * (doc_len / (BM25_AVGDL or 1.0)))
        score += idf * (freq * (BM25_K1 + 1.0)) / (denom or 1.0)
    return score


def _normalize_scores(scores: list[float]) -> list[float]:
    if not scores:
        return []
    min_val = min(scores)
    max_val = max(scores)
    if max_val - min_val <= 1e-6:
        return [0.0 for _ in scores]
    return [(s - min_val) / (max_val - min_val) for s in scores]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a <= 1e-9 or norm_b <= 1e-9:
        return 0.0
    return dot / (norm_a * norm_b)


def _get_embedding(text: str) -> Optional[list[float]]:
    cached = EMBEDDING_CACHE.get(text)
    if cached:
        return cached
    try:
        client = get_llm_client()
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
        vector = response.data[0].embedding
        EMBEDDING_CACHE[text] = vector
        return vector
    except Exception:
        return None


def _rerank_with_llm(query: str, chunks: list[KnowledgeChunk]) -> list[KnowledgeChunk]:
    if not chunks:
        return chunks
    try:
        client = get_llm_client()
    except Exception:
        return chunks

    items = []
    for idx, chunk in enumerate(chunks):
        snippet = chunk.paragraph.strip()
        if len(snippet) > 350:
            snippet = snippet[:350].rsplit(" ", 1)[0]
        title = chunk.title.strip() if chunk.title else ""
        items.append(f"{idx}. {title}\n{snippet}")

    prompt = (
        "Return a JSON array of objects with fields index (int) and score (0-10). "
        "Score each item by relevance to the question. Use all items.\n\n"
        f"Question: {query}\n\n"
        "Items:\n" + "\n\n".join(items)
    )
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        max_output_tokens=200,
        temperature=0,
    )
    text = getattr(response, "output_text", "") or ""
    try:
        data = json.loads(text)
        scores = {}
        for item in data:
            idx = int(item.get("index"))
            score = float(item.get("score"))
            scores[idx] = score
        ranked = sorted(
            [(scores.get(i, 0.0), chunk) for i, chunk in enumerate(chunks)],
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [chunk for _, chunk in ranked]
    except Exception:
        return chunks


def _score_chunk(tokens: Set[str], chunk: KnowledgeChunk) -> float:
    paragraph_tokens = _tokenize(chunk.paragraph)
    if not paragraph_tokens:
        return 0.0
    title_tokens = _tokenize(chunk.title)
    overlap_para = len(tokens & paragraph_tokens)
    overlap_title = len(tokens & title_tokens)
    return overlap_para + 0.5 * overlap_title


def _score_chunk_ratio(tokens: Set[str], chunk: KnowledgeChunk, base_len: int) -> float:
    if not tokens or base_len <= 0:
        return 0.0
    paragraph_tokens = _tokenize(chunk.paragraph)
    if not paragraph_tokens:
        return 0.0
    title_tokens = _tokenize(chunk.title)
    overlap_para = len(tokens & paragraph_tokens)
    overlap_title = len(tokens & title_tokens)
    raw = overlap_para + 0.5 * overlap_title
    return raw / max(1.0, float(base_len))


def _expand_query_tokens(query: str, tokens: Set[str]) -> Set[str]:
    lowered = query.lower()
    expanded = set(tokens)
    if "konj" in lowered or "konja" in lowered:
        expanded.update({"poni", "ponij", "ponija", "jahanje"})
    if "jah" in lowered:
        expanded.update({"jahanje", "poni", "ponij", "ponija"})
    return expanded


def search_knowledge_scored(query: str, top_k: int = 3) -> list[tuple[float, KnowledgeChunk]]:
    base_tokens = _tokenize(query)
    tokens = _expand_query_tokens(query, base_tokens)
    base_len = len(base_tokens)
    if not tokens:
        return []
    lowered = query.lower()
    candidates = None
    for patterns in KEYWORD_RULES.values():
        if any(term in lowered for term in patterns):
            candidates = []
            for chunk in KNOWLEDGE_CHUNKS:
                chunk_text = f"{chunk.title.lower()} {chunk.paragraph.lower()} {chunk.url.lower()}"
                if any(term in chunk_text for term in patterns):
                    candidates.append(chunk)
            break
    # Če je vprašanje o jahanju/poniju, preferiraj specifične odstavke
    if any(term in lowered for term in ["jahanje", "jahati", "jahamo", "poni", "ponij", "konj", "konja"]):
        filtered = []
        source = candidates if candidates is not None else KNOWLEDGE_CHUNKS
        for chunk in source:
            chunk_text = f"{chunk.title.lower()} {chunk.paragraph.lower()} {chunk.url.lower()}"
            if "ponij" in chunk_text or "jahanje" in chunk_text:
                filtered.append(chunk)
        if filtered:
            candidates = filtered
    scored: list[tuple[float, KnowledgeChunk]] = []
    for chunk in (candidates if candidates is not None else KNOWLEDGE_CHUNKS):
        score = _score_chunk_ratio(tokens, chunk, base_len)
        if score > 0:
            scored.append((score, chunk))
    if any(term in lowered for term in ["jahanje", "jahati", "jahamo", "poni", "ponij", "konj", "konja"]):
        boosted: list[tuple[float, KnowledgeChunk]] = []
        for score, chunk in scored:
            chunk_text = f"{chunk.title.lower()} {chunk.url.lower()}"
            if "ponij" in chunk_text or "jahanje" in chunk_text:
                score += 1.0
            boosted.append((score, chunk))
        scored = boosted
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_k]


def search_knowledge(query: str, top_k: int = 5) -> list[KnowledgeChunk]:
    return search_knowledge_hybrid(query, top_k=top_k)


def search_knowledge_hybrid(query: str, top_k: int = 5) -> list[KnowledgeChunk]:
    base_tokens = _tokenize(query)
    tokens = _expand_query_tokens(query, base_tokens)
    if not tokens:
        return []
    bm25_tokens = _bm25_tokenize(" ".join(tokens))
    if not bm25_tokens:
        return []

    bm25_scored: list[tuple[float, int]] = []
    for idx in range(len(KNOWLEDGE_CHUNKS)):
        score = _bm25_score(bm25_tokens, idx)
        if score > 0:
            bm25_scored.append((score, idx))
    bm25_scored.sort(key=lambda item: item[0], reverse=True)
    if not bm25_scored:
        return []

    candidates = bm25_scored[: max(HYBRID_BM25_CANDIDATES, top_k)]
    candidate_chunks = [KNOWLEDGE_CHUNKS[idx] for _, idx in candidates]

    query_embedding = _get_embedding(query)
    if not query_embedding:
        return [chunk for _, chunk in candidates[:top_k]]

    vector_scores: list[float] = []
    for chunk in candidate_chunks:
        embedding = _get_embedding(chunk.paragraph)
        if not embedding:
            vector_scores.append(0.0)
            continue
        vector_scores.append(_cosine_similarity(query_embedding, embedding))

    bm25_scores = [score for score, _ in candidates]
    bm25_norm = _normalize_scores(bm25_scores)
    vector_norm = _normalize_scores(vector_scores)

    hybrid_scored: list[tuple[float, KnowledgeChunk]] = []
    for i, chunk in enumerate(candidate_chunks):
        hybrid_score = (
            HYBRID_BM25_WEIGHT * bm25_norm[i] + HYBRID_VECTOR_WEIGHT * vector_norm[i]
        )
        hybrid_scored.append((hybrid_score, chunk))
    hybrid_scored.sort(key=lambda pair: pair[0], reverse=True)
    ranked = [chunk for _, chunk in hybrid_scored[: max(top_k, RERANK_TOP_K)]]
    reranked = _rerank_with_llm(query, ranked[:RERANK_TOP_K])
    return reranked[:top_k]


_build_bm25_index(KNOWLEDGE_CHUNKS)


KEYWORD_RULES = {
    "salama": ["salama", "salamo", "salame", "klobasa", "klobaso", "mesni izdelki", "klobase"],
    "bunka": ["bunka", "bunko", "bunke", "pohorska bunka"],
    "marmelada": ["marmelada", "marmelado", "marmelade", "marmeldo", "džem", "namaz", "marmelad"],
    "liker": ["liker", "likerje", "žganje", "žganja", "tepkovec"],
    "jahanje": ["jahanje", "jahati", "jahamo", "poni", "ponij", "ponija", "ponijem"],
    "nočitev": ["nočitev", "nočitve", "noči"],
    "kosilo": ["vikend kosilo", "degustacijski", "degustacijo", "kosilo"],
}


def _collect_focus_terms(question: str) -> list[str]:
    lowered = question.lower()
    focus: list[str] = []
    for patterns in KEYWORD_RULES.values():
        if any(term in lowered for term in patterns):
            focus.extend(patterns)
    if not focus:
        focus.extend(IMPORTANT_TERMS)
    return list({term for term in focus if len(term) >= 3})


def _trim_content(content: str, focus_terms: list[str]) -> str:
    if len(content) <= 700:
        return content
    content_lower = content.lower()
    for term in focus_terms:
        idx = content_lower.find(term)
        if idx != -1:
            start = max(0, idx - 200)
            end = min(len(content), idx + 500)
            snippet = content[start:end]
            start_dot = snippet.find(". ")
            if start > 0 and start_dot != -1:
                snippet = snippet[start_dot + 1 :]
            return snippet.strip()
    snippet = content[:700]
    last_dot = snippet.rfind(".")
    if last_dot > 200:
        snippet = snippet[: last_dot + 1]
    return snippet


def _needs_vegetarian_menu_fallback(question: str, paragraphs: list[KnowledgeChunk]) -> bool:
    lowered = question.lower()
    if "vegetar" not in lowered:
        return False
    if not any(token in lowered for token in ["meni", "jedilnik", "predstav", "ponudba"]):
        return False
    for chunk in paragraphs:
        text = f"{chunk.title} {chunk.paragraph}".lower()
        if ("meni" in text or "jedilnik" in text) and "vegetar" in text:
            return False
    return True


def _build_context_snippet(question: str, paragraphs: List[KnowledgeChunk]) -> str:
    focus_terms = _collect_focus_terms(question)
    parts: list[str] = []
    for chunk in paragraphs:
        lines: list[str] = []
        if chunk.title:
            lines.append(f"Naslov: {chunk.title}")
        if chunk.url:
            lines.append(f"URL: {chunk.url}")
        content = _trim_content(chunk.paragraph.strip(), focus_terms)
        lines.append(f"Vsebina: {content}")
        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts)


def _keyword_chunks(question: str, limit: int = 6) -> list[KnowledgeChunk]:
    lowered = question.lower()
    selected: list[KnowledgeChunk] = []
    seen = set()
    for keyword, patterns in KEYWORD_RULES.items():
        if any(term in lowered for term in patterns):
            for chunk in KNOWLEDGE_CHUNKS:
                chunk_text = f"{chunk.title.lower()} {chunk.paragraph.lower()} {chunk.url.lower()}"
                if any(term in chunk_text for term in patterns):
                    key = (chunk.url, chunk.paragraph[:80])
                    if key not in seen:
                        selected.append(chunk)
                        seen.add(key)
                        if len(selected) >= limit:
                            return selected
            if len(selected) >= limit:
                break
    return selected


def _gather_relevant_chunks(question: str, base_top_k: int = 6) -> list[KnowledgeChunk]:
    lowered = question.lower()
    is_bunka = any(word in lowered for word in ["bunka", "bunko", "bunke"])
    is_salama = any(
        word in lowered for word in ["salama", "salamo", "salame", "klobasa", "klobase", "klobaso"]
    )
    is_marmelada = any(word in lowered for word in ["marmelad", "marmelado", "marmelade", "marmeldo", "džem"])
    is_jahanje = any(
        word in lowered for word in ["jahanje", "jahati", "jahamo", "poni", "ponij", "ponija", "ponijem"]
    )

    # mesnine (bunka / salama)
    if is_bunka or is_salama:
        chunks = [
            chunk
            for chunk in KNOWLEDGE_CHUNKS
            if "/izdelek/" in chunk.url.lower()
            and (
                "bunka" in chunk.title.lower()
                or "bunka" in chunk.paragraph.lower()
                or "salama" in chunk.title.lower()
                or "salama" in chunk.paragraph.lower()
                or "mesni izdelki" in chunk.paragraph.lower()
            )
        ]
        return chunks[:4]

    # marmelade
    if is_marmelada:
        chunks = [
            chunk
            for chunk in KNOWLEDGE_CHUNKS
            if "/marmelada" in chunk.url.lower()
            or "marmelad" in chunk.title.lower()
            or "kategorija: marmelade" in chunk.paragraph.lower()
        ]
        return chunks[:4]

    # jahanje / poni – če ni v bazi, dodamo ročni fallback
    if is_jahanje:
        chunks = [
            chunk
            for chunk in KNOWLEDGE_CHUNKS
            if "jahanje" in chunk.paragraph.lower() or "ponij" in chunk.paragraph.lower()
        ]
        if chunks:
            return chunks[:4]
        return [
            KnowledgeChunk(
                url="https://kmetijapodgoro.si/cenik/",
                title="Jahanje s ponijem",
                paragraph="Jahanje s ponijem / 1 krog – 5,00 € (glej cenik Kmetija Pod Goro).",
            )
        ]

    keyword_chunks = _keyword_chunks(question, limit=4)
    base_chunks = search_knowledge(question, top_k=base_top_k)

    combined: list[KnowledgeChunk] = []
    seen = set()
    for chunk in keyword_chunks + base_chunks:
        key = (chunk.url, chunk.paragraph[:80])
        if key in seen:
            continue
        combined.append(chunk)
        seen.add(key)
        if len(combined) >= base_top_k + len(keyword_chunks):
            break
    return combined


def _filter_chunks_by_category(question: str, chunks: list[KnowledgeChunk]) -> list[KnowledgeChunk]:
    lowered = question.lower()

    # mesnine: bunka / salama / klobasa
    if any(word in lowered for word in ["bunka", "bunko", "salama", "klobasa", "mesni"]):
        filtered = [
            c
            for c in chunks
            if "mesni izdelki" in c.paragraph.lower()
            or "kategorija: mesni" in c.paragraph.lower()
            or "bunka" in c.paragraph.lower()
            or "salama" in c.paragraph.lower()
        ]
        if filtered:
            return filtered[:4]
        fallback = [
            c
            for c in KNOWLEDGE_CHUNKS
            if "mesni izdelki" in c.paragraph.lower()
            or "bunka" in c.paragraph.lower()
            or "salama" in c.paragraph.lower()
        ]
        return fallback[:3]

    # marmelade
    if any(word in lowered for word in ["marmelad", "džem"]):
        filtered = [c for c in chunks if "/marmelada" in c.url.lower()]
        if filtered:
            return filtered
        for chunk in KNOWLEDGE_CHUNKS:
            if "/marmelada" in chunk.url.lower():
                return [chunk]
        return chunks

    # likerji / žganje
    if any(word in lowered for word in ["liker", "žganj", "žganje"]):
        filtered = [
            c
            for c in chunks
            if any(token in c.url.lower() for token in ["liker", "žganje", "tepkovec"])
        ]
        if filtered:
            return filtered
        for chunk in KNOWLEDGE_CHUNKS:
            if any(token in chunk.url.lower() for token in ["liker", "žganje", "tepkovec"]):
                return [chunk]
        return chunks

    return chunks


SYSTEM_PROMPT = """
Ti si Maja - prijazna gostiteljica na Turistični kmetiji Kmetija Pod Goro na Pohorju. Pomagaš gostom z informacijami o kmetiji, sobah, hrani in okolici.

TVOJA OSEBNOST:
- Si topla, prijazna in pristna - kot da se pogovarjaš z gostom v jedilnici
- Govoriš naravno, kot pravi človek - ne kot robot ali uraden asistent
- Včasih dodaš osebno noto ("Pri nas je to zelo priljubljeno", "To jed imam sama zelo rada")
- Občasno uporabiš emoji, ampak zmerno (1-2 na odgovor max)
- Goste VEDNO vikaš (vi, vam, vaš)

POGOVOR:
- Odgovarjaš kratko in jedrnato (2-4 stavki), razen če gost vpraša za več podrobnosti
- Postavljaš vprašanja nazaj, da bolje razumeš potrebe ("Za koliko oseb bi bila rezervacija?", "Imate raje sladko ali suho vino?")
- Če nekaj ne veš, to iskreno poveš in ponudiš alternativo
- NE ponavljaj istih fraz - bodi kreativen/a z uvodnimi stavki

REZERVACIJE SOB:
- Sobe so odprte od SREDE do NEDELJE
- Ob ponedeljkih in torkih so ZAPRTE
- Zimski premor: 30.12.2025 - 28.2.2026 (sobe zaprte)
- Božični premor: 22.12.2025 - 26.12.2025 (sobe zaprte)
- Minimalno 2 nočitvi (3 v poletni sezoni jun/jul/avg)
- Cena: 50€/osebo/noč z zajtrkom
- Večerja: dodatnih 25€/osebo
- Za datume IZVEN obdobij zaprtja samozavestno ponudi rezervacijo!

REZERVACIJE MIZ:
- Vikend kosila: sobota in nedelja 12:00-20:00
- Zadnji prihod na kosilo: 15:00
- Vedno potrebna rezervacija vnaprej

PRIMERI DOBRIH ODGOVOROV:

Gost: "Imate proste sobe?"
Ti: "Seveda, z veseljem preverim! 😊 Za kateri datum in koliko oseb bi želeli rezervirati?"

Gost: "23.4.2026"
Ti: "Super, april je čudovit čas pri nas - narava se ravno prebuja! Za 23.4.2026 imamo sobe na voljo. Koliko vas bo in za koliko noči bi želeli ostati?"

Gost: "Kaj ponujate za jesti?"
Ti: "Ob vikendih pripravljamo domača kosila iz lokalnih sestavin - od goveje juhe z jetrnimi cmočki do pohorskega piskra in naše slovite gibanice. 😋 Vas zanima jedilnik za ta vikend?"

Gost: "Hvala"
Ti: "Ni za kaj! Če boste imeli še kakšno vprašanje, sem tu. Lep pozdrav s Pohorja! 🏔️"

ČESA NE DELAŠ:
- Ne izmišljuješ si informacij, ki jih nimaš
- Ne govoriš preveč uradno ali robotsko
- Ne ponavljaš "Več informacij na kmetijapodgoro.si" pri vsakem odgovoru
- Ne daješ predolgih odgovorov brez potrebe
- Ne zaključuješ vedno z istim stavkom
"""


def generate_llm_answer(question: str, top_k: int = 6, history: list[dict[str, str]] | None = None) -> str:
    try:
        paragraphs = _gather_relevant_chunks(question, base_top_k=top_k)
        paragraphs = _filter_chunks_by_category(question, paragraphs)
    except Exception:
        paragraphs = []

    if _needs_vegetarian_menu_fallback(question, paragraphs):
        return (
            "Trenutno nimam konkretnega vegetarijanskega menija. "
            "Lahko pa uredimo vegetarijanski obrok po predhodnem dogovoru."
        )

    if not paragraphs:
        context_text = (
            "Nimam specifičnih podatkov o tem vprašanju, ampak lahko pomagam z drugimi informacijami o kmetiji."
        )
    else:
        context_text = _build_context_snippet(question, paragraphs)

    client = get_llm_client()
    convo: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "developer", "content": f"Kontekst iz baze znanja Kmetija Pod Goro:\n{context_text}"},
    ]
    if history:
        # vzamemo zadnjih nekaj sporočil, da ohranimo kratko zgodovino
        convo.extend(history[-6:])
    convo.append({"role": "user", "content": f"Vprašanje gosta: {question}"})

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=convo,
        max_output_tokens=400,
        temperature=0.7,
        top_p=0.9,
    )

    answer = getattr(response, "output_text", None)
    if not answer:
        outputs = []
        for block in getattr(response, "output", []) or []:
            for content in getattr(block, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    outputs.append(text)
        answer = "\n".join(outputs).strip()

    return answer or (
        "Trenutno v podatkih ne najdem jasnega odgovora. Prosimo, preverite www.kmetijapodgoro.si."
    )
