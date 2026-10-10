import logging
import threading
import time
from typing import cast

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Embeddable, EmbeddingFunction
from chromadb.utils import embedding_functions

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

KB_DIR = BASE_DIR / "data" / "kb"
CHROMA_DIR = BASE_DIR / "data" / "chroma"

EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
COLLECTION_NAME = "lab_kb"

_collection = None
# get_collection() 是“先判断后构造”，后台预热线程和第一个真实请求可能同时进来。
# 没有这把锁，两边都会各自加载一份模型（白占几百 MB 内存），所以必须加。
_lock = threading.Lock()


def get_embedding_fn():
    """惰性创建嵌入模型：真正用到向量库时才导入 torch 并加载权重（约 10s）。

    千万不要写成模块级语句。那会让 import app.main 也花掉这 10s：
    每次 uvicorn --reload 都要重来一遍；更糟的是一旦 HuggingFace 连不上，
    整个应用（连登录接口）都起不来。

    这里不需要自己加缓存：chromadb 的 SentenceTransformerEmbeddingFunction
    内部有一个「类级」字典 models（源码第 9 行 models: Dict[str, Any] = {}），
    构造时先查 `if model_name not in self.models`（第 43 行）再真正加载。
    换句话说，同一个进程里第二次构造是白送的——实测第一次 9.71s、第二次 0.00s，
    而且拿到的是同一个 SentenceTransformer 对象。
    唯一的调用点在 _build_collection() 里，而它被 _lock 保护，只跑一次，
    所以这里直接构造即可。
    """
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )


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
    col = client.get_or_create_collection(
        name=COLLECTION_NAME, embedding_function=embedding_fn
    )
    if col.count() == 0:
        ids = []
        docs = []
        metas = []
        for path in sorted(KB_DIR.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            docs.append(text)
            ids.append(path.stem)
            metas.append({"source": path.name})
        if docs:
            col.add(ids=ids, documents=docs, metadatas=metas)
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


def warmup() -> None:
    """在后台线程里预热向量库，由 main.py 的 lifespan 调用。

    干两件事：
    1. get_collection()：加载模型（约 10s）+ 打开/建集合；
    2. 拿一句废话做一次真实检索：把 query 那条路径（文本向量化、
       索引数据从磁盘载入、距离计算）也一并跑热。
       只做第 1 步的话，第一个用户提问仍然要额外等一次 encode()。

    预热失败绝不能拖垮整个应用：这里只记日志，
    等第一次真实提问时 get_collection() 会再试一次。
    """
    started = time.perf_counter()
    try:
        col = get_collection()
        if col.count() > 0:
            col.query(query_texts=["预热"], n_results=1)
        logger.info("向量库预热完成，耗时 %.1fs", time.perf_counter() - started)
    except Exception:
        logger.exception("向量库预热失败，将在首次使用时重试")


def search(query: str):
    """根据关键字去检索向量库"""
    col = get_collection()
    if col.count() == 0:
        return ""
    limit = min(5, col.count())
    res = col.query(query_texts=[query], n_results=limit)
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    distances = (res.get("distances") or [[]])[0]

    """
    以下是检索出来的资料：
    [预约规则.md]

    # 实验室预约规则...

    
    [安全规范.md]

    安全规范....
    """
    score_parts = []
    for doc, metas, dist in zip(docs, metas, distances):
        # 0-1 越接近1表示越相关
        score = 1 / (1 + dist)
        if score < 0.5:
            continue
        name = metas.get("source") or ""
        score_parts.append({"score": score, "content": f"[{name}]\n{doc}"})
    print(f"检索出来的 score_parts：{score_parts}")
    score_parts.sort(key=lambda x: x["score"], reverse=True)
    final_parts = [item["content"] for item in score_parts[:2]]
    return "\n\n".join(final_parts)
