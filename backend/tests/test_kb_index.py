"""向量库的增量同步（_sync_index）与检索（search）。

这两块以前完全没有测试，而它们是 RAG 的两处「静默失效」高发区：

- 同步改写前是 `if col.count() == 0:` —— 往 kb/ 里丢新文件不生效，
  改已有文件也不生效，必须人工删 data/chroma/。改成按指纹差异同步后，
  「新增 / 内容变了 / 文件被删 / 什么都没变」四条路径都必须各有用例。
- 检索的 TOP_K 与空库提前返回也是边界，空库时不能抛异常。

用假集合（FakeCollection）替掉 chromadb，所以这里不加载模型、不落盘、秒级完成。
真模型那段（get_embedding_fn / _build_collection）故意不测 —— 它是 IO + 十秒级
权重加载，属于冒烟范畴，不属于单测。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.offline_env import configure_offline

configure_offline()

from app.services import kb_service


class FakeCollection:
    """只实现 _sync_index / search 用到的那几个方法。"""

    def __init__(self, docs=None, metas=None):
        self.docs: dict[str, str] = dict(docs or {})
        self.metas: dict[str, dict] = dict(metas or {})
        self.add_calls: list[list[str]] = []
        self.delete_calls: list[dict] = []
        self.query_n_results: int | None = None

    # ---- _sync_index 用到的三个方法 ----
    def get(self, include=None):
        return {"metadatas": [dict(meta) for meta in self.metas.values()]}

    def delete(self, where):
        self.delete_calls.append(where)
        source = where["source"]["$eq"]
        for key in [
            k for k, meta in self.metas.items() if meta.get("source") == source
        ]:
            self.docs.pop(key, None)
            self.metas.pop(key, None)

    def add(self, ids, documents, metadatas):
        self.add_calls.append(list(ids))
        for key, doc, meta in zip(ids, documents, metadatas):
            self.docs[key] = doc
            self.metas[key] = dict(meta)

    # ---- search 用到的方法 ----
    def count(self):
        return len(self.docs)

    def query(self, query_texts, n_results):
        self.query_n_results = n_results
        keys = list(self.docs)[:n_results]
        return {
            "documents": [[self.docs[key] for key in keys]],
            "distances": [[0.1 * (i + 1) for i in range(len(keys))]],
        }

    def seed(self, count, prefix="seed"):
        for i in range(count):
            key = f"{prefix}#{i:03d}"
            self.docs[key] = f"【{prefix}】\n第 {i} 条"
            self.metas[key] = {"source": "seed.md", "section": "", "chunk": i}


class SyncIndexCases(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.kb_dir = Path(tmp.name)
        patcher = patch.object(kb_service, "KB_DIR", self.kb_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.col = FakeCollection()

    def write(self, name, text):
        path = self.kb_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_new_file_is_ingested(self):
        text = "# 第一章 总则\n第一条 内容\n"
        self.write("a.md", text)
        kb_service._sync_index(self.col)
        self.assertEqual(self.col.count(), 1)
        self.assertEqual(self.col.add_calls, [["a.md#000"]])
        self.assertEqual(
            self.col.metas["a.md#000"]["file_hash"],
            kb_service._file_hash(text.strip()),
        )

    def test_unchanged_file_is_skipped(self):
        """指纹一致时必须跳过 —— 否则每次启动都要重新向量化十几秒。"""
        self.write("a.md", "# 章\n内容\n")
        kb_service._sync_index(self.col)
        self.assertEqual(len(self.col.add_calls), 1)
        with self.assertLogs("app.services.kb_service", level="INFO") as captured:
            kb_service._sync_index(self.col)
        self.assertEqual(len(self.col.add_calls), 1, "内容没变却又灌了一次")
        self.assertEqual(self.col.count(), 1)
        self.assertTrue(
            any("知识库无变化" in line for line in captured.output), captured.output
        )

    def test_changed_content_replaces_old_chunks(self):
        self.write("a.md", "# 第一章\n只有一节\n")
        kb_service._sync_index(self.col)
        self.assertEqual(self.col.count(), 1)

        new_text = "# 第一章\n第一节\n# 第二章\n第二节\n"
        self.write("a.md", new_text)
        kb_service._sync_index(self.col)
        self.assertEqual(self.col.count(), 2)
        # 旧指纹的切片必须清干净，不能新旧混在一起
        self.assertEqual(
            {meta["file_hash"] for meta in self.col.metas.values()},
            {kb_service._file_hash(new_text.strip())},
        )
        self.assertTrue(all(key.startswith("a.md#") for key in self.col.docs))

    def test_deleted_file_is_removed(self):
        self.write("a.md", "# 甲\n内容甲\n")
        keep = self.write("b.md", "# 乙\n内容乙\n")
        kb_service._sync_index(self.col)
        self.assertEqual(self.col.count(), 2)

        keep.unlink()
        with self.assertLogs("app.services.kb_service", level="INFO") as captured:
            kb_service._sync_index(self.col)
        self.assertEqual(self.col.count(), 1)
        self.assertTrue(all(not key.startswith("b.md") for key in self.col.docs))
        self.assertTrue(any("移除已删除的文件" in line for line in captured.output))

    def test_chunker_version_bump_forces_reindex(self):
        """改了切片规则（版本号 +1）时，内容没变也必须重灌。"""
        self.write("a.md", "# 章\n内容\n")
        kb_service._sync_index(self.col)
        self.assertEqual(len(self.col.add_calls), 1)

        with patch.object(
            kb_service, "CHUNKER_VERSION", kb_service.CHUNKER_VERSION + 1
        ):
            kb_service._sync_index(self.col)
        self.assertEqual(len(self.col.add_calls), 2, "换了切片算法却没重灌")

    def test_dotfiles_and_blank_files_are_ignored(self):
        """macOS 的 AppleDouble 元数据文件、以及空白 md，都不是知识文档。"""
        self.write("._a.md", "# 不是文档\n内容\n")
        self.write("空白.md", "   \n\n")
        kb_service._sync_index(self.col)
        self.assertEqual(self.col.count(), 0)
        self.assertEqual(self.col.add_calls, [])

    def test_document_carries_source_and_section_prefix(self):
        """章节路径要拼进正文（不只是 metadata），这是召回质量的关键。"""
        self.write("a.md", "# 第一章 总则\n第一条 内容\n")
        kb_service._sync_index(self.col)
        document = self.col.docs["a.md#000"]
        self.assertTrue(document.startswith("【a.md > 第一章 总则】\n"), repr(document))

    def test_metadata_records_source_section_and_chunk(self):
        self.write("a.md", "# 甲\n甲内容\n# 乙\n乙内容\n")
        kb_service._sync_index(self.col)
        self.assertEqual(
            [self.col.metas[f"a.md#{i:03d}"]["section"] for i in (0, 1)],
            ["甲", "乙"],
        )
        self.assertEqual(
            [self.col.metas[f"a.md#{i:03d}"]["chunk"] for i in (0, 1)], [0, 1]
        )
        self.assertTrue(
            all(self.col.metas[key]["source"] == "a.md" for key in self.col.metas)
        )


class SearchCases(unittest.TestCase):
    def setUp(self):
        self.col = FakeCollection()

    def search(self, query):
        with patch.object(kb_service, "get_collection", return_value=self.col):
            return kb_service.search(query)

    def test_empty_index_returns_empty_string(self):
        self.assertEqual(self.search("任何问题"), "")
        self.assertIsNone(self.col.query_n_results, "空库不该去查向量库")

    def test_result_is_limited_to_top_k_final(self):
        self.col.seed(5)
        result = self.search("问题")
        parts = result.split("\n\n")
        self.assertEqual(len(parts), kb_service.TOP_K_FINAL)

    def test_recall_is_capped_by_index_size(self):
        """集合里只有 2 条时，n_results 必须是 2 —— 传 8 会被 chroma 拒绝。"""
        self.col.seed(2)
        self.search("问题")
        self.assertEqual(self.col.query_n_results, 2)

        self.col.seed(kb_service.TOP_K_RECALL + 5)
        self.search("问题")
        self.assertEqual(self.col.query_n_results, kb_service.TOP_K_RECALL)


if __name__ == "__main__":
    unittest.main()
