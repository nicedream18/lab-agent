import hashlib
import logging
import re
import threading
import time
from typing import cast

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Embeddable, EmbeddingFunction

from app.agent.observability import span
from app.config import BASE_DIR, settings
from app.services import inference_service

logger = logging.getLogger(__name__)

KB_DIR = BASE_DIR / "data" / "kb"
CHROMA_DIR = BASE_DIR / "data" / "chroma"

# 离线服务器可以指向预先上传的 SentenceTransformer 模型目录。
EMBEDDING_MODEL = settings.LAB_EMBEDDING_MODEL
COLLECTION_NAME = "lab_kb"

# ---- 切片参数 ----
# 单个 chunk 正文的目标长度（字符数）。中文在 BERT 分词器里大致 1 字 ≈ 1 token，
# 而 bge-small-zh-v1.5 的硬上限就是 512（config.json 的 max_position_embeddings
# 与 sentence_bert_config.json 的 max_seq_length 都是 512）。
# 300 字留了约 40% 余量，再叠上标题路径前缀（≤ 60 字）也不会被静默截断。
# ⚠️ sentence-transformers 超长时是**静默截断**（base/modules/transformer.py 里
# `"text": {"padding": True, "truncation": "longest_first"}`），不报错、不告警。
CHUNK_SIZE = 300
# 二次切割时的重叠字数：避免「一句话正好跨在两段之间」导致两边都检索不到。
CHUNK_OVERLAP = 50
# 切片算法版本号，会掺进源文件指纹（见 _file_hash）。
# ⚠️ 改了 _chunk_markdown / _split_long 的切分规则（包括 CHUNK_SIZE）就必须 +1，
# 否则源文件内容没变时会被判定为「无变化」，新规则永远不会落到已有的向量上。
CHUNKER_VERSION = 1

# ---- 检索参数 ----
# 向量召回候选数；启用 Reranker 后对这些候选批量打分，再选 TOP_K_FINAL。
TOP_K_RECALL = 8
# 最终交给模型的 chunk 数。原来是 2，但一条答案可能跨 3 个切片
# （比如「管制化学品管理」和它前后文分属不同切片）。
TOP_K_FINAL = 3

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 本知识库的导出残留：整行的 `------` 分隔线。留着会污染 embedding，直接丢。
_RULE_RE = re.compile(r"^-{3,}$")
# 「第一章 总则」这类章标题。用途见 _chunk_markdown()：
# 这份文件里并不是所有「第X条」都标成了 ### 级（第二章/第四章/第五章的条
# 与它们的章同是 ##），靠层级推路径会把章标题弄丢，所以要单独认一下。
_CHAPTER_RE = re.compile(r"^第[〇零一二三四五六七八九十百千万]+章")

_collection = None
# get_collection() 是“先判断后构造”，后台预热线程和第一个真实请求可能同时进来。
# 没有这把锁，两边都会各自加载一份模型（白占几百 MB 内存），所以必须加。
_lock = threading.Lock()


def get_embedding_fn():
    """Lazy shared embedding model with bounded cross-request micro-batching.

    Preserve the existing model, CPU default, normalization and Chroma function
    identity. Changing batch size does not require re-embedding stored documents.
    """
    return inference_service.get_embedding_fn(EMBEDDING_MODEL)


def _file_hash(text: str) -> str:
    """源文件指纹 = 切片算法版本 + 文件内容。

    用它判断「这个 md 要不要重灌」，比 mtime 可靠：git checkout、编辑器另存、
    格式化工具都会改 mtime 但内容可能没变，而内容没变时不该重新向量化。

    掺进 CHUNKER_VERSION 是为了堵另一个漏洞：只指纹文件内容的话，
    「改了切片规则」也算「无变化」，新规则永远落不到已有向量上。
    """
    payload = f"{CHUNKER_VERSION}\0{text}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _hard_split(text: str, size: int) -> list[str]:
    """先把段落按行（列表项/表格行）贪心打包；单行本身超长才按字符硬切。"""
    out: list[str] = []
    cur = ""
    for line in text.splitlines() or [text]:
        if len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(line[i : i + size] for i in range(0, len(line), size))
            continue
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def _split_long(body: str, size: int = CHUNK_SIZE) -> list[str]:
    """把超长正文切成不超过 size 的若干段。

    切点优先级：空行（段落）→ 行（列表项）→ 字符硬切。
    从第二段起会带上上一段末尾 CHUNK_OVERLAP 个字符作为重叠，
    这样「一句话正好跨在两段之间」时，两段各自都还能检索到半句。
    """
    if len(body) <= size:
        return [body]

    units: list[str] = []
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        units.extend([para] if len(para) <= size else _hard_split(para, size))

    parts: list[str] = []
    cur = ""
    for unit in units:
        if cur and len(cur) + 2 + len(unit) > size:
            parts.append(cur)
            tail = cur[-CHUNK_OVERLAP:].lstrip() if CHUNK_OVERLAP else ""
            cur = f"{tail}\n{unit}" if tail else unit
        else:
            cur = f"{cur}\n\n{unit}" if cur else unit
    if cur:
        parts.append(cur)
    return parts


def _chunk_markdown(text: str) -> list[tuple[str, str]]:
    """把一篇 markdown 按标题层级切成 [(标题路径, 正文), ...]。

    - `#`~`######` 都是切点；遇到**同级或更高级**标题就把上一段封口，
      所以「### （一）安全检查制度」不会把「### （二）安全教育培训」吃进来
    - 整行的 `----` 分隔线丢掉（导出残留，会污染 embedding）
    - 单节仍然超过 CHUNK_SIZE 时，交给 _split_long() 继续拆
    - 返回的标题路径会由 _sync_index() 拼进切片正文（不只是放进 metadata），
      这是召回质量的关键：孤立的一段「1. 双人验收；2. 双人保管」单独去做
      embedding 时几乎语义不明，带上前缀「第七章 实验室危险化学品安全管理 >
      管制化学品管理」就完全不一样了
    - ⚠️ 源文件里第二章/第四章/第五章的「第X条」是 `##` 级，与它们的「第X章」
      同级，纯靠层级推路径会把这些条从章里「脱出去」（变成孤零零的
      「第五条 校级安全责任体系」）。所以额外记住最近一个章标题，
      路径里没有它就补上。
    """
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    out: list[tuple[str, str]] = []
    chapter = ""

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        path = " > ".join(title for _, title in stack)
        if chapter and path != chapter and not path.startswith(f"{chapter} > "):
            path = f"{chapter} > {path}" if path else chapter
        out.extend((path, part) for part in _split_long(body))

    for line in text.splitlines():
        stripped = line.strip()
        if _RULE_RE.match(stripped):
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            # 先封口再调整标题栈：此刻栈顶正是这段正文所属的路径
            flush()
            title = heading.group(2).strip()
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            if _CHAPTER_RE.match(title):
                chapter = title
            continue
        buf.append(line)
    flush()
    return out


def _sync_index(col: Collection) -> None:
    """把 data/kb/*.md 与向量库对齐（增量同步）。

    改前的写法是 `if col.count() == 0:` —— 只在空库时灌一次。后果是
    「往 kb/ 里丢新文件、或改了已有文件」都不会生效，必须手工删掉
    data/chroma/ 才会重新读盘（而删目录只能靠人记得）。

    这里改成按内容指纹做差异同步：
    - 源文件消失           → 删掉它的全部切片
    - 源文件新增 / 指纹变了 → 先按 source 删旧切片，再灌新的
    - 指纹一致             → 跳过（不重复向量化，省掉每次启动十几秒）

    ⚠️ metadata 里必须存 file_hash：旧数据没有这个字段，取到的是空串，
      与真实指纹不等 ⇒ 被当作「变了」重灌一次。这正是我们想要的迁移行为。
    """
    on_disk: dict[str, tuple[str, str]] = {}
    for path in sorted(KB_DIR.glob("*.md")):
        # macOS 拷贝时可能生成 AppleDouble 元数据文件（._*.md），不是知识文档。
        if path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            on_disk[path.name] = (_file_hash(text), text)

    grouped: dict[str, list[dict]] = {}
    for meta in col.get(include=["metadatas"]).get("metadatas") or []:
        source = str((meta or {}).get("source") or "")
        if source:
            grouped.setdefault(source, []).append(meta)
    indexed: dict[str, str] = {}
    for source, metas in grouped.items():
        hashes = {str(meta.get("file_hash") or "") for meta in metas}
        expected = metas[0].get("chunk_count")
        complete = expected is None or (
            isinstance(expected, int) and expected == len(metas)
            and all(meta.get("chunk_count") == expected for meta in metas)
            and {meta.get("chunk") for meta in metas} == set(range(expected))
        )
        # New batched writes record the expected count. A partial write must be
        # retried next startup even when its first chunks have the new file hash.
        indexed[source] = next(iter(hashes)) if len(hashes) == 1 and complete else ""

    removed = sorted(set(indexed) - set(on_disk))
    for source in removed:
        col.delete(where={"source": {"$eq": source}})
        logger.info("知识库移除已删除的文件：%s", source)

    updated = 0
    for source, (digest, text) in on_disk.items():
        if indexed.get(source) == digest:
            continue
        if source in indexed:
            col.delete(where={"source": {"$eq": source}})
        ids: list[str] = []
        docs: list[str] = []
        metas: list[dict[str, str | int]] = []
        for i, (section, body) in enumerate(_chunk_markdown(text)):
            prefix = f"【{source} > {section}】" if section else f"【{source}】"
            ids.append(f"{source}#{i:03d}")
            docs.append(f"{prefix}\n{body}")
            metas.append(
                {
                    "source": source,
                    "section": section,
                    "chunk": i,
                    "file_hash": digest,
                }
            )
        if not ids:
            logger.warning("知识库文件没切出任何内容，已跳过：%s", source)
            continue
        for meta in metas:
            meta["chunk_count"] = len(ids)
        for start in range(0, len(ids), settings.KB_INDEX_BATCH_SIZE):
            end = start + settings.KB_INDEX_BATCH_SIZE
            col.add(ids=ids[start:end], documents=docs[start:end],
                    metadatas=metas[start:end])  # pyright: ignore[reportArgumentType]
        updated += 1
        logger.info("知识库入库 %s：%d 个切片", source, len(ids))

    if updated or removed:
        logger.info(
            "知识库同步完成：%d 个文件更新、%d 个文件移除，共 %d 个切片",
            updated,
            len(removed),
            col.count(),
        )
    else:
        logger.info("知识库无变化，共 %d 个切片", col.count())


def _build_collection() -> Collection:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    # chroma 把这个形参声明成 EmbeddingFunction[Embeddable]（Embeddable = 文本 ∪ 图片），
    # 而 get_embedding_fn() 返回的 SentenceTransformerEmbeddingFunction 是
    # EmbeddingFunction[Documents]（只处理文本）。Protocol 的泛型参数出现在 __call__
    # 的入参位置上，属于不变型参数，所以 EmbeddingFunction[Documents] 并不是
    # EmbeddingFunction[Embeddable] 的子类型。我们只灌纯文本文档，这里如实断言，
    # 运行时行为与改前完全一致。
    embedding_fn = cast(EmbeddingFunction[Embeddable], get_embedding_fn())
    # 度量显式钉死成 cosine。hnsw 的 space 默认值是 "l2"，一旦被默认值悄悄决定，
    # search() 里 1/(1+dist) 那个「0~1」的假设就完全不成立了（L2 距离无上界），
    # 阈值 0.5 也会退化成一句废话。写在这里是为了让「用哪种距离」是代码说了算，
    # 而不是 chromadb 的默认值说了算。
    # ⚠️ get_or_create_collection 在集合已存在时会忽略这里的 configuration
    # （源码 docstring："If the collection already exists, the schema, configuration,
    # and metadata arguments will be ignored"），所以旧集合改不动，只能删掉
    # data/chroma/ 让它按新度量重建。
    col = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        configuration={"hnsw": {"space": "cosine"}},
    )
    # 索引同步失败不该让整个应用起不来：这个方法既被 warmup() 的后台线程调用，
    # 也在第一个用户提问的同步路径上。如实记日志，本次使用可用的切片。
    try:
        _sync_index(col)
    except Exception:
        logger.exception("知识库索引同步失败，本次使用可用切片，下次初始化重试")
    return col


def get_collection() -> Collection:
    """向量库的初始化（全局只做一次）"""
    global _collection
    if _collection is not None:
        return _collection
    with _lock:
        # 双重检查：可能在这一步之前，后台预热线程已经把它建好了
        if _collection is None:
            _collection = _build_collection()
        return _collection


def close() -> None:
    """Drop collection handles before shutting down their shared model workers."""
    global _collection
    with _lock:
        _collection = None
        inference_service.close()


def warmup() -> None:
    """在后台线程里预热向量库，由 main.py 的 lifespan 调用。

    干两件事：
    1. get_collection()：加载模型（约 10s）+ 打开/建集合；
    2. 做一次真实检索：把查询向量化、索引载入和启用时的批量精排一并跑热。
       只做第 1 步的话，第一个用户提问仍然要额外等一次 encode()。

    预热失败绝不能拖垮整个应用：这里只记日志，
    等第一次真实提问时 get_collection() 会再试一次。
    """
    started = time.perf_counter()
    try:
        col = get_collection()
        if col.count() > 0:
            search("实验室安全管理制度")
        logger.info("向量库预热完成，耗时 %.1fs", time.perf_counter() - started)
    except Exception:
        logger.exception("向量库预热失败，将在首次使用时重试")


def search_many(queries: list[str]) -> list[str]:
    with span("kb.search", run_type="retriever", inputs={"queries": queries}) as run:
        result = _search_many(queries)
        if run is not None:
            run.end(outputs={"contexts": result})
        return result


def _search_many(queries: list[str]) -> list[str]:
    """Batch recall and reranking, preserving query order and empty positions."""
    if not queries:
        return []
    if any(not isinstance(query, str) for query in queries):
        raise TypeError("queries must be strings")
    output = [""] * len(queries)
    active = [(index, query) for index, query in enumerate(queries) if query.strip()]
    if not active:
        return output
    col = get_collection()
    total = col.count()
    if total == 0:
        return output
    for start in range(0, len(active), settings.KB_EMBEDDING_BATCH_SIZE):
        batch = active[start:start + settings.KB_EMBEDDING_BATCH_SIZE]
        with span("kb.recall", run_type="retriever",
                  metadata={"query_count": len(batch), "top_k": min(TOP_K_RECALL, total)}):
            res = col.query(query_texts=[query for _, query in batch],
                            n_results=min(TOP_K_RECALL, total))
        groups = res.get("documents")
        if groups is None or len(groups) != len(batch):
            raise ValueError("retrieval result count does not match query batch")
        docs = [[doc for doc in group if isinstance(doc, str) and doc.strip()]
                for group in groups]
        if settings.KB_RERANKER_ENABLED:
            pairs = [(query, doc) for (_, query), candidates in zip(batch, docs)
                     for doc in candidates]
            try:
                scores = inference_service.rerank(pairs)
            except Exception as exc:
                # Retrieval remains usable if the optional model is unavailable
                # or its bounded queue is full. Never mix scores between queries.
                logger.warning("知识库精排不可用，本次保留向量召回顺序 (%s)",
                               type(exc).__name__)
            else:
                offset = 0
                for index, candidates in enumerate(docs):
                    size = len(candidates)
                    ranked = sorted(zip(candidates, scores[offset:offset + size]),
                                    key=lambda item: item[1], reverse=True)
                    docs[index] = [doc for doc, _ in ranked]
                    offset += size
        for (index, _), candidates in zip(batch, docs):
            output[index] = "\n\n".join(candidates[:TOP_K_FINAL])
    return output


def search(query: str) -> str:
    """Single-query compatibility entry; concurrent calls share inference batches."""
    return search_many([query])[0]
