#!/usr/bin/env python3
"""Tests for today.py. Stdlib only, like the module under test.

    python3 -m unittest -v

Nothing here touches the network: the two functions that would are exercised with
_request stubbed. The suite therefore runs in CI ahead of the render step, and
fails the build before a bad card can be committed over a good one.

The card is generated unattended and read by strangers, so what is worth pinning
is what a human would otherwise have to catch by eye: rows are exactly
PANEL_COLS wide, language bars total exactly 100%, and every number is either
measured or absent -- never a placeholder and never a guess.
"""

import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock
from xml.etree import ElementTree

import today


def plain(markup):
    """The visible text of a row, with the tspan markup taken back off."""
    return "".join(ElementTree.fromstring("<t>%s</t>" % markup).itertext())


NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def stamp(**delta):
    """An ISO-8601 UTC stamp `delta` before NOW."""
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------- time

class RelativeTimeTest(unittest.TestCase):
    def test_reports_each_tier_in_its_own_unit(self):
        cases = [
            (dict(seconds=61), "1m ago"),
            (dict(minutes=5), "5m ago"),
            (dict(minutes=30), "30m ago"),
            (dict(hours=4), "4h ago"),
            (dict(hours=23), "23h ago"),
            (dict(days=3), "3d ago"),
            (dict(days=6), "6d ago"),
            (dict(days=40), "6w ago"),
            (dict(days=360), "51w ago"),
            (dict(days=800), "2y ago"),
        ]
        for delta, expected in cases:
            with self.subTest(delta=delta):
                self.assertEqual(today.relative_time(stamp(**delta), now=NOW), expected)

    def test_minutes_tier_is_reachable(self):
        # Regression: aiming the value at the middle of the refresh window added a
        # fixed three hours to every stamp, putting the floor of the range past
        # this tier. A push a minute old reported "3h ago".
        for seconds in (61, 90, 600, 3000):
            with self.subTest(seconds=seconds):
                self.assertTrue(today.relative_time(stamp(seconds=seconds), now=NOW)
                                .endswith("m ago"))

    def test_rounds_rather_than_floors(self):
        # `//` reports 5h59m as "5h ago" -- a full hour of lag, always late.
        self.assertEqual(today.relative_time(stamp(hours=5, minutes=59), now=NOW), "6h ago")
        self.assertEqual(today.relative_time(stamp(hours=23, minutes=50), now=NOW), "1d ago")

    def test_rounding_past_a_tier_falls_through_to_the_next(self):
        # 59m40s rounds to 60 minutes, which is not a thing this says.
        self.assertEqual(today.relative_time(stamp(minutes=59, seconds=40), now=NOW), "1h ago")
        self.assertEqual(today.relative_time(stamp(days=6, hours=20), now=NOW), "1w ago")

    def test_years_floor_so_the_figure_is_never_overstated(self):
        # Rounding would read 1y7m as "2y" -- an overstatement, not a rounding
        # artifact. account_age floors for the same reason.
        self.assertEqual(today.relative_time(stamp(days=572), now=NOW), "1y ago")
        self.assertEqual(today.relative_time(stamp(days=364 + 365), now=NOW), "1y ago")
        self.assertEqual(today.relative_time(stamp(days=730), now=NOW), "2y ago")

    def test_a_stamp_at_or_ahead_of_now_reads_just_now(self):
        # GitHub's clock and the runner's disagree by seconds either way; that is
        # skew, not a push from the future.
        for delta in (dict(seconds=0), dict(seconds=30), dict(seconds=-90), dict(hours=-2)):
            with self.subTest(delta=delta):
                self.assertEqual(today.relative_time(stamp(**delta), now=NOW), "just now")

    def test_unusable_input_drops_the_row_instead_of_failing_the_build(self):
        for value in (None, "", "2026-08-20", "not a date", "2026-08-20 12:00:00"):
            with self.subTest(value=value):
                self.assertIsNone(today.relative_time(value, now=NOW))


class DaysSinceTest(unittest.TestCase):
    def test_counts_whole_days(self):
        past = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        self.assertEqual(today.days_since(past), 5)

    def test_today_is_zero_days(self):
        self.assertEqual(today.days_since(datetime.now(timezone.utc).strftime("%Y-%m-%d")), 0)

    def test_a_future_date_has_no_answer(self):
        ahead = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertIsNone(today.days_since(ahead))

    def test_missing_date(self):
        self.assertIsNone(today.days_since(None))
        self.assertIsNone(today.days_since(""))


class AccountAgeTest(unittest.TestCase):
    @staticmethod
    def months_before_now(count):
        """A created_at exactly `count` calendar months back, on the 1st.

        The 1st because account_age docks a month when the day of the month has
        not come round yet, and no real day is earlier than the 1st.
        """
        now = datetime.now(timezone.utc)
        total = now.year * 12 + (now.month - 1) - count
        return datetime(total // 12, total % 12 + 1, 1, tzinfo=timezone.utc) \
            .strftime("%Y-%m-%dT00:00:00Z")

    def test_reports_years_and_months(self):
        for months, expected in ((1, "1m"), (11, "11m"), (12, "1y"),
                                 (18, "1y 6m"), (55, "4y 7m")):
            with self.subTest(months=months):
                self.assertEqual(today.account_age(self.months_before_now(months)),
                                 expected + " on GitHub")

    def test_says_what_it_measures(self):
        # Account age, not years of experience. The label is what keeps the two
        # from being read as each other, so it is pinned.
        self.assertTrue(today.account_age(self.months_before_now(24)).endswith(" on GitHub"))

    def test_an_account_younger_than_a_month_has_nothing_to_report(self):
        self.assertIsNone(today.account_age(self.months_before_now(0)))

    def test_unusable_input(self):
        ahead = (datetime.now(timezone.utc) + timedelta(days=400)).strftime("%Y-%m-%dT00:00:00Z")
        self.assertIsNone(today.account_age(ahead))
        self.assertIsNone(today.account_age(None))
        self.assertIsNone(today.account_age("not a date"))


@unittest.skipUnless(today.ZoneInfo, "build has no tzdata")
class UtcOffsetTest(unittest.TestCase):
    def test_resolves_a_whole_hour_offset(self):
        self.assertEqual(today.utc_offset({"zone": "UTC"}), "UTC+0")

    def test_appends_the_note(self):
        self.assertEqual(today.utc_offset({"zone": "UTC", "note": "async-friendly"}),
                         "UTC+0 · async-friendly")

    def test_keeps_the_minutes_on_a_half_hour_zone(self):
        self.assertEqual(today.utc_offset({"zone": "Asia/Kolkata"}), "UTC+5:30")

    def test_a_zone_it_cannot_resolve_drops_the_row(self):
        # Better an absent row than a hardcoded offset that is wrong for five
        # months of the year.
        self.assertIsNone(today.utc_offset({"zone": "Mars/Olympus_Mons"}))
        self.assertIsNone(today.utc_offset({}))
        self.assertIsNone(today.utc_offset(None))


# ----------------------------------------------------------------- language bars

class LanguageSharesTest(unittest.TestCase):
    @staticmethod
    def langs(**sizes):
        return {name: {"size": size, "color": "#123456"} for name, size in sizes.items()}

    def test_percentages_total_exactly_one_hundred(self):
        # The bars are a single column of a fixed width, so anything but 100 is
        # visible as a short or overflowing row.
        distributions = [
            dict(a=1, b=1, c=1),                       # three-way split, no exact answer
            dict(a=100, b=33, c=33, d=33, e=1, f=1, g=1, h=1),
            dict(a=7_200_000, b=1_400_000, c=500_000, d=300_000, e=300_000, f=100_000),
            dict(a=1, b=999_999),                      # one dominant, one negligible
            dict(a=5),
        ]
        for sizes in distributions:
            with self.subTest(sizes=sizes):
                shares = today.language_shares(self.langs(**sizes))
                self.assertEqual(sum(int(pct.rstrip("%")) for _, pct, _, _ in shares), 100)

    def test_fraction_agrees_with_the_printed_percent(self):
        for name, pct, fraction, _ in today.language_shares(self.langs(a=3, b=1)):
            with self.subTest(name=name):
                self.assertAlmostEqual(fraction, int(pct.rstrip("%")) / 100.0)

    def test_ranks_by_size(self):
        names = [name for name, _, _, _ in today.language_shares(self.langs(a=1, b=9, c=5))]
        self.assertEqual(names, ["b", "c", "a"])

    def test_everything_past_the_cut_collapses_into_other(self):
        sizes = {chr(ord("a") + i): 10 - i for i in range(today.LANG_COUNT + 3)}
        shares = today.language_shares(self.langs(**sizes))
        self.assertEqual(len(shares), today.LANG_COUNT + 1)
        name, _, _, color = shares[-1]
        self.assertEqual(name, "other")
        # No color: "other" is a mixture, and borrowing one member's color would
        # attribute the whole bar to it.
        self.assertIsNone(color)

    def test_no_other_row_when_everything_fits(self):
        shares = today.language_shares(self.langs(a=2, b=1))
        self.assertNotIn("other", [name for name, _, _, _ in shares])

    def test_nothing_measured_means_no_bars(self):
        self.assertEqual(today.language_shares({}), [])
        self.assertEqual(today.language_shares({"a": {"size": 0, "color": None}}), [])


# ----------------------------------------------------------------- panel rows

class PanelRowTest(unittest.TestCase):
    def test_a_row_is_exactly_the_panel_width(self):
        for length in (1, 5, 20, 40, 47, 50):
            with self.subTest(length=length):
                self.assertEqual(len(plain(today.row("Focus", "x" * length))),
                                 today.PANEL_COLS)

    def test_an_oversized_value_is_truncated_to_fit(self):
        with mock.patch("sys.stderr"):  # the overflow warning is expected here
            markup = today.row("Focus", "x" * 200)
        text = plain(markup)
        self.assertEqual(len(text), today.PANEL_COLS)
        # Ellipsis rather than a hard cut, so a reader can tell it was shortened.
        self.assertTrue(text.endswith("…"))

    def test_the_label_survives_truncation(self):
        # Shaving from the right is what keeps the row identifiable when the value
        # is too long to show.
        with mock.patch("sys.stderr"):
            text = plain(today.row("Portfolio", "x" * 200))
        self.assertIn("Portfolio:", text)

    def test_truncate_shaves_exactly_the_excess_from_the_tail(self):
        # The ellipsis replaces the last surviving character rather than being
        # appended to it, so the shave is exactly `excess` wide. compose() relies
        # on that to land back on the panel width without re-measuring.
        segments = [("keep", "key"), ("drop this", "value")]
        out = today._truncate(segments, 5)
        self.assertEqual(out[0], ("keep", "key"))
        self.assertEqual(out[1][0], "dro…")
        self.assertEqual(sum(len(text) for text, _ in out),
                         sum(len(text) for text, _ in segments) - 5)

    def test_truncate_walks_back_through_earlier_segments(self):
        # A value can be split across segments -- "6 received · 51 given" is three
        # -- so an overflow larger than the last one has to keep going.
        segments = [("head", "key"), ("mid", "dim"), ("tail", "value")]
        out = today._truncate(segments, 6)
        self.assertEqual("".join(text for text, _ in out), "head…")

    def test_a_rule_fills_the_width_it_is_given(self):
        for cols in (today.PANEL_COLS, today.ASCII_COLS):
            with self.subTest(cols=cols):
                self.assertEqual(len(plain(today.rule("Services", cols))), cols)

    def test_a_rule_longer_than_its_width_does_not_go_negative(self):
        self.assertGreater(len(plain(today.rule("x" * 80, today.ASCII_COLS))), 0)

    def test_a_live_service_and_a_dead_one_read_differently(self):
        up = plain(today.service_row("a.com", True, 200, 91, 111))
        self.assertIn("111d", up)
        self.assertIn("200 · 91ms", up)

        refused = plain(today.service_row("a.com", False, None, None, 111))
        self.assertIn("unreachable", refused)

        errored = plain(today.service_row("a.com", False, 503, 12, None))
        self.assertIn("HTTP 503", errored)
        # No uptime figure when the launch date is unknown -- not a zero.
        self.assertNotIn("0d", errored)

    def test_markup_in_a_value_cannot_break_the_svg(self):
        # Values come from config.json and from the API; either could contain a
        # character that is markup here.
        text = plain(today.row("Focus", "a & b <tag>"))
        self.assertIn("a & b <tag>", text)

    def test_esc(self):
        self.assertEqual(today.esc("a & b <c> d"), "a &amp; b &lt;c&gt; d")

    def test_an_empty_segment_emits_nothing(self):
        self.assertEqual(today.span(""), "")
        self.assertEqual(today.span(None), "")


# ----------------------------------------------------------------- monogram

class ArtRectsTest(unittest.TestCase):
    def test_a_solid_run_merges_into_one_rect(self):
        # Two full blocks are four sub-columns by two sub-rows of the same ink, and
        # every internal join between them is a seam the background can show through.
        rects = today.art_rects(["██"], None)
        self.assertEqual(len(rects), 1)

    def test_rects_overlap_rather_than_abut(self):
        [rect] = today.art_rects(["█"], None)
        width = float(rect.split("width='")[1].split("'")[0])
        self.assertAlmostEqual(width, today.CHAR_W + today.OVERLAP, places=2)

    def test_blank_cells_produce_no_ink(self):
        self.assertEqual(today.art_rects(["  "], None), [])
        self.assertEqual(today.art_rects([], None), [])

    def test_a_glyph_it_does_not_know_is_treated_as_blank(self):
        # Better a gap than a crash on an ascii.txt edited by hand.
        self.assertEqual(today.art_rects(["?"], None), [])

    def test_coverage_softens_partly_inked_cells(self):
        self.assertIn('class="aa2"', today.art_rects(["█"], ["5"])[0])
        self.assertIn('class="aa1"', today.art_rects(["█"], ["1"])[0])
        # A fully covered cell carries no class, so it renders at full opacity.
        self.assertNotIn("class=", today.art_rects(["█"], ["9"])[0])

    def test_coverage_tiers(self):
        self.assertIsNone(today._tier(9))
        self.assertIsNone(today._tier(7))
        self.assertEqual(today._tier(6), "aa2")
        self.assertEqual(today._tier(3), "aa2")
        self.assertEqual(today._tier(2), "aa1")
        self.assertEqual(today._tier(0), "aa1")

    def test_differing_coverage_is_not_merged_away(self):
        # Merging these would flatten the gradation that stops the letterforms
        # reading as a staircase.
        self.assertEqual(len(today.art_rects(["██"], ["19"])), 2)


# ----------------------------------------------------------------- github

def repo(name, private=False, stars=0, pushed=None, languages=()):
    return {
        "name": name,
        "isPrivate": private,
        "stargazerCount": stars,
        "pushedAt": pushed,
        "languages": {"edges": [{"size": size, "node": {"name": lang, "color": "#000"}}
                                for lang, size in languages]},
    }


def page(nodes, cursor=None):
    return {
        "user": {
            "createdAt": "2022-01-10T21:38:11Z",
            "followers": {"totalCount": 6},
            "starredRepositories": {"totalCount": 51},
            "repositories": {
                "totalCount": len(nodes),
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                "nodes": nodes,
            },
            "contributionsCollection": {
                "totalCommitContributions": 700,
                "restrictedContributionsCount": 51,
            },
        }
    }


def history(*commits):
    """A PROFILE_QUERY payload. GitHub returns history newest first."""
    return {"repository": {"defaultBranchRef": {"target": {"history": {"nodes": [
        {"committedDate": date, "author": {"user": {"login": login} if login else None}}
        for date, login in commits
    ]}}}}}


class ProfileRepoPushTest(unittest.TestCase):
    def push(self, payload):
        with mock.patch.object(today, "graphql", return_value=payload) as call:
            result = today.profile_repo_push("Aidenkopec")
        self.call = call
        return result

    def test_asks_for_the_lookup_to_be_non_fatal(self):
        # Asserted at the call site, not just on the None it returns: this is the
        # only query on the card whose failure must not take the build with it,
        # and dropping the flag would look like a harmless tidy-up.
        self.push(history(("2026-08-19T22:00:00Z", "Aidenkopec")))
        self.assertIs(self.call.call_args.args[0], today.PROFILE_QUERY)
        self.assertEqual(self.call.call_args.kwargs.get("required"), False)

    def test_takes_the_newest_commit_the_user_wrote(self):
        # The bot's commits are the card rendering itself; counting them makes
        # "Last push" a readout of the cron schedule rather than of any work.
        self.assertEqual(self.push(history(
            ("2026-08-20T09:00:00Z", "github-actions[bot]"),
            ("2026-08-19T22:00:00Z", "Aidenkopec"),
            ("2026-08-18T10:00:00Z", "Aidenkopec"),
        )), "2026-08-19T22:00:00Z")

    def test_login_match_ignores_case(self):
        self.assertEqual(self.push(history(("2026-08-19T22:00:00Z", "aidenkopec"))),
                         "2026-08-19T22:00:00Z")

    def test_no_commit_of_theirs_in_reach(self):
        # Then some other repo is newer anyway and this could never have won.
        self.assertIsNone(self.push(history(("2026-08-20T09:00:00Z", "github-actions[bot]"))))

    def test_a_failed_lookup_costs_one_row_of_reach_not_the_build(self):
        # graphql(required=False) returns None rather than exiting, so a schema
        # change here cannot leave the whole card stale for six hours.
        self.assertIsNone(self.push(None))

    def test_tolerates_a_commit_with_no_github_account_behind_it(self):
        self.assertIsNone(self.push(history(("2026-08-20T09:00:00Z", None))))

    def test_tolerates_an_empty_or_missing_repository(self):
        self.assertIsNone(self.push({"repository": None}))
        self.assertIsNone(self.push({"repository": {"defaultBranchRef": None}}))
        self.assertIsNone(self.push({}))


class GithubStatsTest(unittest.TestCase):
    def collect(self, pages, profile=None, exclude=()):
        pages = list(pages)

        def fake(query, variables, attempts=3, required=True):
            if query is today.PROFILE_QUERY:
                return profile
            return pages.pop(0)

        with mock.patch.object(today, "graphql", side_effect=fake):
            return today.github_stats("Aidenkopec", exclude)

    def test_pages_through_the_whole_repository_list(self):
        stats = self.collect([
            page([repo("a"), repo("b")], cursor="cursor-1"),
            page([repo("c")]),
        ])
        self.assertEqual(stats["repos_public"], 3)

    def test_splits_public_from_private(self):
        # A single combined total reads as inflated the moment a reader clicks
        # through to the Repositories tab and counts the public ones.
        stats = self.collect([page([repo("a"), repo("b", private=True), repo("c", private=True)])])
        self.assertEqual((stats["repos_public"], stats["repos_private"]), (1, 2))

    def test_sums_stars_and_carries_the_profile_totals(self):
        stats = self.collect([page([repo("a", stars=4), repo("b", stars=2)])])
        self.assertEqual(stats["stars"], 6)
        self.assertEqual(stats["starred"], 51)
        self.assertEqual(stats["followers"], 6)

    def test_commits_include_the_private_ones(self):
        self.assertEqual(self.collect([page([repo("a")])])["commits"], 751)

    def test_excluded_repos_lose_their_bytes_but_keep_their_count_and_stars(self):
        # GitHub reports a repo's whole language breakdown regardless of who wrote
        # it, so a vendored project would otherwise dominate the bars. Counts and
        # stars still have to agree with the public profile.
        stats = self.collect(
            [page([repo("mine", stars=1, languages=[("Python", 100)]),
                   repo("freqtrade", stars=2, languages=[("Python", 900_000)])])],
            exclude=["freqtrade"],
        )
        self.assertEqual(stats["repos_public"], 2)
        self.assertEqual(stats["stars"], 3)
        self.assertEqual(stats["languages"]["Python"]["size"], 100)

    def test_exclusion_is_case_insensitive(self):
        stats = self.collect(
            [page([repo("FreqTrade", languages=[("Python", 900_000)])])],
            exclude=["freqtrade"],
        )
        self.assertEqual(stats["languages"], {})

    def test_last_push_is_the_newest_across_repos(self):
        stats = self.collect([page([
            repo("a", pushed="2026-08-01T00:00:00Z"),
            repo("c", pushed="2026-08-19T00:00:00Z"),
            repo("b", pushed="2026-08-10T00:00:00Z"),
        ])])
        self.assertEqual(stats["pushed_at"], "2026-08-19T00:00:00Z")

    def test_the_profile_repos_own_timestamp_is_ignored(self):
        # This workflow commits the rendered SVGs back into it on every run, so its
        # pushedAt reports the cron schedule and would always be the newest.
        stats = self.collect(
            [page([repo("Aidenkopec", pushed="2026-08-20T11:00:00Z"),
                   repo("other", pushed="2026-08-19T00:00:00Z")])],
            profile=history(("2026-08-20T11:00:00Z", "github-actions[bot]")),
        )
        self.assertEqual(stats["pushed_at"], "2026-08-19T00:00:00Z")

    def test_but_real_work_on_the_profile_repo_still_counts(self):
        stats = self.collect(
            [page([repo("Aidenkopec", pushed="2026-08-20T11:00:00Z"),
                   repo("other", pushed="2026-08-19T00:00:00Z")])],
            profile=history(("2026-08-20T11:00:00Z", "github-actions[bot]"),
                            ("2026-08-20T09:00:00Z", "Aidenkopec")),
        )
        self.assertEqual(stats["pushed_at"], "2026-08-20T09:00:00Z")

    def test_the_profile_repo_is_still_counted_and_starred(self):
        stats = self.collect([page([repo("Aidenkopec", stars=6, pushed="2026-08-20T11:00:00Z")])])
        self.assertEqual(stats["repos_public"], 1)
        self.assertEqual(stats["stars"], 6)

    def test_no_push_anywhere_leaves_the_row_absent_rather_than_wrong(self):
        stats = self.collect([page([repo("a")])])
        self.assertIsNone(stats["pushed_at"])
        self.assertIsNone(today.relative_time(stats["pushed_at"], now=NOW))


# ----------------------------------------------------------------- network edges

class Response:
    def __init__(self, status=200, body=b""):
        self.status, self._body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=None):
        return self._body


class ProbeTest(unittest.TestCase):
    def probe(self, *outcomes):
        results = list(outcomes)

        def fake(url, headers=None, data=None, timeout=15):
            outcome = results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(today, "_request", side_effect=fake), \
                mock.patch("today.time.sleep"):
            return today.probe("https://example.com")

    def test_a_live_service(self):
        ok, status, latency = self.probe(Response(200))
        self.assertTrue(ok)
        self.assertEqual(status, 200)
        self.assertIsInstance(latency, int)

    def test_a_redirect_still_counts_as_up(self):
        self.assertTrue(self.probe(Response(301))[0])

    def test_an_http_error_is_down_but_answered(self):
        ok, status, latency = self.probe(
            urllib.error.HTTPError("https://example.com", 503, "err", {}, None))
        self.assertFalse(ok)
        self.assertEqual(status, 503)
        self.assertIsInstance(latency, int)

    def test_one_lost_connection_does_not_condemn_the_service(self):
        # The card can stand for six hours; that is far too long to show a live
        # site as down because a single TCP connect lost a race.
        ok, status, _ = self.probe(OSError("connection reset"), Response(200))
        self.assertTrue(ok)
        self.assertEqual(status, 200)

    def test_an_unreachable_service_is_data_not_an_exception(self):
        self.assertEqual(self.probe(OSError("no route"), OSError("no route")),
                         (False, None, None))


class GraphqlTest(unittest.TestCase):
    def call(self, outcomes, required):
        results = list(outcomes)

        def fake(url, headers=None, data=None, timeout=15):
            outcome = results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(today, "TOKEN", "test-token"), \
                mock.patch.object(today, "_request", side_effect=fake), \
                mock.patch("today.time.sleep"), mock.patch("sys.stderr"):
            return today.graphql("query", {}, attempts=2, required=required)

    def test_returns_the_data_block(self):
        body = json.dumps({"data": {"user": {"login": "Aidenkopec"}}}).encode()
        self.assertEqual(self.call([Response(200, body)], required=True),
                         {"user": {"login": "Aidenkopec"}})

    def test_retries_a_transport_failure(self):
        # A single 502 from GitHub happens, and would otherwise fail an unattended
        # cron and leave the card stale until the next run.
        body = json.dumps({"data": {"ok": True}}).encode()
        self.assertEqual(self.call([OSError("502"), Response(200, body)], required=True),
                         {"ok": True})

    def test_a_required_query_that_cannot_be_answered_fails_the_build(self):
        with self.assertRaises(SystemExit):
            self.call([OSError("502"), OSError("502")], required=True)

    def test_a_query_error_is_not_retried(self):
        # A GraphQL `errors` payload means the query is wrong, and repeating it
        # will not make it right.
        body = json.dumps({"errors": [{"message": "bad field"}]}).encode()
        with self.assertRaises(SystemExit):
            self.call([Response(200, body)], required=True)

    def test_an_optional_query_degrades_instead(self):
        body = json.dumps({"errors": [{"message": "bad field"}]}).encode()
        self.assertIsNone(self.call([Response(200, body)], required=False))
        self.assertIsNone(self.call([OSError("502"), OSError("502")], required=False))


# ----------------------------------------------------------------- whole card

class RenderTest(unittest.TestCase):
    """The offline path end to end.

    Only the measurements are fixtures; every layout, color and glyph decision
    downstream is the real one.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(today.HERE, "config.json"), encoding="utf-8") as fh:
            cls.cfg = json.load(fh)
        cls.stats, cls.services = today.offline_fixtures(cls.cfg)
        cls.rows = today.build_rows(cls.cfg, cls.stats, cls.services)

    def render(self, theme="dark_mode"):
        glyphs, covers = today.read_art()
        blocks = [("Languages by bytes", today.language_shares(self.stats["languages"]))]
        with tempfile.TemporaryDirectory() as tmp:
            path = today.render(theme, today.THEMES[theme], today.art_rects(glyphs, covers),
                                len(glyphs), blocks, self.rows, tmp)
            with open(path, encoding="utf-8") as fh:
                return fh.read()

    def test_both_themes_produce_parseable_svg(self):
        for theme in today.THEMES:
            with self.subTest(theme=theme):
                root = ElementTree.fromstring(self.render(theme))
                self.assertTrue(root.tag.endswith("svg"))

    def test_the_card_carries_the_panel(self):
        text = "".join(ElementTree.fromstring(self.render()).itertext())
        for expected in ("aidenkopec@remote", "Services", "In Production",
                         "Last push", "Last checked"):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_the_theme_palette_reaches_the_output(self):
        self.assertIn(today.THEMES["dark_mode"]["bg"], self.render("dark_mode"))
        self.assertIn(today.THEMES["light_mode"]["bg"], self.render("light_mode"))

    def test_every_panel_row_fits_the_panel(self):
        # A row wider than the panel runs off the card, and nothing downstream
        # would catch it.
        for i, markup in enumerate(self.rows):
            if not markup:
                continue
            with self.subTest(row=i):
                self.assertLessEqual(len(plain(markup)), today.PANEL_COLS)

    def test_offline_fixtures_cover_every_field_the_panel_reads(self):
        # A missing key here surfaces as a KeyError in CI rather than a blank row
        # on the live card.
        self.assertEqual(
            set(self.stats),
            {"repos_public", "repos_private", "stars", "starred", "followers",
             "commits", "created_at", "pushed_at", "languages"},
        )
        self.assertEqual(len(self.services), len(self.cfg["services"]))


if __name__ == "__main__":
    unittest.main()
