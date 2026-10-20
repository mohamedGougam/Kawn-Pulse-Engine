"""Pure-Python tests for pulse_tracking.card_selection (no Django needed).

Run with:  python -m unittest pulse_tracking.test_card_selection
"""
import random
import unittest
from collections import Counter

from pulse_tracking.card_selection import (
    card_key, dedupe_cards, display_topic, pick_balanced_card,
    pick_random_card, title_from_quote, topic_key,
)


def make_cards(n, topic="World Cup", id_prefix="c"):
    return [
        {"topic": topic, "quote": f"quote {i}", "card_id": f"{id_prefix}{i}",
         "source_url": f"https://example.com/{i}"}
        for i in range(n)
    ]


class PickRandomCardTests(unittest.TestCase):
    def test_choice_is_not_always_the_first_card(self):
        cards = make_cards(10)
        rng = random.Random(7)
        picks = Counter(card_key(pick_random_card(cards, set(), rng)) for _ in range(500))
        self.assertGreater(len(picks), 5)               # genuinely varies
        self.assertLess(max(picks.values()), 150)       # no single card dominates

    def test_already_notified_cards_are_never_picked(self):
        cards = make_cards(5)
        notified = {card_key(c) for c in cards[:4]}
        rng = random.Random(1)
        for _ in range(50):
            self.assertEqual(pick_random_card(cards, notified, rng)["card_id"], "c4")

    def test_nothing_new_returns_none(self):
        cards = make_cards(3)
        self.assertIsNone(pick_random_card(cards, {card_key(c) for c in cards}))
        self.assertIsNone(pick_random_card([], set()))

    def test_engine_regenerating_ids_does_not_defeat_dedup(self):
        # Same articles, brand-new card ids each refresh (the engine deletes
        # and rebuilds cards every time). Identity must be source_url.
        sent = set()
        rng = random.Random(3)
        first = make_cards(4, id_prefix="run1-")
        for _ in range(4):
            run = [dict(c, card_id=f"{random.random()}") for c in first]
            picked = pick_random_card(run, sent, rng)
            self.assertIsNotNone(picked)
            self.assertNotIn(card_key(picked), sent)
            sent.add(card_key(picked))
        self.assertEqual(len(sent), 4)                  # all four, no repeats
        self.assertIsNone(pick_random_card(first, sent, rng))  # then: nothing new

    def test_same_article_under_two_topics_counts_once(self):
        a = make_cards(2, topic="A")
        b = make_cards(2, topic="B")                    # same urls
        self.assertEqual(len(dedupe_cards(a + b)), 2)

    def test_pool_spans_all_topics(self):
        pool = make_cards(3, topic="A") + [
            {"topic": "B", "quote": "b", "card_id": "b0", "source_url": "https://b.example/0"}]
        topics = {pick_random_card(pool, set(), random.Random(s))["topic"] for s in range(200)}
        self.assertEqual(topics, {"A", "B"})

    def test_card_without_identity_is_kept(self):
        generic = {"topic": "X", "quote": None, "card_id": None, "source_url": None}
        self.assertEqual(pick_random_card([generic], {"anything"}), generic)


class TitleTests(unittest.TestCase):
    def test_short_quote_used_as_is(self):
        self.assertEqual(title_from_quote("Big news", "Topic"), "Big news")

    def test_long_quote_trimmed(self):
        t = title_from_quote("word " * 60, "Topic")
        self.assertLessEqual(len(t), 80)
        self.assertTrue(t.endswith("…"))

    def test_missing_quote_falls_back_to_topic(self):
        self.assertEqual(title_from_quote(None, "Topic"), "Topic")
        self.assertEqual(title_from_quote("   ", "Topic"), "Topic")


class DisplayTopicTests(unittest.TestCase):
    def test_capitalises_lowercase_words(self):
        self.assertEqual(display_topic("git"), "Git")
        self.assertEqual(display_topic("machine learning"), "Machine Learning")

    def test_keeps_existing_capitals_and_digits(self):
        for t in ("iPhone", "NASA", "GPT-5", "3D printing"):
            self.assertEqual(display_topic(t).split()[0], t.split()[0])

    def test_empty(self):
        self.assertEqual(display_topic(None), "")


class PickBalancedCardTests(unittest.TestCase):
    def pool(self):
        return make_cards(20, topic="git", id_prefix="g") + [
            {"topic": "Rust", "quote": "r", "card_id": "r0", "source_url": "https://r.example/0"},
            {"topic": "Linux", "quote": "l", "card_id": "l0", "source_url": "https://l.example/0"},
        ]

    def test_big_topic_does_not_dominate(self):
        topics = Counter(
            pick_balanced_card(self.pool(), set(), {}, random.Random(s))["topic"]
            for s in range(300))
        self.assertGreater(topics["Rust"], 50)
        self.assertGreater(topics["Linux"], 50)

    def test_prefers_least_recently_notified_topic(self):
        from datetime import datetime, timedelta
        t0 = datetime(2026, 10, 1)
        last = {"git": t0 + timedelta(days=3), "rust": t0 + timedelta(days=2),
                "linux": t0 + timedelta(days=1)}
        for s in range(20):
            self.assertEqual(
                pick_balanced_card(self.pool(), set(), last, random.Random(s))["topic"], "Linux")

    def test_never_notified_topic_wins(self):
        last = {"git": 5, "rust": 4}
        for s in range(20):
            self.assertEqual(
                pick_balanced_card(self.pool(), set(), last, random.Random(s))["topic"], "Linux")

    def test_skips_topics_with_nothing_fresh(self):
        sent = {card_key(c) for c in self.pool() if c["topic"] != "git"}
        self.assertEqual(pick_balanced_card(self.pool(), sent, {})["topic"], "git")

    def test_nothing_new(self):
        sent = {card_key(c) for c in self.pool()}
        self.assertIsNone(pick_balanced_card(self.pool(), sent, {}))

    def test_topic_key_ignores_case(self):
        self.assertEqual(topic_key(" Git "), topic_key("git"))


if __name__ == "__main__":
    unittest.main()
