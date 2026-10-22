"""Tests for ExperimentRunner, scenario execution, and report generation (Priority 4)."""

import pytest
from pathlib import Path

from qos_controller.experiments.runner import ExperimentRunner
from qos_controller.experiments.scenarios import SCENARIOS, get_scenario


def test_list_all_planned_scenarios():
    runner = ExperimentRunner(output_dir="test_reports")
    scenarios = runner.list_scenarios()
    assert len(scenarios) >= 6
    ids = {s["id"] for s in scenarios}
    assert "iso_video_call" in ids
    assert "capacity_drop" in ids
    assert "streams_gaming" in ids
    assert "work_call_intent" in ids
    assert "misclassification" in ids
    assert "enforcement_failure" in ids


def test_get_scenario_valid_and_invalid():
    s = get_scenario("iso_video_call")
    assert s.name == "ISO Download + Interactive Call"
    with pytest.raises(KeyError):
        get_scenario("non_existent_scenario")


def test_run_scenario_produces_reports(tmp_path: Path):
    runner = ExperimentRunner(output_dir=tmp_path)
    job_id = runner.start_scenario("iso_video_call")
    assert job_id.startswith("exp_iso_video_call_")

    # Wait for completion (simulation executes synchronously in thread)
    import time
    for _ in range(50):
        job = runner.get_job(job_id)
        if job and job.state in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert job is not None
    assert job.state == "completed"
    assert job.result is not None

    res = job.result
    assert "no_qos" in res.comparisons
    assert "static_qos" in res.comparisons
    assert "adaptive_qos" in res.comparisons
    assert res.comparisons["adaptive_qos"]["call_rtt_ms"] < res.comparisons["no_qos"]["call_rtt_ms"]

    # Verify generated report files exist and are non-empty
    json_file = tmp_path / f"{job_id}.json"
    csv_file = tmp_path / f"{job_id}.csv"
    md_file = tmp_path / f"{job_id}.md"
    html_file = tmp_path / f"{job_id}.html"

    assert json_file.exists() and json_file.stat().st_size > 0
    assert csv_file.exists() and csv_file.stat().st_size > 0
    assert md_file.exists() and md_file.stat().st_size > 0
    assert html_file.exists() and html_file.stat().st_size > 0

    html_content = html_file.read_text(encoding="utf-8")
    assert "ISO Download + Interactive Call" in html_content
    assert "No QoS (FIFO)" in html_content


def test_capacity_drop_scenario(tmp_path: Path):
    runner = ExperimentRunner(output_dir=tmp_path)
    job_id = runner.start_scenario("capacity_drop")
    import time
    for _ in range(50):
        job = runner.get_job(job_id)
        if job and job.state in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert job and job.state == "completed"
    assert job.result.comparisons["adaptive_qos"]["time_to_adapt_s"] < 5.0
