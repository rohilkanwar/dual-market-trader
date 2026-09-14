import json
import subprocess
import sys
from pathlib import Path


def test_paper_loop_once_with_fixtures(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.paper_loop",
            "--once",
            "--artifact-dir",
            str(artifact_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    latest = json.loads((artifact_dir / "paper_loop_latest.json").read_text())
    history = (artifact_dir / "paper_loop_history.jsonl").read_text().splitlines()
    assert latest["paper_only"] is True
    assert latest["mode"] == "fixtures"
    assert latest["primary_track"] == "single_venue_fair_value"
    assert len(latest["tracks"]) == 6
    assert len(history) == 1
    structured_log = json.loads(result.stderr.strip())
    assert structured_log["event"] == "paper_loop_cycle_completed"
    assert set(structured_log["tracks"]) == {
        "gated_cross_venue_macro",
        "ungated_cross_venue_macro",
        "single_venue_fair_value",
        "sports_cross_venue",
        "small_deliberate_bet",
        "news_underreaction",
    }
