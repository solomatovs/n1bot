# v 8 n1helper  - раРАБОЧАЯ ВЕРСИЯ
from __future__ import annotations
import os, re, uuid

import re
import requests
import streamlit as st
import numpy as np

from typing import List, Dict, Tuple, Optional
import numpy as np


# from langchain.schema import Document
# Используйте это (РАБОТАЕТ):
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableMap
from langchain_chroma import Chroma
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.chat_models import ChatOllama
# from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.messages import SystemMessage

import chromadb
from chromadb.config import Settings
from openai import OpenAI

import urllib3
urllib3.disable_warnings()

EMBEDDING_MODEL: str = st.secrets.get("EMBEDDING_MODEL")
LLM_TIMEOUT: int = int(st.secrets.get("LLM_TIMEOUT", 600))
EMBEDDING_TIMEOUT: int = int(st.secrets.get("EMBEDDING_TIMEOUT", 120))


# =========================
# tiktoken (офлайн безопасно)
# =========================
try:
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None

# В самом начале файла, после импортов
import ssl
import urllib3
import warnings

# Отключаем все SSL предупреждения
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# Создаем непривязанный SSL контекст
try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    pass
else:
    ssl._create_default_https_context = _create_unverified_https_context


def _approx_token_count_bytes(b: bytes) -> int:
    return (len(b) + 3) // 4


import ssl
import certifi
import urllib3
# Отключаем предупреждения
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Создаем кастомную сессию с отключенной проверкой SSL
def create_ssl_session():
    session = requests.Session()
    session.verify = False  # Отключаем проверку SSL
    session.headers.update({
        "Authorization": "Bearer sk-fe1ZWrr7lPUN7tb8ZFlYEw",
        "Content-Type": "application/json"
    })
    return session
    

class AdvancedChunker:
    def __init__(self, tokenizer=None, max_tokens: int = 512, overlap_tokens: int = 50):
        self.tokenizer = tokenizer
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
    
    def split_into_chunks(self, docs: List[Document], ollama_api_url: str) -> List[Document]:
        """Многоуровневый чанкинг с сохранением структуры"""
        all_chunks = []
        
        for doc in docs:
            chunks = self._process_document(doc, ollama_api_url)
            all_chunks.extend(chunks)
        
        return all_chunks
    
    def _process_document(self, doc: Document, ollama_api_url: str) -> List[Document]:
        """Обрабатывает один документ"""
        text = doc.page_content
        metadata = doc.metadata.copy()
        
        # 1. Разбиваем на структурные секции
        sections = self._split_into_sections(text)
        
        # 2. Для каждой секции применяем оптимальный метод чанкинга
        chunks = []
        for section in sections:
            section_chunks = self._chunk_section(section, metadata, ollama_api_url)
            chunks.extend(section_chunks)
        
        return chunks
    
    def _split_into_sections(self, text: str) -> List[Dict]:
        """Разбивает текст на логические секции по заголовкам"""
        lines = text.split('\n')
        sections = []
        current_section = []
        current_title = "Без названия"
        current_level = 0
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Определяем заголовок
            heading_match = re.match(r'^(#+)\s+(.+)$', line)
            if heading_match:
                # Сохраняем предыдущую секцию
                if current_section:
                    sections.append({
                        'title': current_title,
                        'level': current_level,
                        'content': '\n'.join(current_section)
                    })
                
                # Начинаем новую секцию
                current_level = len(heading_match.group(1))
                current_title = heading_match.group(2)
                current_section = []
            else:
                current_section.append(line)
        
        # Добавляем последнюю секцию
        if current_section:
            sections.append({
                'title': current_title,
                'level': current_level,
                'content': '\n'.join(current_section)
            })
        
        return sections
    
    def _chunk_section(self, section: Dict, metadata: Dict, ollama_api_url: str) -> List[Document]:
        """Выбирает оптимальный метод чанкинга для секции"""
        content = section['content']
        
        # Определяем тип контента
        content_type = self._detect_content_type(content)
        
        # Выбираем стратегию чанкинга в зависимости от типа контента
        if content_type == "code":
            return self._chunk_code(content, metadata, section)
        elif content_type == "table":
            return self._chunk_table(content, metadata, section)
        elif content_type == "list":
            return self._chunk_list(content, metadata, section)
        else:
            return self._chunk_text(content, metadata, section, ollama_api_url)
    
    def _detect_content_type(self, text: str) -> str:
        """Определяет тип контента"""
        lines = text.split('\n')
        
        # Проверяем на код
        code_lines = sum(1 for line in lines if line.startswith('    ') or '```' in line)
        if code_lines / max(1, len(lines)) > 0.3:
            return "code"
        
        # Проверяем на таблицу
        table_lines = sum(1 for line in lines if '|' in line and len(line.split('|')) > 2)
        if table_lines / max(1, len(lines)) > 0.3:
            return "table"
        
        # Проверяем на список
        list_lines = sum(1 for line in lines if re.match(r'^[\d+\.\-\*\+]\s', line))
        if list_lines / max(1, len(lines)) > 0.4:
            return "list"
        
        return "text"
    
    def _chunk_text(self, text: str, metadata: Dict, section: Dict, ollama_api_url: str) -> List[Document]:
        """Семантический чанкинг для текста"""
        try:
            # Пытаемся использовать семантический чанкинг
            embedding = OllamaEmbeddings(
                # model="jeffh/intfloat-multilingual-e5-large-instruct:f32",
                model=EMBEDDING_MODEL,
                base_url=ollama_api_url
            )
            
            # Разбиваем на абзацы
            paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
            if not paragraphs:
                return []
            
            # Получаем эмбеддинги
            vecs = embedding.embed_documents(paragraphs)
            
            chunks = []
            current_chunk = []
            current_tokens = 0
            prev_vec = None
            
            for para, vec in zip(paragraphs, vecs):
                para_tokens = self._count_tokens(para)
                
                # Проверяем семантическую схожесть
                sim = 1.0
                if prev_vec is not None:
                    sim = np.dot(vec, prev_vec) / (np.linalg.norm(vec) * np.linalg.norm(prev_vec))
                
                # Объединяем если схожесть высокая и не превышаем лимит токенов
                if (prev_vec is not None and sim >= 0.7 and
                    (current_tokens + para_tokens) <= self.max_tokens):
                    current_chunk.append(para)
                    current_tokens += para_tokens
                else:
                    # Сохраняем текущий чанк
                    if current_chunk:
                        chunks.append(self._create_chunk(current_chunk, metadata, section, "semantic"))
                    
                    # Начинаем новый
                    current_chunk = [para]
                    current_tokens = para_tokens
                    prev_vec = vec
            
            # Добавляем последний чанк
            if current_chunk:
                chunks.append(self._create_chunk(current_chunk, metadata, section, "semantic"))
                
            return chunks
            
        except Exception:
            # Fallback: рекурсивное разбиение по предложениям
            return self._chunk_by_sentences(text, metadata, section)
    
    def _chunk_by_sentences(self, text: str, metadata: Dict, section: Dict) -> List[Document]:
        """Чанкинг по предложениям (fallback)"""
        # Простое разбиение на абзацы с ограничением по токенам
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
        chunks = []
        current_chunk = []
        current_tokens = 0
        
        for para in paragraphs:
            para_tokens = self._count_tokens(para)
            
            if current_tokens + para_tokens > self.max_tokens and current_chunk:
                chunks.append(self._create_chunk(current_chunk, metadata, section, "paragraph"))
                current_chunk = []
                current_tokens = 0
            
            current_chunk.append(para)
            current_tokens += para_tokens
        
        if current_chunk:
            chunks.append(self._create_chunk(current_chunk, metadata, section, "paragraph"))
        
        return chunks
    
    def _chunk_code(self, code: str, metadata: Dict, section: Dict) -> List[Document]:
        """Чанкинг для кода"""
        # Сохраняем код как единый блок
        return [self._create_chunk([code], metadata, section, "code")]
    
    def _chunk_table(self, table: str, metadata: Dict, section: Dict) -> List[Document]:
        """Чанкинг для таблиц"""
        # Сохраняем таблицу как единый блок
        return [self._create_chunk([table], metadata, section, "table")]
    
    def _chunk_list(self, list_text: str, metadata: Dict, section: Dict) -> List[Document]:
        """Чанкинг для списков"""
        items = re.split(r'\n(?=[\d+\.\-\*\+]\s)', list_text)
        chunks = []
        current_chunk = []
        current_tokens = 0
        
        for item in items:
            item_tokens = self._count_tokens(item)
            
            if current_tokens + item_tokens > self.max_tokens and current_chunk:
                chunks.append(self._create_chunk(current_chunk, metadata, section, "list"))
                current_chunk = []
                current_tokens = 0
            
            current_chunk.append(item)
            current_tokens += item_tokens
        
        if current_chunk:
            chunks.append(self._create_chunk(current_chunk, metadata, section, "list"))
        
        return chunks
    
    def _create_chunk(self, content_parts: List[str], metadata: Dict, section: Dict, chunk_type: str) -> Document:
        """Создает документ чанка с метаданными"""
        content = '\n\n'.join(content_parts)
        chunk_metadata = {
            **metadata,
            'chunk_type': chunk_type,
            'section_title': section.get('title', ''),
            'section_level': section.get('level', 0),
            'token_count': self._count_tokens(content)
        }
        return Document(page_content=content, metadata=chunk_metadata)
    
    def _count_tokens(self, text: str) -> int:
        """Подсчет токенов"""
        if self.tokenizer:
            return len(self.tokenizer.encode(text))
        return len(text) // 4  # Примерная оценка

class _ApproxEncoder:
    def encode(self, s: str) -> List[int]:
        return [0] * _approx_token_count_bytes(s.encode("utf-8"))


def get_tiktoken_encoder():
    if tiktoken is None:
        return _ApproxEncoder()
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return _ApproxEncoder()


enc = get_tiktoken_encoder()

def split_into_chunks_semantic(
    docs: List[Document],
    ollama_api_url: str,
    # model: str = "jeffh/intfloat-multilingual-e5-large-instruct:f32",
    model: str = EMBEDDING_MODEL,
    threshold: float = 0.8,
    max_tokens: int = 500,
    tokenizer=None
) -> List[Document]:
    """Обертка для совместимости со старым кодом"""
    # Создаем чанкер с переданными параметрами
    chunker = AdvancedChunker(
        tokenizer=tokenizer or enc,
        max_tokens=max_tokens,
        overlap_tokens=int(max_tokens * 0.1)  # 10% overlap
    )
    
    # Используем улучшенный чанкинг
    return chunker.split_into_chunks(docs, ollama_api_url)


def _split_into_structural_blocks(text: str) -> List[Dict[str, str]]:
    """Разбивает текст на структурные блоки: заголовки, списки, параграфы"""
    blocks = []
    lines = text.split('\n')
    
    current_block = []
    current_type = None
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Определяем тип текущей строки
        line_type = _get_line_type(line)
        
        # Если тип изменился или это начало нового структурного элемента
        if current_type != line_type or line_type in ["heading", "list"]:
            # Сохраняем предыдущий блок
            if current_block and current_type:
                blocks.append({
                    "text": "\n".join(current_block),
                    "type": current_type
                })
            # Начинаем новый блок
            current_block = [line]
            current_type = line_type
        else:
            # Продолжаем текущий блок
            current_block.append(line)
    
    # Добавляем последний блок
    if current_block and current_type:
        blocks.append({
            "text": "\n".join(current_block),
            "type": current_type
        })
    
    return blocks


def _get_line_type(line: str) -> str:
    """Определяет тип строки"""
    # Заголовки (Markdown)
    if re.match(r'^#+\s+', line):
        return "heading"
    # Нумерованные списки
    if re.match(r'^\d+\.\s+', line):
        return "list"
    # Маркированные списки
    if re.match(r'^[-*+]\s+', line):
        return "list"
    # Блоки кода
    if line.startswith('```') or line.startswith('    '):
        return "code"
    # Таблицы
    if '|' in line and len(line.split('|')) > 2:
        return "table"
    # Обычный текст
    return "paragraph"

# =========================
# Chroma helpers
# =========================
def _get_client(db_path: str) -> chromadb.PersistentClient:
    return chromadb.PersistentClient(path=db_path, settings=Settings(anonymized_telemetry=False))

def getVectorstore(name: str, db_path: str, llm_base_url: str,
                   embedding_model: Optional[str] = None) -> Chroma:
    model = embedding_model or os.getenv("EMBEDDING_MODEL", EMBEDDING_MODEL )
    client = _get_client(db_path)
    
    # Используем LiteLLMEmbeddings (который мы создали ранее)
    embedding = LiteLLMEmbeddings(
        model=model,
        base_url=llm_base_url,
        api_key=st.secrets.get("LITELLM_API_KEY", "sk-fe1ZWrr7lPUN7tb8ZFlYEw")
    )
    
    return Chroma(client=client, collection_name=name, embedding_function=embedding)


def store2Chroma(
    docs: List[Document],
    collection_name: str,
    db_path: str,
    batch_size: int = 16,  # Уменьшил размер батча
    llm_base_url: str = None,
    embedding_model: Optional[str] = None,
):
    if llm_base_url is None:
        llm_base_url = st.secrets.get("OLLAMA_API_URL", "https://spb99akl-dgx02.gazprom-neft.local")
    
    api_key = st.secrets.get("LITELLM_API_KEY", "sk-fe1ZWrr7lPUN7tb8ZFlYEw")
    
    # Создаем клиент для проверки
    try:
        test_embeddings = LiteLLMEmbeddings(
            model=embedding_model or EMBEDDING_MODEL,
            base_url=llm_base_url,
            api_key=api_key
        )
        # Тестовый запрос
        test_embeddings.embed_query("test")
        st.success("✅ Подключение к liteLLM работает")
    except Exception as e:
        st.error(f"❌ Ошибка подключения к liteLLM: {e}")
        return None
    
    client = _get_client(db_path)
    client.get_or_create_collection(name=collection_name)

    model = embedding_model or os.getenv("EMBEDDING_MODEL", EMBEDDING_MODEL)
    
    embedding = LiteLLMEmbeddings(
        model=model,
        base_url=llm_base_url,
        api_key=api_key
    )
    
    vectorstore = Chroma(
        client=client,
        collection_name=collection_name,
        embedding_function=embedding
    )

    def _normalize(d: Document) -> Document:
        md = dict(getattr(d, "metadata", {}) or {})
        md.setdefault("type", "original")
        return Document(page_content=d.page_content, metadata=md)

    docs = [_normalize(d) for d in docs]
    total = len(docs)
    ok, bad = 0, 0
    pbar = st.progress(0.0, text="Начинаю загрузку…")

    for i in range(0, total, batch_size):
        batch = docs[i: i + batch_size]
        try:
            # Добавляем небольшую задержку между батчами
            time.sleep(0.5)
            vectorstore.add_documents(batch)
            ok += len(batch)
        except Exception as e:
            st.warning(f"⚠️ Ошибка на батче {i+1}-{i+len(batch)}: {e}")
            bad += len(batch)
        finally:
            pbar.progress(min((i + batch_size) / max(total, 1), 1.0),
                          text=f"Обработано: {min(i + batch_size, total)} / {total}")

    pbar.empty()
    st.success(f"✅ Готово. Успешно: {ok}, с ошибкой: {bad}")
    return client.get_collection(collection_name)


def remove_collection(name: str, db_path: str):
    client = _get_client(db_path)
    client.delete_collection(name)


# =========================
# Retrieval + RAG
# =========================
def getOpenAI(base_url: str) -> OpenAI:
    """Создает OpenAI клиент с правильными настройками для liteLLM"""
    from openai import OpenAI
    import urllib3
    import httpx
    
    # Отключаем warnings
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # Подготавливаем URL
    base_url = base_url.rstrip('/')
    if not base_url.endswith('/v1'):
        base_url = f"{base_url}/v1"
    
    # Создаем кастомный HTTPX клиент с отключенной проверкой SSL
    http_client = httpx.Client(
        verify=False,  # Отключаем проверку SSL
        timeout=float(LLM_TIMEOUT),
        headers={
            "Authorization": f"Bearer {st.secrets.get('LITELLM_API_KEY', 'unused')}",
            "Content-Type": "application/json"
        }
    )
    
    return OpenAI(
        base_url=base_url,
        api_key=st.secrets.get("LITELLM_API_KEY", "sk-fe1ZWrr7lPUN7tb8ZFlYEw"),
        http_client=http_client
    )

def gen_query_variants(openai: OpenAI, model: str, q: str, n: int = 3) -> List[str]:
    prompt = f"Дай {n} кратких переформулировок запроса; по одной на строку.\nЗапрос: {q}"
    try:
        r = openai.chat.completions.create(
            model=model, temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        lines = [s.strip("- ").strip() for s in (r.choices[0].message.content or "").splitlines() if s.strip()]
        return [q] + lines[:n]
    except Exception:
        return [q]


def rrf_merge(rank_lists: List[List[Document]], K: int = 60) -> List[Document]:
    scores: Dict[str, float] = {}
    pick: Dict[str, Document] = {}

    def key(d: Document) -> str:
        md = tuple(sorted((d.metadata or {}).items()))
        return f"{hash(d.page_content)}::{hash(md)}"

    for rl in rank_lists:
        for rank, d in enumerate(rl):
            k = key(d)
            scores[k] = scores.get(k, 0.0) + 1.0 / (K + rank + 1)
            pick.setdefault(k, d)
    return [pick[k] for k, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def _pid_from_meta(md: Dict) -> str:
    return str(md.get("page_id") or "unknown")


def _group_limit_per_page(docs: List[Document], per_page: int = 1) -> List[Document]:
    by: Dict[str, List[Document]] = {}
    for d in docs:
        pid = _pid_from_meta(d.metadata or {})
        by.setdefault(pid, []).append(d)
    picked: List[Document] = []
    for _, lst in by.items():
        picked.extend(lst[:per_page])
    return picked


def _build_sources(docs: List[Document]) -> str:
    lines = []
    seen = set()
    for d in docs:
        md = d.metadata or {}
        space = md.get("space_key")
        pid = md.get("page_id")
        url = md.get("url") or ""
        if not (space and pid):
            continue
        head = md.get("Header 1") or md.get("Header 2") or ""
        key = (space, pid, head)
        if key in seen:
            continue
        seen.add(key)
        line = f"- {space}:{pid} {head}".strip()
        if url:
            line += f" — {url}"
        lines.append(line)
    return "\n".join(lines)

def retrieve_docs(
    vectorstore: Chroma,
    query: str,
    use_multi_query: bool,
    openai: Optional[OpenAI],
    llm_model: Optional[str],
    k_single: int = 12,
    mq_variants: int = 3,
    k_per_variant: int = 6,
    total_top: int = 12,
    content_types: Optional[List[str]] = None
) -> List[Document]:
    """Улучшенный поиск с фильтрацией по типам контента"""
    
    # Определяем тип запроса для оптимальной фильтрации
    query_type = _classify_query_type(query)

    # Формируем список условий для $and
    conditions = [{"type": {"$eq": "original"}}]
    
    if content_types:
        conditions.append({"chunk_type": {"$in": content_types}})
    elif query_type == "code":
        conditions.append({"chunk_type": {"$eq": "code"}})
    elif query_type == "fact":
        conditions.append({"chunk_type": {"$in": ["text", "paragraph", "list"]}})
    
    # Если условие одно — передаём его напрямую, если несколько — оборачиваем в $and
    if len(conditions) == 1:
        search_filters = conditions[0]
    else:
        search_filters = {"$and": conditions}
    
    # # Настраиваем фильтры в зависимости от типа запроса - исправленный формат для ChromaDB
    # search_filters = {"type": {"$eq": "original"}}  # Исправлено: используем $eq
    
    # if content_types:
    #     # ChromaDB ожидает формат: {"chunk_type": {"$in": ["text", "paragraph", "list"]}}
    #     search_filters["chunk_type"] = {"$in": content_types}
    # elif query_type == "code":
    #     search_filters["chunk_type"] = {"$eq": "code"}  # Исправлено
    # elif query_type == "fact":
    #     # Для нескольких значений используем $in
    #     search_filters["chunk_type"] = {"$in": ["text", "paragraph", "list"]}
    # Для остальных типов не добавляем фильтр chunk_type
    
    if not use_multi_query or openai is None or not llm_model:
        retr = vectorstore.as_retriever(
            search_kwargs={
                "k": k_single,
                "filter": search_filters
            }
        )
        return retr.invoke(query) or []

    variants = gen_query_variants(openai, llm_model, query, n=mq_variants)
    rank_lists: List[List[Document]] = []
    errors: List[str] = []

    for v in variants:
        try:
            retr = vectorstore.as_retriever(
                search_kwargs={
                    "k": k_per_variant,
                    "filter": search_filters
                }
            )
            results = retr.invoke(v) or []
            rank_lists.append(results)
        except Exception as e:
            print(f"Ошибка при поиске для варианта '{v}': {e}")
            errors.append(str(e))
            rank_lists.append([])

    merged = rrf_merge(rank_lists)

    # Если все варианты упали с ошибкой — пробрасываем
    if not merged and errors:
        raise RuntimeError(f"Ошибка поиска по векторной БД: {errors[0]}")

    # Переранжируем результаты с учетом релевантности
    reranked = _rerank_results(merged, query, query_type)

    return reranked[:total_top]


def _classify_query_type(query: str) -> str:
    """Классифицирует тип запроса"""
    query_lower = query.lower()
    
    if any(word in query_lower for word in ['код', 'пример', 'функция', 'метод', 'класс']):
        return "code"
    elif any(word in query_lower for word in ['таблица', 'данные', 'статистика']):
        return "table"
    elif any(word in query_lower for word in ['как', 'инструкция', 'шаги', 'сделать']):
        return "howto"
    elif any(word in query_lower for word in ['что', 'определение', 'понятие', 'такое']):
        return "fact"
    
    return "general"


def _rerank_results(docs: List[Document], query: str, query_type: str) -> List[Document]:
    """Переранжирование результатов на основе дополнительных критериев"""
    scored_docs = []
    
    for doc in docs:
        score = 1.0
        content = doc.page_content.lower()
        metadata = doc.metadata or {}
        chunk_type = metadata.get('chunk_type', '')
        
        # Бонус за соответствие типа контента
        if (query_type == "code" and chunk_type == "code") or \
           (query_type == "table" and chunk_type == "table"):
            score *= 1.5
        
        # Бонус за заголовки секций
        if metadata.get('section_level', 0) > 0:
            score *= 1.2
        
        # Штраф за слишком короткие/длинные чанки
        token_count = metadata.get('token_count', 0)
        if token_count < 50 or token_count > 1000:
            score *= 0.8
        
        scored_docs.append((doc, score))
    
    # Сортируем по score
    scored_docs.sort(key=lambda x: x[1], reverse=True)
    return [doc for doc, score in scored_docs]


def generate_answer_with_context(
    embed_collection_name: str,
    query: str,
    model: str,
    top_n: int,
    db_path: str,
    llm_base_url: str,
    # ollama_openai_url: str,
    # ollama_api_url: Optional[str] = None,
    embedding_model: Optional[str] = None,
    use_multi_query: bool = True,
    mq_variants: int = 3,
    k_per_variant: int = 6,
    variant_offset: int = 0,
    exclude_page_ids: Optional[List[str]] = None,
    answers_per_variant: int = 3,
) -> str:
    print(f"server={llm_base_url} \nmodel={model} \nquery={query} \nembed_collection_name={embed_collection_name}")
    ollama_openai_url=llm_base_url
    # ollama_api_url=llm_base_url
    try:
        openai = getOpenAI(llm_base_url)
    except Exception as e:
        return f"Ошибка подключения к LLM ({llm_base_url}): {e}"

    base_api = (ollama_openai_url.replace("/v1", "")).rstrip("/")
    try:
        vectorstore = getVectorstore(embed_collection_name, db_path=db_path,
                                     llm_base_url=base_api, embedding_model=embedding_model)
    except Exception as e:
        return f"Ошибка подключения к векторной БД (коллекция '{embed_collection_name}'): {e}"

    print(vectorstore)
    try:
        cands = retrieve_docs(
            vectorstore, query,
            use_multi_query=use_multi_query,
            openai=openai, llm_model=model,
            k_single=max(top_n, 12),
            mq_variants=mq_variants,
            k_per_variant=k_per_variant,
            total_top=max(top_n, 12),
        )
    except Exception as e:
        return f"Ошибка поиска документов: {e}"

    if not cands:
        return "Я не нашёл релевантный контекст по вашей коллекции."

    cands = _group_limit_per_page(cands, per_page=1)

    if exclude_page_ids:
        excl = set(str(x) for x in exclude_page_ids)
        cands = [d for d in cands if _pid_from_meta(d.metadata or {}) not in excl]

    start = max(0, variant_offset * answers_per_variant)
    selected = cands[start:start + answers_per_variant] or cands[:answers_per_variant]

    context = "\n\n".join(d.page_content for d in selected)
    sources_block = _build_sources(selected)

    try:
        resp = openai.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                 "content": ("Ты — эксперт по корпоративной базе знаний. "
                             "Отвечай ТОЛЬКО по предоставленному контексту, не ищи ничего в интернете.")},
                {"role": "user",
                 "content": f"Контекст:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."},
            ],
            temperature=0,
        )
        answer = resp.choices[0].message.content
    except Exception as e:
        return f"Не удалось сгенерировать ответ: {e}"

    if sources_block:
        answer = f"{answer}\n\n---\n**Источники:**\n{sources_block}"
    return answer

# =========================
# Confluence ingest
# =========================
def list_space_page_ids(
    base_url: str,
    token: str,
    space_key: str,
    verify_ssl: bool = False,
    page_limit: int = 200,
    timeout: int = 20,
) -> List[str]:
    headers = {"Authorization": f"Bearer {token}"}
    start = 0
    ids: List[str] = []
    while True:
        params = {"spaceKey": space_key, "type": "page", "limit": page_limit, "start": start}
        r = requests.get(f"{base_url.rstrip('/')}/rest/api/content",
                         headers=headers, params=params, verify=verify_ssl, timeout=timeout)
        r.raise_for_status()
        data = r.json() or {}
        results = data.get("results", []) or []
        if not results:
            break
        ids.extend(str(it.get("id")) for it in results if it.get("id"))
        if len(results) < page_limit:
            break
        start += len(results)
    return ids


def normalize_as_original(docs: List[Document], space_key: str, page_id: str, base_url: Optional[str] = None) -> List[Document]:
    out: List[Document] = []
    url = f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}" if base_url else None
    for d in docs:
        md = dict(getattr(d, "metadata", {}) or {})
        md.setdefault("type", "original")
        md["source"] = "confluence"
        md["space_key"] = space_key
        md["page_id"] = page_id
        if url:
            md.setdefault("url", url)
        out.append(Document(page_content=d.page_content, metadata=md))
    return out


def make_chunk_ids(space_key: str, page_id: str, count: int, suffix: str = "") -> List[str]:
    return [f"{space_key}-{page_id}-{i:03d}{suffix}" for i in range(count)]


def append_documents(
    collection_name: str,
    docs: List[Document],
    ids: List[str],
    db_path: str,
    ollama_api_url: str,
    embedding_model: Optional[str] = None,
) -> Tuple[int, int]:
    vs = getVectorstore(collection_name, db_path=db_path, llm_base_url=ollama_api_url, embedding_model=embedding_model)
    ok = 0
    bad = 0
    try:
        vs.add_documents(docs, ids=ids)
        ok += len(docs)
    except Exception:
        for d, _id in zip(docs, ids):
            try:
                vs.add_documents([d], ids=[_id])
                ok += 1
            except Exception:
                bad += 1
    return ok, bad


def ingest_space_incremental(
    base_url: str,
    token: str,
    space_key: str,
    collection_name: str,
    db_path: str,
    ollama_api_url: str,
    summarize: bool = False,
    max_pages: Optional[int] = None,
    verify_ssl: bool = False,
    embedding_model: Optional[str] = None,
) -> Tuple[int, int, int]:
    from langchain_community.document_loaders import ConfluenceLoader
    from langchain_community.document_loaders.confluence import ContentFormat

    page_ids = list_space_page_ids(base_url, token, space_key, verify_ssl=verify_ssl)
    if not page_ids:
        return (0, 0, 0)

    if max_pages is not None and max_pages > 0:
        page_ids = page_ids[:max_pages]

    processed_pages = 0
    ok_docs = 0
    bad_docs = 0

    total_pages = len(page_ids)
    pbar = st.progress(0.0, text=f"Готовлюсь… 0 / {total_pages}")

    for idx, pid in enumerate(page_ids, start=1):
        try:
            loader = ConfluenceLoader(
                url=base_url,
                token=token,
                include_attachments=False,
                keep_markdown_format=True,
                content_format=ContentFormat.EXPORT_VIEW,
                page_ids=[pid],
                confluence_kwargs={"verify_ssl": verify_ssl},
                limit=1,
            )
            docs = loader.load()
            if not docs:
                processed_pages += 1
                pbar.progress(idx / total_pages, text=f"Страница {idx}/{total_pages}: пусто")
                continue

            chunks = split_into_chunks_semantic(
                docs, ollama_api_url=ollama_api_url, tokenizer=enc, max_tokens=500
            )

            prepared = normalize_as_original(chunks, space_key, pid, base_url=base_url)
            ids = make_chunk_ids(space_key, pid, len(prepared))

            _ok, _bad = append_documents(
                collection_name,
                prepared,
                ids,
                db_path=db_path,
                ollama_api_url=ollama_api_url,
                embedding_model=embedding_model,
            )
            ok_docs += _ok
            bad_docs += _bad
            processed_pages += 1

            pbar.progress(
                idx / total_pages,
                text=f"Стр. {idx}/{total_pages} (pageId={pid}) — добавлено {_ok}, ошибок {_bad}",
            )
        except Exception as e:
            processed_pages += 1
            pbar.progress(
                idx / total_pages,
                text=f"Стр. {idx}/{total_pages} (pageId={pid}) — ошибка: {e}",
            )

    pbar.empty()
    return processed_pages, ok_docs, bad_docs




from langchain.embeddings.base import Embeddings
import requests
from typing import List, Optional
import time


import httpx
from langchain.embeddings.base import Embeddings

class LiteLLMEmbeddings(Embeddings):
    """Кастомный класс эмбеддингов для liteLLM с httpx"""
    
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str = "unused",
        timeout: int = None
    ):
        self.model = model
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key

        # Создаем HTTPX клиент
        self.client = httpx.Client(
            verify=False,  # Отключаем проверку SSL
            timeout=float(timeout if timeout is not None else EMBEDDING_TIMEOUT),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
        )
        
        # Отключаем warnings
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    def _embed(self, texts: List[str], prefix: str = "") -> List[List[float]]:
        """Внутренний метод для получения эмбеддингов"""
        # Добавляем префикс для E5 модели
        if "e5" in self.model.lower():
            texts = [f"{prefix}{t}" for t in texts]
        
        response = self.client.post(
            f"{self.base_url}/v1/embeddings",
            json={
                "model": self.model,
                "input": texts
            }
        )
        
        if response.status_code != 200:
            raise Exception(f"Error from liteLLM: {response.status_code} - {response.text}")
        
        data = response.json()
        return [item["embedding"] for item in data["data"]]
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Эмбеддинги для документов"""
        return self._embed(texts, prefix="passage: ")
    
    def embed_query(self, text: str) -> List[float]:
        """Эмбеддинги для запроса"""
        result = self._embed([text], prefix="query: ")
        return result[0]
    
    def __del__(self):
        """Закрываем клиент при удалении"""
        if hasattr(self, 'client'):
            self.client.close()

# Замените импорты
from langchain_openai import OpenAIEmbeddings  # вместо OllamaEmbeddings
# Удалите: from langchain_community.embeddings import OllamaEmbeddings

# И замените класс E5OllamaEmbeddings на:
class E5OllamaEmbeddings(OpenAIEmbeddings):
    """Адаптер для liteLLM с префиксами E5"""
    
    def __init__(
        self,
        model: str = EMBEDDING_MODEL,
        base_url: Optional[str] = None,
        api_key: str = "sk-fe1ZWrr7lPUN7tb8ZFlYEw",  # Ваш API ключ
        **kwargs
    ):
        super().__init__(
            model=model,
            openai_api_base=base_url.rstrip('/') + '/v1',  # Важно: добавляем /v1
            openai_api_key=api_key,
            **kwargs
        )
    
    def embed_query(self, text: str):
        return super().embed_query(f"query: {text}")
    
    def embed_documents(self, texts: List[str]):
        return super().embed_documents([f"passage: {t}" for t in texts])



# # v 8 n1helper  - раРАБОЧАЯ ВЕРСИЯ 
# from __future__ import annotations
# import os, re, uuid

# import re
# import requests
# import streamlit as st
# import numpy as np

# from typing import List, Dict, Tuple, Optional
# import numpy as np


# # from langchain.schema import Document
# # Используйте это (РАБОТАЕТ):
# from langchain_core.documents import Document
# from langchain_core.messages import HumanMessage
# from langchain_core.output_parsers import StrOutputParser
# from langchain_core.prompts import ChatPromptTemplate
# from langchain_core.runnables import RunnableMap
# from langchain_chroma import Chroma
# from langchain_community.embeddings import OllamaEmbeddings
# from langchain_community.chat_models import ChatOllama
# # from langchain.chains.combine_documents import create_stuff_documents_chain
# from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
# from langchain_core.messages import SystemMessage

# import chromadb
# from chromadb.config import Settings
# from openai import OpenAI

# import urllib3
# urllib3.disable_warnings()
# # =========================
# # tiktoken (офлайн безопасно)
# # =========================
# try:
#     import tiktoken  # type: ignore
# except Exception:
#     tiktoken = None

# # В самом начале файла, после импортов
# import ssl
# import urllib3
# import warnings

# # Отключаем все SSL предупреждения
# urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# # Создаем непривязанный SSL контекст
# try:
#     _create_unverified_https_context = ssl._create_unverified_context
# except AttributeError:
#     pass
# else:
#     ssl._create_default_https_context = _create_unverified_https_context


# def _approx_token_count_bytes(b: bytes) -> int:
#     return (len(b) + 3) // 4


# import ssl
# import certifi
# import urllib3
# # Отключаем предупреждения
# urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# # Создаем кастомную сессию с отключенной проверкой SSL
# def create_ssl_session():
#     session = requests.Session()
#     session.verify = False  # Отключаем проверку SSL
#     session.headers.update({
#         "Authorization": "Bearer sk-fe1ZWrr7lPUN7tb8ZFlYEw",
#         "Content-Type": "application/json"
#     })
#     return session
    

# class AdvancedChunker:
#     def __init__(self, tokenizer=None, max_tokens: int = 512, overlap_tokens: int = 50):
#         self.tokenizer = tokenizer
#         self.max_tokens = max_tokens
#         self.overlap_tokens = overlap_tokens
    
#     def split_into_chunks(self, docs: List[Document], ollama_api_url: str) -> List[Document]:
#         """Многоуровневый чанкинг с сохранением структуры"""
#         all_chunks = []
        
#         for doc in docs:
#             chunks = self._process_document(doc, ollama_api_url)
#             all_chunks.extend(chunks)
        
#         return all_chunks
    
#     def _process_document(self, doc: Document, ollama_api_url: str) -> List[Document]:
#         """Обрабатывает один документ"""
#         text = doc.page_content
#         metadata = doc.metadata.copy()
        
#         # 1. Разбиваем на структурные секции
#         sections = self._split_into_sections(text)
        
#         # 2. Для каждой секции применяем оптимальный метод чанкинга
#         chunks = []
#         for section in sections:
#             section_chunks = self._chunk_section(section, metadata, ollama_api_url)
#             chunks.extend(section_chunks)
        
#         return chunks
    
#     def _split_into_sections(self, text: str) -> List[Dict]:
#         """Разбивает текст на логические секции по заголовкам"""
#         lines = text.split('\n')
#         sections = []
#         current_section = []
#         current_title = "Без названия"
#         current_level = 0
        
#         for line in lines:
#             line = line.strip()
#             if not line:
#                 continue
            
#             # Определяем заголовок
#             heading_match = re.match(r'^(#+)\s+(.+)$', line)
#             if heading_match:
#                 # Сохраняем предыдущую секцию
#                 if current_section:
#                     sections.append({
#                         'title': current_title,
#                         'level': current_level,
#                         'content': '\n'.join(current_section)
#                     })
                
#                 # Начинаем новую секцию
#                 current_level = len(heading_match.group(1))
#                 current_title = heading_match.group(2)
#                 current_section = []
#             else:
#                 current_section.append(line)
        
#         # Добавляем последнюю секцию
#         if current_section:
#             sections.append({
#                 'title': current_title,
#                 'level': current_level,
#                 'content': '\n'.join(current_section)
#             })
        
#         return sections
    
#     def _chunk_section(self, section: Dict, metadata: Dict, ollama_api_url: str) -> List[Document]:
#         """Выбирает оптимальный метод чанкинга для секции"""
#         content = section['content']
        
#         # Определяем тип контента
#         content_type = self._detect_content_type(content)
        
#         # Выбираем стратегию чанкинга в зависимости от типа контента
#         if content_type == "code":
#             return self._chunk_code(content, metadata, section)
#         elif content_type == "table":
#             return self._chunk_table(content, metadata, section)
#         elif content_type == "list":
#             return self._chunk_list(content, metadata, section)
#         else:
#             return self._chunk_text(content, metadata, section, ollama_api_url)
    
#     def _detect_content_type(self, text: str) -> str:
#         """Определяет тип контента"""
#         lines = text.split('\n')
        
#         # Проверяем на код
#         code_lines = sum(1 for line in lines if line.startswith('    ') or '```' in line)
#         if code_lines / max(1, len(lines)) > 0.3:
#             return "code"
        
#         # Проверяем на таблицу
#         table_lines = sum(1 for line in lines if '|' in line and len(line.split('|')) > 2)
#         if table_lines / max(1, len(lines)) > 0.3:
#             return "table"
        
#         # Проверяем на список
#         list_lines = sum(1 for line in lines if re.match(r'^[\d+\.\-\*\+]\s', line))
#         if list_lines / max(1, len(lines)) > 0.4:
#             return "list"
        
#         return "text"
    
#     def _chunk_text(self, text: str, metadata: Dict, section: Dict, ollama_api_url: str) -> List[Document]:
#         """Семантический чанкинг для текста"""
#         try:
#             # Пытаемся использовать семантический чанкинг
#             embedding = OllamaEmbeddings(
#                 model="jeffh/intfloat-multilingual-e5-large-instruct:f32",
#                 base_url=ollama_api_url
#             )
            
#             # Разбиваем на абзацы
#             paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
#             if not paragraphs:
#                 return []
            
#             # Получаем эмбеддинги
#             vecs = embedding.embed_documents(paragraphs)
            
#             chunks = []
#             current_chunk = []
#             current_tokens = 0
#             prev_vec = None
            
#             for para, vec in zip(paragraphs, vecs):
#                 para_tokens = self._count_tokens(para)
                
#                 # Проверяем семантическую схожесть
#                 sim = 1.0
#                 if prev_vec is not None:
#                     sim = np.dot(vec, prev_vec) / (np.linalg.norm(vec) * np.linalg.norm(prev_vec))
                
#                 # Объединяем если схожесть высокая и не превышаем лимит токенов
#                 if (prev_vec is not None and sim >= 0.7 and 
#                     (current_tokens + para_tokens) <= self.max_tokens):
#                     current_chunk.append(para)
#                     current_tokens += para_tokens
#                 else:
#                     # Сохраняем текущий чанк
#                     if current_chunk:
#                         chunks.append(self._create_chunk(current_chunk, metadata, section, "semantic"))
                    
#                     # Начинаем новый
#                     current_chunk = [para]
#                     current_tokens = para_tokens
#                     prev_vec = vec
            
#             # Добавляем последний чанк
#             if current_chunk:
#                 chunks.append(self._create_chunk(current_chunk, metadata, section, "semantic"))
                
#             return chunks
            
#         except Exception:
#             # Fallback: рекурсивное разбиение по предложениям
#             return self._chunk_by_sentences(text, metadata, section)
    
#     def _chunk_by_sentences(self, text: str, metadata: Dict, section: Dict) -> List[Document]:
#         """Чанкинг по предложениям (fallback)"""
#         # Простое разбиение на абзацы с ограничением по токенам
#         paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
#         chunks = []
#         current_chunk = []
#         current_tokens = 0
        
#         for para in paragraphs:
#             para_tokens = self._count_tokens(para)
            
#             if current_tokens + para_tokens > self.max_tokens and current_chunk:
#                 chunks.append(self._create_chunk(current_chunk, metadata, section, "paragraph"))
#                 current_chunk = []
#                 current_tokens = 0
            
#             current_chunk.append(para)
#             current_tokens += para_tokens
        
#         if current_chunk:
#             chunks.append(self._create_chunk(current_chunk, metadata, section, "paragraph"))
        
#         return chunks
    
#     def _chunk_code(self, code: str, metadata: Dict, section: Dict) -> List[Document]:
#         """Чанкинг для кода"""
#         # Сохраняем код как единый блок
#         return [self._create_chunk([code], metadata, section, "code")]
    
#     def _chunk_table(self, table: str, metadata: Dict, section: Dict) -> List[Document]:
#         """Чанкинг для таблиц"""
#         # Сохраняем таблицу как единый блок
#         return [self._create_chunk([table], metadata, section, "table")]
    
#     def _chunk_list(self, list_text: str, metadata: Dict, section: Dict) -> List[Document]:
#         """Чанкинг для списков"""
#         items = re.split(r'\n(?=[\d+\.\-\*\+]\s)', list_text)
#         chunks = []
#         current_chunk = []
#         current_tokens = 0
        
#         for item in items:
#             item_tokens = self._count_tokens(item)
            
#             if current_tokens + item_tokens > self.max_tokens and current_chunk:
#                 chunks.append(self._create_chunk(current_chunk, metadata, section, "list"))
#                 current_chunk = []
#                 current_tokens = 0
            
#             current_chunk.append(item)
#             current_tokens += item_tokens
        
#         if current_chunk:
#             chunks.append(self._create_chunk(current_chunk, metadata, section, "list"))
        
#         return chunks
    
#     def _create_chunk(self, content_parts: List[str], metadata: Dict, section: Dict, chunk_type: str) -> Document:
#         """Создает документ чанка с метаданными"""
#         content = '\n\n'.join(content_parts)
#         chunk_metadata = {
#             **metadata,
#             'chunk_type': chunk_type,
#             'section_title': section.get('title', ''),
#             'section_level': section.get('level', 0),
#             'token_count': self._count_tokens(content)
#         }
#         return Document(page_content=content, metadata=chunk_metadata)
    
#     def _count_tokens(self, text: str) -> int:
#         """Подсчет токенов"""
#         if self.tokenizer:
#             return len(self.tokenizer.encode(text))
#         return len(text) // 4  # Примерная оценка

# class _ApproxEncoder:
#     def encode(self, s: str) -> List[int]:
#         return [0] * _approx_token_count_bytes(s.encode("utf-8"))


# def get_tiktoken_encoder():
#     if tiktoken is None:
#         return _ApproxEncoder()
#     try:
#         return tiktoken.get_encoding("cl100k_base")
#     except Exception:
#         return _ApproxEncoder()


# enc = get_tiktoken_encoder()


# # =========================
# # Эмбеддинги E5 с префиксами
# # =========================
# # class E5OllamaEmbeddings(OllamaEmbeddings):
# #     def embed_query(self, text: str):
# #         return super().embed_query(f"query: {text}")

# #     def embed_documents(self, texts: List[str]):
# #         return super().embed_documents([f"passage: {t}" for t in texts])


# # =========================
# # Семантический чанкинг
# # =========================
# # def split_into_chunks_semantic(
# #     docs: List[Document],
# #     ollama_api_url: str,
# #     model: str = "jeffh/intfloat-multilingual-e5-large-instruct:f32",
# #     threshold: float = 0.8,
# #     max_tokens: int = 500,
# #     tokenizer=None
# # ) -> List[Document]:
# #     """Комбинированный чанкинг: семантический + структурированный"""
# #     embedding = OllamaEmbeddings(model=model, base_url=ollama_api_url)
# #     out_docs: List[Document] = []

# #     for d in docs:
# #         text = d.page_content
# #         metadata = d.metadata.copy()
        
# #         # 1. Сначала разбиваем на структурные блоки (заголовки, списки, параграфы)
# #         structural_blocks = _split_into_structural_blocks(text)
        
# #         if not structural_blocks:
# #             continue

# #         # 2. Получаем эмбеддинги для структурных блоков
# #         block_texts = [block["text"] for block in structural_blocks]
# #         try:
# #             vecs = embedding.embed_documents(block_texts)
# #         except Exception:
# #             # Если эмбеддинги не работают, используем только структурное разбиение
# #             for block in structural_blocks:
# #                 if block["text"].strip():
# #                     out_docs.append(Document(
# #                         page_content=block["text"],
# #                         metadata={**metadata, "block_type": block["type"]}
# #                     ))
# #             continue

# #         # 3. Объединяем семантически похожие блоки
# #         current_chunk_parts = []
# #         current_tokens = 0
# #         prev_vec = None
# #         prev_block_type = None

# #         for block, vec in zip(structural_blocks, vecs):
# #             block_text = block["text"]
# #             block_type = block["type"]
            
# #             # Токенизация блока
# #             block_tokens = len(tokenizer.encode(block_text)) if tokenizer else len(block_text) // 4
            
# #             # Проверяем семантическую схожесть
# #             sim = 1.0
# #             if prev_vec is not None:
# #                 sim = np.dot(vec, prev_vec) / (np.linalg.norm(vec) * np.linalg.norm(prev_vec))
            
# #             # Проверяем, можно ли объединить с предыдущим блоком
# #             can_merge = (
# #                 prev_vec is not None and 
# #                 sim >= threshold and 
# #                 (current_tokens + block_tokens) <= max_tokens and
# #                 # Не объединяем заголовки с обычным текстом
# #                 not (prev_block_type == "heading" and block_type != "heading")
# #             )
            
# #             if can_merge:
# #                 current_chunk_parts.append(block_text)
# #                 current_tokens += block_tokens
# #             else:
# #                 # Сохраняем текущий чанк
# #                 if current_chunk_parts:
# #                     out_docs.append(Document(
# #                         page_content="\n\n".join(current_chunk_parts),
# #                         metadata={**metadata, "chunk_type": "semantic"}
# #                     ))
                
# #                 # Начинаем новый чанк
# #                 current_chunk_parts = [block_text]
# #                 current_tokens = block_tokens
            
# #             prev_vec = vec
# #             prev_block_type = block_type

# #         # Добавляем последний чанк
# #         if current_chunk_parts:
# #             out_docs.append(Document(
# #                 page_content="\n\n".join(current_chunk_parts),
# #                 metadata={**metadata, "chunk_type": "semantic"}
# #             ))

# #     return out_docs

# # def split_into_chunks_semantic(
# #     docs: List[Document],
# #     ollama_api_url: str,
# #     model: str = "jeffh/intfloat-multilingual-e5-large-instruct:f32",
# #     threshold: float = 0.8,
# #     max_tokens: int = 500,
# #     tokenizer=None
# # ) -> List[Document]:
# #     """Совместимость со старым кодом - использует улучшенный чанкинг"""
# #     chunker = AdvancedChunker(
# #         tokenizer=tokenizer or enc,
# #         max_tokens=max_tokens,
# #         overlap_tokens=50
# #     )
# #     return chunker.split_into_chunks(docs, ollama_api_url)


# def split_into_chunks_semantic(
#     docs: List[Document],
#     ollama_api_url: str,
#     model: str = "jeffh/intfloat-multilingual-e5-large-instruct:f32",
#     threshold: float = 0.8,
#     max_tokens: int = 500,
#     tokenizer=None
# ) -> List[Document]:
#     """Обертка для совместимости со старым кодом"""
#     # Создаем чанкер с переданными параметрами
#     chunker = AdvancedChunker(
#         tokenizer=tokenizer or enc,
#         max_tokens=max_tokens,
#         overlap_tokens=int(max_tokens * 0.1)  # 10% overlap
#     )
    
#     # Используем улучшенный чанкинг
#     return chunker.split_into_chunks(docs, ollama_api_url)


# def _split_into_structural_blocks(text: str) -> List[Dict[str, str]]:
#     """Разбивает текст на структурные блоки: заголовки, списки, параграфы"""
#     blocks = []
#     lines = text.split('\n')
    
#     current_block = []
#     current_type = None
    
#     for line in lines:
#         line = line.strip()
#         if not line:
#             continue
            
#         # Определяем тип текущей строки
#         line_type = _get_line_type(line)
        
#         # Если тип изменился или это начало нового структурного элемента
#         if current_type != line_type or line_type in ["heading", "list"]:
#             # Сохраняем предыдущий блок
#             if current_block and current_type:
#                 blocks.append({
#                     "text": "\n".join(current_block),
#                     "type": current_type
#                 })
#             # Начинаем новый блок
#             current_block = [line]
#             current_type = line_type
#         else:
#             # Продолжаем текущий блок
#             current_block.append(line)
    
#     # Добавляем последний блок
#     if current_block and current_type:
#         blocks.append({
#             "text": "\n".join(current_block),
#             "type": current_type
#         })
    
#     return blocks


# def _get_line_type(line: str) -> str:
#     """Определяет тип строки"""
#     # Заголовки (Markdown)
#     if re.match(r'^#+\s+', line):
#         return "heading"
#     # Нумерованные списки
#     if re.match(r'^\d+\.\s+', line):
#         return "list"
#     # Маркированные списки
#     if re.match(r'^[-*+]\s+', line):
#         return "list"
#     # Блоки кода
#     if line.startswith('```') or line.startswith('    '):
#         return "code"
#     # Таблицы
#     if '|' in line and len(line.split('|')) > 2:
#         return "table"
#     # Обычный текст
#     return "paragraph"

# # =========================
# # Chroma helpers
# # =========================
# def _get_client(db_path: str) -> chromadb.PersistentClient:
#     return chromadb.PersistentClient(path=db_path, settings=Settings(anonymized_telemetry=False))


# # def getVectorstore(name: str, db_path: str, ollama_api_url: str,
# #                    embedding_model: Optional[str] = None) -> Chroma:
# #     model = embedding_model or os.getenv("EMBEDDING_MODEL", "jeffh/intfloat-multilingual-e5-large-instruct:f32")
# #     client = _get_client(db_path)
# #     embedding = E5OllamaEmbeddings(model=model, base_url=ollama_api_url)
# #     return Chroma(client=client, collection_name=name, embedding_function=embedding)
# def getVectorstore(name: str, db_path: str, llm_base_url: str,
#                    embedding_model: Optional[str] = None) -> Chroma:
#     model = embedding_model or os.getenv("EMBEDDING_MODEL", "jeffh/intfloat-multilingual-e5-large-instruct:f32")
#     client = _get_client(db_path)
    
#     # Используем LiteLLMEmbeddings (который мы создали ранее)
#     embedding = LiteLLMEmbeddings(
#         model=model,
#         base_url=llm_base_url,
#         api_key=st.secrets.get("LITELLM_API_KEY", "sk-fe1ZWrr7lPUN7tb8ZFlYEw")
#     )
    
#     return Chroma(client=client, collection_name=name, embedding_function=embedding)


# def store2Chroma(
#     docs: List[Document],
#     collection_name: str,
#     db_path: str,
#     batch_size: int = 16,  # Уменьшил размер батча
#     llm_base_url: str = None,
#     embedding_model: Optional[str] = None,
# ):
#     if llm_base_url is None:
#         llm_base_url = st.secrets.get("OLLAMA_API_URL", "https://spb99akl-dgx02.gazprom-neft.local")
    
#     api_key = st.secrets.get("LITELLM_API_KEY", "sk-fe1ZWrr7lPUN7tb8ZFlYEw")
    
#     # Создаем клиент для проверки
#     try:
#         test_embeddings = LiteLLMEmbeddings(
#             model=embedding_model or "jeffh/intfloat-multilingual-e5-large-instruct:f32",
#             base_url=llm_base_url,
#             api_key=api_key
#         )
#         # Тестовый запрос
#         test_embeddings.embed_query("test")
#         st.success("✅ Подключение к liteLLM работает")
#     except Exception as e:
#         st.error(f"❌ Ошибка подключения к liteLLM: {e}")
#         return None
    
#     client = _get_client(db_path)
#     client.get_or_create_collection(name=collection_name)

#     model = embedding_model or os.getenv("EMBEDDING_MODEL", "jeffh/intfloat-multilingual-e5-large-instruct:f32")
    
#     embedding = LiteLLMEmbeddings(
#         model=model,
#         base_url=llm_base_url,
#         api_key=api_key
#     )
    
#     vectorstore = Chroma(
#         client=client, 
#         collection_name=collection_name, 
#         embedding_function=embedding
#     )

#     def _normalize(d: Document) -> Document:
#         md = dict(getattr(d, "metadata", {}) or {})
#         md.setdefault("type", "original")
#         return Document(page_content=d.page_content, metadata=md)

#     docs = [_normalize(d) for d in docs]
#     total = len(docs)
#     ok, bad = 0, 0
#     pbar = st.progress(0.0, text="Начинаю загрузку…")

#     for i in range(0, total, batch_size):
#         batch = docs[i: i + batch_size]
#         try:
#             # Добавляем небольшую задержку между батчами
#             time.sleep(0.5)
#             vectorstore.add_documents(batch)
#             ok += len(batch)
#         except Exception as e:
#             st.warning(f"⚠️ Ошибка на батче {i+1}-{i+len(batch)}: {e}")
#             bad += len(batch)
#         finally:
#             pbar.progress(min((i + batch_size) / max(total, 1), 1.0),
#                           text=f"Обработано: {min(i + batch_size, total)} / {total}")

#     pbar.empty()
#     st.success(f"✅ Готово. Успешно: {ok}, с ошибкой: {bad}")
#     return client.get_collection(collection_name)
    
# # def store2Chroma(
# #     docs: List[Document],
# #     collection_name: str,
# #     db_path: str,
# #     batch_size: int = 32,
# #     ollama_api_url: str = "http://127.0.0.1:11434",
# #     embedding_model: Optional[str] = None,
# # ):
# #     client = _get_client(db_path)
# #     client.get_or_create_collection(name=collection_name)

# #     model = embedding_model or os.getenv("EMBEDDING_MODEL", "jeffh/intfloat-multilingual-e5-large-instruct:f32")
# #     embedding = E5OllamaEmbeddings(model=model, base_url=ollama_api_url)
# #     vectorstore = Chroma(client=client, collection_name=collection_name, embedding_function=embedding)

# #     def _normalize(d: Document) -> Document:
# #         md = dict(getattr(d, "metadata", {}) or {})
# #         md.setdefault("type", "original")
# #         return Document(page_content=d.page_content, metadata=md)

# #     docs = [_normalize(d) for d in docs]
# #     total = len(docs)
# #     ok, bad = 0, 0
# #     pbar = st.progress(0.0, text="Начинаю загрузку…")

# #     for i in range(0, total, batch_size):
# #         batch = docs[i: i + batch_size]
# #         try:
# #             vectorstore.add_documents(batch)
# #             ok += len(batch)
# #         except Exception as e:
# #             st.warning(f"⚠️ Ошибка на батче {i+1}-{i+len(batch)}: {e}")
# #             bad += len(batch)
# #         finally:
# #             pbar.progress(min((i + batch_size) / max(total, 1), 1.0),
# #                           text=f"Обработано: {min(i + batch_size, total)} / {total}")

# #     pbar.empty()
# #     st.success(f"✅ Готово. Успешно: {ok}, с ошибкой: {bad}")
# #     return client.get_collection(collection_name)


# def remove_collection(name: str, db_path: str):
#     client = _get_client(db_path)
#     client.delete_collection(name)


# # =========================
# # Retrieval + RAG
# # =========================
# # def getOpenAI(ollama_openai_url: str) -> OpenAI:
# #     return OpenAI(base_url=ollama_openai_url.rstrip("/"), api_key="ollama")

# def getOpenAI(base_url: str) -> OpenAI:
#     """Создает OpenAI клиент с правильными настройками для liteLLM"""
#     from openai import OpenAI
#     import urllib3
#     import httpx
    
#     # Отключаем warnings
#     urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
#     # Подготавливаем URL
#     base_url = base_url.rstrip('/')
#     if not base_url.endswith('/v1'):
#         base_url = f"{base_url}/v1"
    
#     # Создаем кастомный HTTPX клиент с отключенной проверкой SSL
#     http_client = httpx.Client(
#         verify=False,  # Отключаем проверку SSL
#         timeout=60.0,  # Таймаут 60 секунд
#         headers={
#             "Authorization": f"Bearer {st.secrets.get('LITELLM_API_KEY', 'sk-fe1ZWrr7lPUN7tb8ZFlYEw')}",
#             "Content-Type": "application/json"
#         }
#     )
    
#     return OpenAI(
#         base_url=base_url,
#         api_key=st.secrets.get("LITELLM_API_KEY", "sk-fe1ZWrr7lPUN7tb8ZFlYEw"),
#         http_client=http_client
#     )

# def gen_query_variants(openai: OpenAI, model: str, q: str, n: int = 3) -> List[str]:
#     prompt = f"Дай {n} кратких переформулировок запроса; по одной на строку.\nЗапрос: {q}"
#     try:
#         r = openai.chat.completions.create(
#             model=model, temperature=0.3,
#             messages=[{"role": "user", "content": prompt}],
#         )
#         lines = [s.strip("- ").strip() for s in (r.choices[0].message.content or "").splitlines() if s.strip()]
#         return [q] + lines[:n]
#     except Exception:
#         return [q]


# def rrf_merge(rank_lists: List[List[Document]], K: int = 60) -> List[Document]:
#     scores: Dict[str, float] = {}
#     pick: Dict[str, Document] = {}

#     def key(d: Document) -> str:
#         md = tuple(sorted((d.metadata or {}).items()))
#         return f"{hash(d.page_content)}::{hash(md)}"

#     for rl in rank_lists:
#         for rank, d in enumerate(rl):
#             k = key(d)
#             scores[k] = scores.get(k, 0.0) + 1.0 / (K + rank + 1)
#             pick.setdefault(k, d)
#     return [pick[k] for k, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


# def _pid_from_meta(md: Dict) -> str:
#     return str(md.get("page_id") or "unknown")


# def _group_limit_per_page(docs: List[Document], per_page: int = 1) -> List[Document]:
#     by: Dict[str, List[Document]] = {}
#     for d in docs:
#         pid = _pid_from_meta(d.metadata or {})
#         by.setdefault(pid, []).append(d)
#     picked: List[Document] = []
#     for _, lst in by.items():
#         picked.extend(lst[:per_page])
#     return picked


# def _build_sources(docs: List[Document]) -> str:
#     lines = []
#     seen = set()
#     for d in docs:
#         md = d.metadata or {}
#         space = md.get("space_key")
#         pid = md.get("page_id")
#         url = md.get("url") or ""
#         if not (space and pid):
#             continue
#         head = md.get("Header 1") or md.get("Header 2") or ""
#         key = (space, pid, head)
#         if key in seen:
#             continue
#         seen.add(key)
#         line = f"- {space}:{pid} {head}".strip()
#         if url:
#             line += f" — {url}"
#         lines.append(line)
#     return "\n".join(lines)


# # def retrieve_docs(
# #     vectorstore: Chroma,
# #     query: str,
# #     use_multi_query: bool,
# #     openai: Optional[OpenAI],
# #     llm_model: Optional[str],
# #     k_single: int = 12,
# #     mq_variants: int = 3,
# #     k_per_variant: int = 6,
# #     total_top: int = 12,
# # ) -> List[Document]:
# #     if not use_multi_query or openai is None or not llm_model:
# #         retr = vectorstore.as_retriever(search_kwargs={"k": k_single, "filter": {"type": "original"}})
# #         return retr.invoke(query) or []

# #     variants = gen_query_variants(openai, llm_model, query, n=mq_variants)
# #     rank_lists: List[List[Document]] = []
# #     print("СПИСОК ВОПРОСОВ: \n",variants)
# #     for v in variants:
# #         retr = vectorstore.as_retriever(search_kwargs={"k": k_per_variant, "filter": {"type": "original"}})
# #         # print(f"{retr}\n\n")
# #         rank_lists.append(retr.invoke(v) or [])
# #         print(f"ВОПРОС: {v} \nОТВЕТ:{rank_lists}\n\n")
# #     merged = rrf_merge(rank_lists)
# #     print(f"\nОКОНЧАТЕЛЬНЫЙ ОТВЕТ: \n{merged}")
# #     return merged[:total_top]

# def retrieve_docs(
#     vectorstore: Chroma,
#     query: str,
#     use_multi_query: bool,
#     openai: Optional[OpenAI],
#     llm_model: Optional[str],
#     k_single: int = 12,
#     mq_variants: int = 3,
#     k_per_variant: int = 6,
#     total_top: int = 12,
#     content_types: Optional[List[str]] = None
# ) -> List[Document]:
#     """Улучшенный поиск с фильтрацией по типам контента"""
    
#     # Определяем тип запроса для оптимальной фильтрации
#     query_type = _classify_query_type(query)
    
#     # Настраиваем фильтры в зависимости от типа запроса - исправленный формат для ChromaDB
#     search_filters = {"type": {"$eq": "original"}}  # Исправлено: используем $eq
    
#     if content_types:
#         # ChromaDB ожидает формат: {"chunk_type": {"$in": ["text", "paragraph", "list"]}}
#         search_filters["chunk_type"] = {"$in": content_types}
#     elif query_type == "code":
#         search_filters["chunk_type"] = {"$eq": "code"}  # Исправлено
#     elif query_type == "fact":
#         # Для нескольких значений используем $in
#         search_filters["chunk_type"] = {"$in": ["text", "paragraph", "list"]}
#     # Для остальных типов не добавляем фильтр chunk_type
    
#     if not use_multi_query or openai is None or not llm_model:
#         retr = vectorstore.as_retriever(
#             search_kwargs={
#                 "k": k_single,
#                 "filter": search_filters
#             }
#         )
#         return retr.invoke(query) or []

#     variants = gen_query_variants(openai, llm_model, query, n=mq_variants)
#     rank_lists: List[List[Document]] = []
    
#     for v in variants:
#         try:
#             retr = vectorstore.as_retriever(
#                 search_kwargs={
#                     "k": k_per_variant,
#                     "filter": search_filters
#                 }
#             )
#             results = retr.invoke(v) or []
#             rank_lists.append(results)
#         except Exception as e:
#             print(f"Ошибка при поиске для варианта '{v}': {e}")
#             rank_lists.append([])
    
#     merged = rrf_merge(rank_lists)
    
#     # Переранжируем результаты с учетом релевантности
#     reranked = _rerank_results(merged, query, query_type)
    
#     return reranked[:total_top]



# # def _classify_query_type(query: str) -> str:
# #     """Классифицирует тип запроса"""
# #     query_lower = query.lower()
    
# #     if any(word in query_lower for word in ['код', 'пример', 'функция', 'метод', 'класс']):
# #         return "code"
# #     elif any(word in query_lower for word in ['таблица', 'данные', 'статистика']):
# #         return "table"
# #     elif any(word in query_lower for word in ['как', 'инструкция', 'шаги']):
# #         return "howto"
# #     elif any(word in query_lower for word in ['что', 'определение', 'понятие']):
# #         return "fact"
    
# #     return "general"
# def _classify_query_type(query: str) -> str:
#     """Классифицирует тип запроса"""
#     query_lower = query.lower()
    
#     if any(word in query_lower for word in ['код', 'пример', 'функция', 'метод', 'класс']):
#         return "code"
#     elif any(word in query_lower for word in ['таблица', 'данные', 'статистика']):
#         return "table"
#     elif any(word in query_lower for word in ['как', 'инструкция', 'шаги', 'сделать']):
#         return "howto"
#     elif any(word in query_lower for word in ['что', 'определение', 'понятие', 'такое']):
#         return "fact"
    
#     return "general"


# def _rerank_results(docs: List[Document], query: str, query_type: str) -> List[Document]:
#     """Переранжирование результатов на основе дополнительных критериев"""
#     scored_docs = []
    
#     for doc in docs:
#         score = 1.0
#         content = doc.page_content.lower()
#         metadata = doc.metadata or {}
#         chunk_type = metadata.get('chunk_type', '')
        
#         # Бонус за соответствие типа контента
#         if (query_type == "code" and chunk_type == "code") or \
#            (query_type == "table" and chunk_type == "table"):
#             score *= 1.5
        
#         # Бонус за заголовки секций
#         if metadata.get('section_level', 0) > 0:
#             score *= 1.2
        
#         # Штраф за слишком короткие/длинные чанки
#         token_count = metadata.get('token_count', 0)
#         if token_count < 50 or token_count > 1000:
#             score *= 0.8
        
#         scored_docs.append((doc, score))
    
#     # Сортируем по score
#     scored_docs.sort(key=lambda x: x[1], reverse=True)
#     return [doc for doc, score in scored_docs]


# def generate_answer_with_context(
#     embed_collection_name: str,
#     query: str,
#     model: str,
#     top_n: int,
#     db_path: str,
#     llm_base_url: str,
#     # ollama_openai_url: str,
#     # ollama_api_url: Optional[str] = None,
#     embedding_model: Optional[str] = None,
#     use_multi_query: bool = True,
#     mq_variants: int = 3,
#     k_per_variant: int = 6,
#     variant_offset: int = 0,
#     exclude_page_ids: Optional[List[str]] = None,
#     answers_per_variant: int = 3,
# ) -> str:
#     print(f"server={llm_base_url} \nmodel={model} \nquery={query} \nembed_collection_name={embed_collection_name}")
#     ollama_openai_url=llm_base_url
#     # ollama_api_url=llm_base_url
#     openai = getOpenAI(llm_base_url)
#     base_api = (ollama_openai_url.replace("/v1", "")).rstrip("/")
#     vectorstore = getVectorstore(embed_collection_name, db_path=db_path,
#                                  llm_base_url=base_api, embedding_model=embedding_model)
#     print(vectorstore)
#     cands = retrieve_docs(
#         vectorstore, query,
#         use_multi_query=use_multi_query,
#         openai=openai, llm_model=model,
#         k_single=max(top_n, 12),
#         mq_variants=mq_variants,
#         k_per_variant=k_per_variant,
#         total_top=max(top_n, 12),
#     )
#     if not cands:
#         return "Я не нашёл релевантный контекст по вашей коллекции."

#     cands = _group_limit_per_page(cands, per_page=1)

#     if exclude_page_ids:
#         excl = set(str(x) for x in exclude_page_ids)
#         cands = [d for d in cands if _pid_from_meta(d.metadata or {}) not in excl]

#     start = max(0, variant_offset * answers_per_variant)
#     selected = cands[start:start + answers_per_variant] or cands[:answers_per_variant]

#     context = "\n\n".join(d.page_content for d in selected)
#     sources_block = _build_sources(selected)

#     try:
#         resp = openai.chat.completions.create(
#             model=model,
#             messages=[
#                 {"role": "system",
#                  "content": ("Ты — эксперт по корпоративной базе знаний. "
#                              "Отвечай ТОЛЬКО по предоставленному контексту, не ищи ничего в интернете.")},
#                 {"role": "user",
#                  "content": f"Контекст:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."},
#             ],
#             temperature=0,
#         )
#         answer = resp.choices[0].message.content
#     except Exception as e:
#         return f"Не удалось сгенерировать ответ: {e}"

#     if sources_block:
#         answer = f"{answer}\n\n---\n**Источники:**\n{sources_block}"
#     return answer

# # def generate_answer_with_context(
# #     embed_collection_name: str,
# #     query: str,
# #     model: str,
# #     top_n: int,
# #     db_path: str,
# #     llm_base_url: str, 
# #     embedding_model: Optional[str] = None,
# #     use_multi_query: bool = True,
# #     mq_variants: int = 3,
# #     k_per_variant: int = 6,
# #     variant_offset: int = 0,
# #     exclude_page_ids: Optional[List[str]] = None,
# #     answers_per_variant: int = 3,
# # ) -> str:
# #     print(f"server={llm_base_url} \nmodel={model} \nquery={query} \nembed_collection_name={embed_collection_name}")
# #     # Используем llm_base_url вместо старых параметров
# #     openai = getOpenAI(llm_base_url)  # Передаем URL без /v1
    
# #     # Формируем base_api для векторного хранилища
# #     base_api = llm_base_url.rstrip('/')
# #     if base_api.endswith('/v1'):
# #         base_api = base_api[:-3]
    
# #     vectorstore = getVectorstore(
# #         embed_collection_name, 
# #         db_path=db_path,
# #         llm_base_url=base_api,  # Используем новый параметр
# #         embedding_model=embedding_model
# #     )

# #     cands = retrieve_docs(
# #         vectorstore, query,
# #         use_multi_query=use_multi_query,
# #         openai=openai, 
# #         llm_model=model,
# #         k_single=max(top_n, 12),
# #         mq_variants=mq_variants,
# #         k_per_variant=k_per_variant,
# #         total_top=max(top_n, 12),
# #     )
    
# #     if not cands:
# #         return "Я не нашёл релевантный контекст по вашей коллекции."

# #     cands = _group_limit_per_page(cands, per_page=1)

# #     if exclude_page_ids:
# #         excl = set(str(x) for x in exclude_page_ids)
# #         cands = [d for d in cands if _pid_from_meta(d.metadata or {}) not in excl]

# #     start = max(0, variant_offset * answers_per_variant)
# #     selected = cands[start:start + answers_per_variant] or cands[:answers_per_variant]

# #     context = "\n\n".join(d.page_content for d in selected)
# #     sources_block = _build_sources(selected)

# #     try:
# #         resp = openai.chat.completions.create(
# #             model=model,
# #             messages=[
# #                 {"role": "system",
# #                  "content": ("Ты — эксперт по корпоративной базе знаний. "
# #                              "Отвечай ТОЛЬКО по предоставленному контексту, не ищи ничего в интернете.")},
# #                 {"role": "user",
# #                  "content": f"Контекст:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."},
# #             ],
# #             temperature=0,
# #         )
# #         answer = resp.choices[0].message.content
# #     except Exception as e:
# #         return f"Не удалось сгенерировать ответ: {e}"

# #     if sources_block:
# #         answer = f"{answer}\n\n---\n**Источники:**\n{sources_block}"
# #     return answer


# # =========================
# # Confluence ingest
# # =========================
# def list_space_page_ids(
#     base_url: str,
#     token: str,
#     space_key: str,
#     verify_ssl: bool = False,
#     page_limit: int = 200,
#     timeout: int = 20,
# ) -> List[str]:
#     headers = {"Authorization": f"Bearer {token}"}
#     start = 0
#     ids: List[str] = []
#     while True:
#         params = {"spaceKey": space_key, "type": "page", "limit": page_limit, "start": start}
#         r = requests.get(f"{base_url.rstrip('/')}/rest/api/content",
#                          headers=headers, params=params, verify=verify_ssl, timeout=timeout)
#         r.raise_for_status()
#         data = r.json() or {}
#         results = data.get("results", []) or []
#         if not results:
#             break
#         ids.extend(str(it.get("id")) for it in results if it.get("id"))
#         if len(results) < page_limit:
#             break
#         start += len(results)
#     return ids


# def normalize_as_original(docs: List[Document], space_key: str, page_id: str, base_url: Optional[str] = None) -> List[Document]:
#     out: List[Document] = []
#     url = f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}" if base_url else None
#     for d in docs:
#         md = dict(getattr(d, "metadata", {}) or {})
#         md.setdefault("type", "original")
#         md["source"] = "confluence"
#         md["space_key"] = space_key
#         md["page_id"] = page_id
#         if url:
#             md.setdefault("url", url)
#         out.append(Document(page_content=d.page_content, metadata=md))
#     return out


# def make_chunk_ids(space_key: str, page_id: str, count: int, suffix: str = "") -> List[str]:
#     return [f"{space_key}-{page_id}-{i:03d}{suffix}" for i in range(count)]


# def append_documents(
#     collection_name: str,
#     docs: List[Document],
#     ids: List[str],
#     db_path: str,
#     ollama_api_url: str,
#     embedding_model: Optional[str] = None,
# ) -> Tuple[int, int]:
#     vs = getVectorstore(collection_name, db_path=db_path, ollama_api_url=ollama_api_url, embedding_model=embedding_model)
#     ok = 0
#     bad = 0
#     try:
#         vs.add_documents(docs, ids=ids)
#         ok += len(docs)
#     except Exception:
#         for d, _id in zip(docs, ids):
#             try:
#                 vs.add_documents([d], ids=[_id])
#                 ok += 1
#             except Exception:
#                 bad += 1
#     return ok, bad


# def ingest_space_incremental(
#     base_url: str,
#     token: str,
#     space_key: str,
#     collection_name: str,
#     db_path: str,
#     ollama_api_url: str,
#     summarize: bool = False,
#     max_pages: Optional[int] = None,
#     verify_ssl: bool = False,
#     embedding_model: Optional[str] = None,
# ) -> Tuple[int, int, int]:
#     from langchain_community.document_loaders import ConfluenceLoader
#     from langchain_community.document_loaders.confluence import ContentFormat

#     page_ids = list_space_page_ids(base_url, token, space_key, verify_ssl=verify_ssl)
#     if not page_ids:
#         return (0, 0, 0)

#     if max_pages is not None and max_pages > 0:
#         page_ids = page_ids[:max_pages]

#     processed_pages = 0
#     ok_docs = 0
#     bad_docs = 0

#     total_pages = len(page_ids)
#     pbar = st.progress(0.0, text=f"Готовлюсь… 0 / {total_pages}")

#     for idx, pid in enumerate(page_ids, start=1):
#         try:
#             loader = ConfluenceLoader(
#                 url=base_url,
#                 token=token,
#                 include_attachments=False,
#                 keep_markdown_format=True,
#                 content_format=ContentFormat.EXPORT_VIEW,
#                 page_ids=[pid],
#                 confluence_kwargs={"verify_ssl": verify_ssl},
#                 limit=1,
#             )
#             docs = loader.load()
#             if not docs:
#                 processed_pages += 1
#                 pbar.progress(idx / total_pages, text=f"Страница {idx}/{total_pages}: пусто")
#                 continue

#             chunks = split_into_chunks_semantic(
#                 docs, ollama_api_url=ollama_api_url, tokenizer=enc, max_tokens=500
#             )

#             prepared = normalize_as_original(chunks, space_key, pid, base_url=base_url)
#             ids = make_chunk_ids(space_key, pid, len(prepared))

#             _ok, _bad = append_documents(
#                 collection_name,
#                 prepared,
#                 ids,
#                 db_path=db_path,
#                 ollama_api_url=ollama_api_url,
#                 embedding_model=embedding_model,
#             )
#             ok_docs += _ok
#             bad_docs += _bad
#             processed_pages += 1

#             pbar.progress(
#                 idx / total_pages,
#                 text=f"Стр. {idx}/{total_pages} (pageId={pid}) — добавлено {_ok}, ошибок {_bad}",
#             )
#         except Exception as e:
#             processed_pages += 1
#             pbar.progress(
#                 idx / total_pages,
#                 text=f"Стр. {idx}/{total_pages} (pageId={pid}) — ошибка: {e}",
#             )

#     pbar.empty()
#     return processed_pages, ok_docs, bad_docs


# # def test_connection(base_url: str, api_key: str) -> bool:
# #     """Тестирует соединение с liteLLM"""
# #     try:
# #         session = requests.Session()
# #         session.verify = False
# #         session.headers.update({"Authorization": f"Bearer {api_key}"})
        
# #         # Проверяем доступность модели
# #         response = session.get(
# #             f"{base_url}/v1/models",
# #             timeout=10
# #         )
        
# #         if response.status_code == 200:
# #             models = response.json()
# #             st.write("Доступные модели:", [m["id"] for m in models.get("data", [])])
# #             return True
# #         else:
# #             st.error(f"Ошибка: статус {response.status_code}")
# #             return False
            
# #     except Exception as e:
# #         st.error(f"Ошибка подключения: {e}")
# #         return False

# # # Используйте перед загрузкой:
# # if test_connection(llm_base_url, api_key):
# #     store2Chroma(...)






# from langchain.embeddings.base import Embeddings
# import requests
# from typing import List, Optional
# import time


# import httpx
# from langchain.embeddings.base import Embeddings

# class LiteLLMEmbeddings(Embeddings):
#     """Кастомный класс эмбеддингов для liteLLM с httpx"""
    
#     def __init__(
#         self,
#         model: str,
#         base_url: str,
#         api_key: str = "sk-fe1ZWrr7lPUN7tb8ZFlYEw",
#         timeout: int = 60
#     ):
#         self.model = model
#         self.base_url = base_url.rstrip('/')
#         self.api_key = api_key
        
#         # Создаем HTTPX клиент
#         self.client = httpx.Client(
#             verify=False,  # Отключаем проверку SSL
#             timeout=timeout,
#             headers={
#                 "Authorization": f"Bearer {api_key}",
#                 "Content-Type": "application/json"
#             }
#         )
        
#         # Отключаем warnings
#         import urllib3
#         urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
#     def _embed(self, texts: List[str], prefix: str = "") -> List[List[float]]:
#         """Внутренний метод для получения эмбеддингов"""
#         # Добавляем префикс для E5 модели
#         if "e5" in self.model.lower():
#             texts = [f"{prefix}{t}" for t in texts]
        
#         response = self.client.post(
#             f"{self.base_url}/v1/embeddings",
#             json={
#                 "model": self.model,
#                 "input": texts
#             }
#         )
        
#         if response.status_code != 200:
#             raise Exception(f"Error from liteLLM: {response.status_code} - {response.text}")
        
#         data = response.json()
#         return [item["embedding"] for item in data["data"]]
    
#     def embed_documents(self, texts: List[str]) -> List[List[float]]:
#         """Эмбеддинги для документов"""
#         return self._embed(texts, prefix="passage: ")
    
#     def embed_query(self, text: str) -> List[float]:
#         """Эмбеддинги для запроса"""
#         result = self._embed([text], prefix="query: ")
#         return result[0]
    
#     def __del__(self):
#         """Закрываем клиент при удалении"""
#         if hasattr(self, 'client'):
#             self.client.close()

# # class LiteLLMEmbeddings(Embeddings):
# #     """Улучшенный класс эмбеддингов для liteLLM с поддержкой SSL"""
    
# #     def __init__(
# #         self,
# #         model: str,
# #         base_url: str,
# #         api_key: str = "sk-fe1ZWrr7lPUN7tb8ZFlYEw",
# #         max_retries: int = 3,
# #         timeout: int = 60
# #     ):
# #         self.model = model
# #         self.base_url = base_url.rstrip('/')
# #         self.api_key = api_key
# #         self.max_retries = max_retries
# #         self.timeout = timeout
        
# #         # Создаем сессию с отключенной проверкой SSL
# #         self.session = requests.Session()
# #         self.session.verify = False  # КРИТИЧЕСКИ ВАЖНО для вашего случая
# #         self.session.headers.update({
# #             "Authorization": f"Bearer {api_key}",
# #             "Content-Type": "application/json"
# #         })
        
# #         # Подавляем warnings о SSL
# #         import urllib3
# #         urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
# #     def _embed_with_retry(self, payload: dict) -> dict:
# #         """Отправка запроса с повторными попытками"""
# #         for attempt in range(self.max_retries):
# #             try:
# #                 response = self.session.post(
# #                     f"{self.base_url}/v1/embeddings",
# #                     json=payload,
# #                     timeout=self.timeout
# #                 )
                
# #                 if response.status_code == 200:
# #                     return response.json()
# #                 else:
# #                     print(f"Попытка {attempt + 1}: статус {response.status_code}")
                    
# #             except requests.exceptions.ConnectionError as e:
# #                 print(f"Попытка {attempt + 1}: ошибка соединения - {e}")
# #                 if attempt < self.max_retries - 1:
# #                     time.sleep(2 ** attempt)  # Экспоненциальная задержка
# #                 else:
# #                     raise
# #             except Exception as e:
# #                 print(f"Попытка {attempt + 1}: ошибка - {e}")
# #                 if attempt < self.max_retries - 1:
# #                     time.sleep(2 ** attempt)
# #                 else:
# #                     raise
        
# #         raise Exception("Все попытки запроса не удались")
    
# #     def embed_documents(self, texts: List[str]) -> List[List[float]]:
# #         """Эмбеддинги для документов"""
# #         # Обрабатываем батчами по 10 текстов
# #         all_embeddings = []
        
# #         for i in range(0, len(texts), 10):
# #             batch = texts[i:i+10]
# #             # Добавляем префикс для E5
# #             prefixed_batch = [f"passage: {t}" for t in batch]
            
# #             payload = {
# #                 "model": self.model,
# #                 "input": prefixed_batch
# #             }
            
# #             try:
# #                 result = self._embed_with_retry(payload)
# #                 embeddings = [item["embedding"] for item in result["data"]]
# #                 all_embeddings.extend(embeddings)
# #             except Exception as e:
# #                 print(f"Ошибка при обработке батча {i//10 + 1}: {e}")
# #                 # В случае ошибки возвращаем нулевые векторы для этого батча
# #                 dummy_embeddings = [[0.0] * 1024 for _ in batch]  # Размерность для E5
# #                 all_embeddings.extend(dummy_embeddings)
        
# #         return all_embeddings
    
# #     def embed_query(self, text: str) -> List[float]:
# #         """Эмбеддинги для запроса"""
# #         payload = {
# #             "model": self.model,
# #             "input": [f"query: {text}"]
# #         }
        
# #         try:
# #             result = self._embed_with_retry(payload)
# #             return result["data"][0]["embedding"]
# #         except Exception as e:
# #             print(f"Ошибка при эмбеддинге запроса: {e}")
# #             return [0.0] * 1024  # Возвращаем нулевой вектор в случае ошибки






# # Замените импорты
# from langchain_openai import OpenAIEmbeddings  # вместо OllamaEmbeddings
# # Удалите: from langchain_community.embeddings import OllamaEmbeddings

# # И замените класс E5OllamaEmbeddings на:
# class E5OllamaEmbeddings(OpenAIEmbeddings):
#     """Адаптер для liteLLM с префиксами E5"""
    
#     def __init__(
#         self,
#         model: str = "jeffh/intfloat-multilingual-e5-large-instruct:f32",
#         base_url: Optional[str] = None,
#         api_key: str = "sk-fe1ZWrr7lPUN7tb8ZFlYEw",  # Ваш API ключ
#         **kwargs
#     ):
#         super().__init__(
#             model=model,
#             openai_api_base=base_url.rstrip('/') + '/v1',  # Важно: добавляем /v1
#             openai_api_key=api_key,
#             **kwargs
#         )
    
#     def embed_query(self, text: str):
#         return super().embed_query(f"query: {text}")
    
#     def embed_documents(self, texts: List[str]):
#         return super().embed_documents([f"passage: {t}" for t in texts])