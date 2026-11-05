import os
import unittest
from pathlib import Path

from skillevo import config, curator, skills
from tests.helpers import IsolatedTestCase


def mk(root: Path, name: str, desc: str, body: str):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f'---\nname: {name}\ndescription: "{desc}"\n---\n{body}\n', encoding="utf-8")
    return d


BODY_A = "# Docker cache\n" + "\n".join(f"- Rule {i}: clear docker build cache layers image base digest pin rebuild {i}" for i in range(8))
BODY_B = BODY_A + "\n- Extra unique tip: use buildkit inline cache exporters for registry reuse in pipelines"


class CuratorTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "skills"
        mk(self.dir, "docker-cache", "Use when docker builds use stale cached layers and the image base is pinned", BODY_A)
        mk(self.dir, "docker-cache-extra", "Use when docker builds use stale cached layers and the image base digest is pinned", BODY_B)
        mk(self.dir, "poetry-tips", "Use when managing python dependencies with poetry lock files", "# Poetry\nRun poetry lock after editing pyproject. Use groups for dev tools.")
        mk(self.dir, "empty-one", "Use when nothing at all happens in this broken skill", "")
        d = mk(self.dir, "broken-refs", "Use when reading the broken references skill documentation", "See [guide](references/missing.md) for details on the broken thing here.")
        self.items = skills.discover([self.dir])
        self.cfg = config.load(self.tmp)

    def verdicts(self, **kw):
        return {f.name: f for f in curator.analyze(self.items, self.cfg, **kw)}

    def test_duplicates_merge_into_best_primary(self):
        v = self.verdicts()
        merged = [f for f in v.values() if f.verdict == "merge"]
        self.assertEqual(len(merged), 1)
        self.assertTrue({merged[0].name, merged[0].merge_into} == {"docker-cache", "docker-cache-extra"})
        self.assertEqual(v["poetry-tips"].verdict, "keep")

    def test_structural_findings_and_tags(self):
        v = self.verdicts()
        self.assertEqual(v["empty-one"].verdict, "retire")
        self.assertIn("structural", v["empty-one"].sources())
        self.assertEqual(v["broken-refs"].verdict, "improve")
        self.assertTrue(any("broken file reference" in d for _, d in v["broken-refs"].evidence))

    def test_unmeasured_is_not_waste(self):
        v = self.verdicts(usage={}, span_days=2)
        self.assertEqual(v["poetry-tips"].usage, "unmeasured")
        self.assertEqual(v["poetry-tips"].verdict, "keep")

    def test_zero_usage_alone_only_reviews_but_with_second_source_retires(self):
        v = self.verdicts(usage={"docker-cache": {"uses": 5}}, span_days=60)
        self.assertEqual(v["poetry-tips"].verdict, "review")
        self.assertEqual(v["poetry-tips"].sources(), {"usage"})
        v2 = self.verdicts(usage={"docker-cache": {"uses": 5}}, span_days=60, interests=["docker"])
        self.assertEqual(v2["poetry-tips"].verdict, "retire")
        self.assertEqual(v2["poetry-tips"].sources(), {"usage", "profile"})
        self.assertEqual(v2["docker-cache"].usage, "5 use(s) in 60d")

    def test_judge_can_downgrade_duplicate_to_related(self):
        v = {f.name: f for f in curator.analyze(self.items, self.cfg, judge=lambda a, b: False)}
        self.assertFalse(any(f.verdict == "merge" for f in v.values()))
        self.assertTrue(any(f.verdict == "review" for f in v.values()))

    def test_merge_draft_keeps_primary_and_adds_only_novel_lines(self):
        a = next(s for s in self.items if s.name == "docker-cache")
        b = next(s for s in self.items if s.name == "docker-cache-extra")
        draft = curator.draft_merge(a, [b])
        self.assertIn("buildkit inline cache", draft)
        self.assertEqual(draft.count("Rule 3:"), 1)
        self.assertIn("Also covers: docker-cache-extra", draft)
        errors, _ = skills.validate(draft, "docker-cache")
        self.assertEqual(errors, [])

    def test_model_verdict_can_promote_below_threshold_pair_and_picks_survivor(self):
        self.cfg.curator.overlap_threshold = 0.99          # TF-IDF alone would call these merely related
        def judge(a, b):
            return curator.PairVerdict(True, "older copy", keep="docker-cache-extra", source="fake") if {a.name, b.name} == {"docker-cache", "docker-cache-extra"} else None
        v = {f.name: f for f in curator.analyze(self.items, self.cfg, judge=judge)}
        self.assertEqual(v["docker-cache"].merge_into, "docker-cache-extra")       # survivor chosen by the judge, not by size
        self.assertTrue(any("judged a duplicate by fake" in d for _, d in v["docker-cache"].evidence))

    def test_analysis_does_not_modify_inputs(self):
        before = {p: p.read_bytes() for p in self.dir.rglob("*") if p.is_file()}
        curator.analyze(self.items, self.cfg)
        self.assertEqual(before, {p: p.read_bytes() for p in self.dir.rglob("*") if p.is_file()})


@unittest.skipUnless(os.environ.get("SKILLEVO_TEST_CORPUS"), "set SKILLEVO_TEST_CORPUS to a directory of skills to run")
class CorpusTests(unittest.TestCase):
    """Optional: parse every SKILL.md of a third-party corpus (e.g. a fresh clone of ECC's skills/ folder)."""

    def test_every_skill_parses(self):
        items = skills.discover([Path(os.environ["SKILLEVO_TEST_CORPUS"])])
        self.assertGreater(len(items), 0)
        bad = [s.path.name for s in items if not s.description]
        self.assertEqual(bad, [], f"skills without a parsed description: {bad[:10]}")


class PairLabelFixtureTests(unittest.TestCase):
    def test_fixture_is_well_formed(self):
        import json
        data = json.loads((Path(__file__).parent / "fixtures" / "ecc_pair_labels.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["pairs"]), 20)
        self.assertEqual({p["label"] for p in data["pairs"]}, {"duplicate", "complementary"})
        self.assertIn("single labeler", data["labeler"])
        keys = [frozenset((p["a"], p["b"])) for p in data["pairs"]]
        self.assertEqual(len(keys), len(set(keys)))

    @unittest.skipUnless(os.environ.get("SKILLEVO_TEST_CORPUS"), "set SKILLEVO_TEST_CORPUS to run")
    def test_every_labelled_skill_exists_in_the_corpus(self):
        import json
        names = {s.name for s in skills.discover([Path(os.environ["SKILLEVO_TEST_CORPUS"])])}
        data = json.loads((Path(__file__).parent / "fixtures" / "ecc_pair_labels.json").read_text(encoding="utf-8"))
        missing = {n for p in data["pairs"] for n in (p["a"], p["b"])} - names
        self.assertEqual(missing, set())
