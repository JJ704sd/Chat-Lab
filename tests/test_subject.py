import unittest

from chatlog_assistant.subject import SubjectResolver


class SubjectResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = SubjectResolver()

    def test_zhongji_suffix_is_zhongji(self) -> None:
        match = self.resolver.resolve("张某 @中技物流")
        self.assertEqual(match.bucket, "zhongji")
        self.assertEqual(match.raw_subject, "中技物流")

    def test_other_suffix_is_other(self) -> None:
        match = self.resolver.resolve("客服 @嘉航盛物流")
        self.assertEqual(match.bucket, "other")

    def test_corp_name_is_zhongji_without_display_suffix(self) -> None:
        match = self.resolver.resolve("张浩", "中技物流")
        self.assertEqual(match.bucket, "zhongji")
        self.assertEqual(match.raw_subject, "中技物流")

    def test_body_mentions_are_not_part_of_subject_resolution(self) -> None:
        match = self.resolver.resolve("客服 @嘉航盛物流")
        self.assertEqual(match.bucket, "other")

    def test_missing_suffix_falls_back_to_other(self) -> None:
        self.assertEqual(self.resolver.resolve("客服").bucket, "other")


if __name__ == "__main__":
    unittest.main()

