"""Search ranking: which zone matched, how much it counted, and context ranges."""

from __future__ import annotations

import math

from conftest import make_capsule, make_gene
from evomap_genes.scoring import context_ranges, score_asset, tokenize


def test_tokenize_drops_punctuation_and_single_chars() -> None:
    assert tokenize("TimeoutError: a retry-backoff (x2)") == [
        "timeouterror",
        "retry",
        "backoff",
        "x2",
    ]


def test_tokenize_dedupes_and_keeps_order() -> None:
    assert tokenize("retry retry backoff retry") == ["retry", "backoff"]


def test_title_outranks_body() -> None:
    in_title = make_gene(id="a", title="connection pooling", strategy={})
    in_body = make_gene(id="b", title="unrelated", strategy={"note": "connection pooling"})
    title_score, _ = score_asset(in_title, "connection pooling")
    body_score, _ = score_asset(in_body, "connection pooling")
    assert title_score > body_score > 0.0


def test_tag_hits_are_weighted_above_body() -> None:
    tagged = make_gene(id="a", title="x", tags=["flaky"], strategy={})
    bodied = make_gene(id="b", title="x", tags=[], strategy={"note": "flaky"})
    assert score_asset(tagged, "flaky")[0] > score_asset(bodied, "flaky")[0]


def test_no_evidence_scores_zero() -> None:
    score, why = score_asset(make_gene(), "quantum chromodynamics")
    assert score == 0.0
    assert "no keyword or context evidence" in why


def test_score_stays_inside_unit_interval() -> None:
    gene = make_gene(title="timeout timeout timeout retry network", tags=["timeout", "network"])
    score, _ = score_asset(gene, "timeout retry network")
    assert 0.0 < score < 1.0


def test_why_names_the_zones_that_matched() -> None:
    _, why = score_asset(make_gene(), "timeout")
    assert "title~'timeout'" in why
    assert "tags~'timeout'" in why


def test_scoring_is_deterministic() -> None:
    gene = make_gene()
    first = score_asset(gene, "timeout retry", {"tilt_deg": 10})
    for _ in range(5):
        assert score_asset(gene, "timeout retry", {"tilt_deg": 10}) == first


# ------------------------------------------------------------ context ranges


def test_range_from_precondition_text() -> None:
    gene = make_gene(preconditions=["tilt_deg <= 30", "battery_v >= 11.0"])
    assert context_ranges(gene) == {
        "tilt_deg": (-math.inf, 30.0),
        "battery_v": (11.0, math.inf),
    }


def test_range_from_between_syntax() -> None:
    gene = make_gene(preconditions=["0 <= tilt_deg <= 30"])
    assert context_ranges(gene)["tilt_deg"] == (0.0, 30.0)


def test_multiple_constraints_intersect() -> None:
    gene = make_gene(preconditions=["tilt_deg >= 5", "tilt_deg <= 30"])
    assert context_ranges(gene)["tilt_deg"] == (5.0, 30.0)


def test_range_from_strategy_context_pair() -> None:
    gene = make_gene(preconditions=[], strategy={"context": {"tilt_deg": [0, 30]}})
    assert context_ranges(gene)["tilt_deg"] == (0.0, 30.0)


def test_range_from_min_max_dict_and_extra() -> None:
    gene = make_gene(preconditions=[], extra={"context": {"load_nm": {"min": 1, "max": 4}}})
    assert context_ranges(gene)["load_nm"] == (1.0, 4.0)


def test_capsule_environment_supplies_ranges() -> None:
    assert context_ranges(make_capsule())["battery_v"] == (11.0, 12.6)


def test_context_inside_range_adds_and_says_so() -> None:
    gene = make_gene(preconditions=["0 <= tilt_deg <= 30"])
    base, _ = score_asset(gene, "timeout")
    inside, why = score_asset(gene, "timeout", {"tilt_deg": 12})
    assert inside > base
    assert "context tilt_deg=12 in [0,30]" in why


def test_context_outside_range_subtracts_and_says_so() -> None:
    gene = make_gene(preconditions=["0 <= tilt_deg <= 30"])
    base, _ = score_asset(gene, "timeout")
    outside, why = score_asset(gene, "timeout", {"tilt_deg": 47})
    assert outside < base
    assert "context tilt_deg=47 outside [0,30]" in why


def test_out_of_range_can_veto_a_weak_keyword_match() -> None:
    gene = make_gene(
        id="g",
        title="x",
        tags=[],
        preconditions=["tilt_deg <= 30"],
        strategy={"note": "flaky"},
        constraints=[],
        validation=[],
    )
    score, _ = score_asset(gene, "flaky", {"tilt_deg": 90})
    assert score == 0.0


def test_open_ended_range_is_rendered_with_infinity() -> None:
    gene = make_gene(preconditions=["battery_v >= 11.0"])
    _, why = score_asset(gene, "timeout", {"battery_v": 12.0})
    assert "[11,+inf]" in why


def test_non_numeric_context_value_matches_text() -> None:
    capsule = make_capsule()
    _, why = score_asset(capsule, "retry", {"platform": "linux"})
    assert "context platform='linux' mentioned" in why


def test_unknown_context_key_is_ignored_gracefully() -> None:
    gene = make_gene()
    with_ctx, _ = score_asset(gene, "timeout", {"totally_unknown_key": 3})
    without, _ = score_asset(gene, "timeout")
    assert with_ctx == without


# --------------------------------------------------------------- CJK tokenization

def test_tokenize_splits_cjk_into_bigrams() -> None:
    """Chinese has no spaces, so a word regex finds nothing -- bigrams do."""
    assert tokenize("腿板掉线") == ["腿板", "板掉", "掉线"]
    assert tokenize("好") == ["好"]                       # a lone character survives
    assert tokenize("retry 超时 timeout") == ["retry", "超时", "timeout"]


def test_tokenize_keeps_document_order_across_scripts() -> None:
    assert tokenize("alpha 中文 beta") == ["alpha", "中文", "beta"]


def test_cjk_query_finds_cjk_asset() -> None:
    """The regression this fixes: a Chinese query used to score every asset 0.0."""
    hit = make_capsule(id="a", title="腿板掉线恢复步骤", trigger_signals=["关节角度全零"])
    miss = make_capsule(id="b", title="unrelated english title", trigger_signals=["nothing"])
    hit_score, why = score_asset(hit, "腿板掉线 关节全零")
    miss_score, _ = score_asset(miss, "腿板掉线 关节全零")
    assert hit_score > 0.0, why
    assert miss_score == 0.0


def test_cjk_and_latin_queries_are_scored_the_same_way() -> None:
    """A title hit must outrank a body hit in Chinese too, not just in English."""
    in_title = make_capsule(id="a", title="连接池复用", content="x" * 60)
    in_body = make_capsule(id="b", title="无关标题", content="连接池复用" + "x" * 60)
    title_score, _ = score_asset(in_title, "连接池复用")
    body_score, _ = score_asset(in_body, "连接池复用")
    assert title_score > body_score > 0.0
