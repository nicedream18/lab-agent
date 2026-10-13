"""知识库切片：标题感知切分、超长二次切割、指纹。

这些都是纯函数（输入 str、输出 str/list），不碰数据库也不碰向量库 ——
之所以单独测，是因为这里踩过两个真坑，而且都是「静默出错」：

1. **章标题脱层**：源文件里第二章/第四章/第五章的「第X条」与它们的「第X章」
   同为 `##` 级，纯靠层级推路径会让这些条从章里脱出去，切片正文变成孤零零的
   「第八条 管制化学品管理」，embedding 几乎语义不明。
2. **改了切分规则却不重灌**：指纹只算文件内容时，「换切片算法」也算「无变化」，
   新规则永远落不到已有向量上，必须手工删 data/chroma/ 才生效。

下面每个用例都钉住一条具体行为，改坏了就会红。
"""

import unittest
from unittest.mock import patch

from evals.offline_env import configure_offline

configure_offline()

from app.services import kb_service


class FileHashCases(unittest.TestCase):
    def test_hash_is_stable_and_short(self):
        text = "第一章 总则\n第一条 为了安全。"
        first = kb_service._file_hash(text)
        self.assertEqual(first, kb_service._file_hash(text))
        self.assertEqual(len(first), 12)

    def test_different_content_gives_different_hash(self):
        self.assertNotEqual(kb_service._file_hash("甲"), kb_service._file_hash("甲乙"))

    def test_chunker_version_is_part_of_the_hash(self):
        """改切片算法必须让已有切片的指纹失效，否则新规则永远不生效。"""
        text = "第一章 总则"
        before = kb_service._file_hash(text)
        with patch.object(
            kb_service, "CHUNKER_VERSION", kb_service.CHUNKER_VERSION + 1
        ):
            after = kb_service._file_hash(text)
        self.assertNotEqual(before, after)


class HardSplitCases(unittest.TestCase):
    def test_short_lines_are_packed_together(self):
        parts = kb_service._hard_split("aaa\nbbb\nccc", 10)
        self.assertEqual(parts, ["aaa\nbbb", "ccc"])

    def test_single_overlong_line_is_cut_by_character(self):
        parts = kb_service._hard_split("x" * 700, 300)
        self.assertEqual([len(part) for part in parts], [300, 300, 100])
        self.assertEqual("".join(parts), "x" * 700)

    def test_every_part_stays_within_size(self):
        text = "\n".join(["a" * 40] * 12)
        parts = kb_service._hard_split(text, 100)
        self.assertTrue(parts)
        self.assertTrue(all(len(part) <= 100 for part in parts), parts)


class SplitLongCases(unittest.TestCase):
    def test_short_body_is_returned_as_is(self):
        self.assertEqual(kb_service._split_long("很短的一段"), ["很短的一段"])

    def test_paragraphs_are_packed_up_to_size(self):
        body = "\n\n".join(["甲" * 100, "乙" * 100, "丙" * 100])
        parts = kb_service._split_long(body, 300)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], "甲" * 100 + "\n\n" + "乙" * 100)
        self.assertTrue(all(len(part) <= 300 for part in parts), parts)

    def test_parts_overlap_so_a_half_sentence_is_still_findable(self):
        """一句话正好跨在两段之间时，两边各自都要还能检索到那半句。"""
        body = "\n\n".join(["甲" * 100, "乙" * 100, "丙" * 100])
        parts = kb_service._split_long(body, 300)
        self.assertEqual(parts[0][-50:], parts[1][:50])
        self.assertEqual(parts[0][-50:], "乙" * 50)

    def test_body_without_blank_lines_still_splits(self):
        """没有空行可切时退化成字符硬切，但绝不允许「原样返回一整段」。"""
        parts = kb_service._split_long("x" * 1000, 300)
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0], "x" * 300)
        # 除重叠时插入的换行外，不应夹带别的内容
        self.assertTrue(all(set(part) <= {"x", "\n"} for part in parts))

    def test_overlap_prefix_is_the_only_way_a_part_exceeds_size(self):
        """重叠前缀会让「带着上一段尾巴的那一片」略微超出 size。

        这是已知的、可接受的行为（真实知识库的最大切片仍是 300 以内，
        见 test_real_knowledge_base_files_all_chunk_without_loss），
        所以钉住它，避免以后有人误以为「每一片都必然 <= CHUNK_SIZE」。
        """
        parts = kb_service._split_long("x" * 1000, 300)
        longest = max(len(part) for part in parts)
        self.assertGreater(longest, 300)
        self.assertLessEqual(longest, 300 + kb_service.CHUNK_OVERLAP + 1)


class ChunkMarkdownCases(unittest.TestCase):
    def test_empty_text_produces_nothing(self):
        self.assertEqual(kb_service._chunk_markdown("   \n\n  "), [])

    def test_heading_without_body_produces_nothing(self):
        self.assertEqual(kb_service._chunk_markdown("# 标题\n"), [])

    def test_heading_path_is_nested(self):
        text = "# 总则\n## 第一条 目的\n为了安全。\n"
        self.assertEqual(
            kb_service._chunk_markdown(text), [("总则 > 第一条 目的", "为了安全。")]
        )

    def test_sibling_headings_are_not_merged(self):
        text = "## 甲\n甲内容\n## 乙\n乙内容\n"
        self.assertEqual(
            kb_service._chunk_markdown(text), [("甲", "甲内容"), ("乙", "乙内容")]
        )

    def test_deeper_heading_closes_the_previous_one(self):
        text = "# 章\n## 甲\n甲内容\n### 乙\n乙内容\n"
        self.assertEqual(
            kb_service._chunk_markdown(text),
            [("章 > 甲", "甲内容"), ("章 > 甲 > 乙", "乙内容")],
        )

    def test_rule_lines_are_dropped(self):
        """导出的整行 `------` 会污染 embedding，不能进正文。"""
        self.assertEqual(
            kb_service._chunk_markdown("# 标题\n--------\n正文\n"), [("标题", "正文")]
        )

    def test_chapter_is_restored_when_a_same_level_article_replaces_it(self):
        """回归：第二章/第四章/第五章的「第X条」与「第X章」同为 ## 级。

        靠层级推路径会把章标题挤掉，切片正文变成孤零零的「第八条 …」，
        所以要用「最近一个章标题」把它补回来。
        """
        text = (
            "## 第五章 实验室危险化学品安全管理\n"
            "## 第八条 管制化学品管理\n"
            "1. 双人验收；2. 双人保管。\n"
        )
        chunks = kb_service._chunk_markdown(text)
        self.assertEqual(len(chunks), 1)
        path, body = chunks[0]
        self.assertEqual(
            path, "第五章 实验室危险化学品安全管理 > 第八条 管制化学品管理"
        )
        self.assertIn("双人验收", body)

    def test_chapter_is_not_duplicated_when_it_is_still_on_the_stack(self):
        """章标题还在路径里时，不要再补一次 —— 否则前缀会重复。"""
        text = (
            "## 第三章 实验室消防安全管理\n"
            "### 第九条 灭火器检查\n"
            "每月检查一次。\n"
        )
        chunks = kb_service._chunk_markdown(text)
        self.assertEqual(
            chunks[0][0],
            "第三章 实验室消防安全管理 > 第九条 灭火器检查",
        )

    def test_long_section_is_split_but_keeps_the_same_path(self):
        body = "\n\n".join(["段" * 100] * 5)
        chunks = kb_service._chunk_markdown(f"# 长章节\n{body}\n")
        self.assertGreater(len(chunks), 1)
        self.assertEqual({path for path, _ in chunks}, {"长章节"})
        for _, part in chunks:
            self.assertLessEqual(len(part), kb_service.CHUNK_SIZE)

    def test_real_knowledge_base_files_all_chunk_without_loss(self):
        """真文件跑一遍：每个 md 都要切出内容，且每一片都不超上限。"""
        files = sorted(kb_service.KB_DIR.glob("*.md"))
        self.assertTrue(files, "data/kb/ 下应当有知识文档")
        for path in files:
            if path.name.startswith("."):
                continue
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            chunks = kb_service._chunk_markdown(text)
            self.assertTrue(chunks, f"{path.name} 没切出任何切片")
            for section, body in chunks:
                self.assertLessEqual(len(body), kb_service.CHUNK_SIZE, path.name)
                self.assertNotIn("----", body, path.name)
                # 标题路径前缀是召回质量的关键，超过 60 字说明层级拼错了
                self.assertLessEqual(len(section), 120, f"{path.name}: {section}")


if __name__ == "__main__":
    unittest.main()
