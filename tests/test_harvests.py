import json
from decimal import Decimal
from pathlib import Path

from research.harvest_scoreboard import _bucket_relation, _interval, findings_from_harvest
from research.harvests import HarvestBundle


def _write_bundle(harvest_dir: Path) -> None:
    harvest_dir.mkdir(parents=True)
    (harvest_dir / "kalshi_series.json").write_text(
        json.dumps(
            {
                "KXCPIYOY": [
                    {
                        "ticker": "KXCPI-26AUG-T2.1",
                        "title": "August 2026 CPI above 2.1",
                        "result": "yes",
                        "expiration_value": "2.5",
                    },
                    {
                        "ticker": "KXCPI-26JUL-T3.0",
                        "title": "July 2026 CPI above 3.0",
                        "result": "yes",
                        "expiration_value": "2.7",
                    },
                ]
            }
        )
    )
    (harvest_dir / "polymarket_events.json").write_text(
        json.dumps(
            [
                {
                    "title": "US CPI August 2026",
                    "slug": "us-cpi-august-2026",
                    "markets": [
                        {
                            "conditionId": "0xaug",
                            "question": "CPI August 2026 between 2.4 and 2.6?",
                            "outcomePrices": '["1", "0"]',
                            "closed": True,
                        }
                    ],
                },
                {
                    "title": "US CPI July 2026",
                    "slug": "us-cpi-july-2026",
                    "markets": [
                        {
                            "conditionId": "0xjul",
                            "question": "CPI July 2026 between 2.6 and 2.8?",
                            "outcomePrices": '["1", "0"]',
                            "closed": True,
                        }
                    ],
                },
            ]
        )
    )


def test_bundle_loads_and_flattens_events(tmp_path: Path) -> None:
    _write_bundle(tmp_path / "harvests")
    bundle = HarvestBundle.load(tmp_path / "harvests")

    assert not bundle.empty
    assert "polymarket_cpi_resolved.json: missing" in bundle.warnings
    assert {m["event_slug"] for m in bundle.polymarket_markets()} == {"us-cpi-august-2026", "us-cpi-july-2026"}


def test_missing_harvest_dir_is_empty_not_fatal(tmp_path: Path) -> None:
    bundle = HarvestBundle.load(tmp_path / "nope")
    assert bundle.empty
    assert findings_from_harvest(bundle) == {}


def test_interval_and_relation_parsing() -> None:
    kalshi = {"title": "August 2026 CPI above 2.1"}
    poly = {"question": "CPI August 2026 between 2.4 and 2.6?"}
    assert _interval(kalshi).lower == Decimal("2.1") and _interval(kalshi).upper is None
    assert _bucket_relation(kalshi, poly) == "kalshi_contains_polymarket"
    assert _bucket_relation({"title": "CPI below 2.0"}, poly) == "disjoint"
    assert _bucket_relation({"title": "CPI somewhere"}, poly) == "unparsed"


def test_findings_count_real_divergences_only(tmp_path: Path) -> None:
    _write_bundle(tmp_path / "harvests")
    findings = findings_from_harvest(HarvestBundle.load(tmp_path / "harvests"))

    macro = findings["macro_admitted_bucket_divergences"]
    # August: Polymarket resolved 2.4-2.6 inside Kalshi's "above 2.1" => Kalshi YES agrees.
    # July: Polymarket resolved 2.6-2.8, disjoint from "above 3.0" => Kalshi should be NO but is YES.
    assert macro["sample_size"] == 2
    assert macro["observed"] == 1
    assert macro["label"] == "macro 1/2 bucket divergences"
    assert "fed_exact_divergences" not in findings  # no Fed data harvested -> no invented count
