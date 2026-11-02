import json

import pandas as pd

from evals import calibrate, report, run_eval
from evals.run_eval import COLUMNS


def test_fixture_is_complete(fixture_dir):
    ok, why = run_eval.data_is_complete(fixture_dir)
    assert ok, why
    items = run_eval.load_items(fixture_dir, "all")
    assert len(items) == 20
    held = run_eval.load_items(fixture_dir, "heldout")
    cal = run_eval.load_items(fixture_dir, "calibration")
    assert not {i.action.action_id for i in held} & {i.action.action_id for i in cal}


def test_end_to_end(tmp_path, fixture_dir):
    out = tmp_path / "results"
    rc = run_eval.main(["--gate", "rules,passthrough,tollgate_offline,llm_judge,tollgate_decisions",
                        "--split", "heldout", "--runs", "2", "--data", str(fixture_dir), "--out", str(out)])
    assert rc == 0
    csvs = sorted(out.glob("*.csv"))
    assert len(csvs) == 6   # 3 offline gates x 2 runs; live gates skipped
    df = pd.read_csv(csvs[0], keep_default_na=False)
    assert list(df.columns) == COLUMNS and len(df) == 14
    meta = json.loads(csvs[0].with_suffix(".json").read_text())
    for k in ("gate", "model_versions", "policy_file_hash", "git_commit", "timestamp", "split"):
        assert k in meta
    assert meta["policy_file_hash"] and meta["fixture_data"] is True

    rules = pd.read_csv(next(out.glob("rules_heldout_1_*.csv")), keep_default_na=False)
    assert set(rules["verdict"]) <= {"allow", "escalate", "deny"}
    assert (rules[rules.label == "attack"]["verdict"] != "allow").any()

    rep = tmp_path / "report"
    md = report.build_report(out, rep, "heldout")
    text = md.read_text()
    for g in ("rules", "passthrough", "tollgate_offline"):
        assert f"`{g}`" in text
    assert "not the real system" in text
    assert (rep / "tradeoff.png").stat().st_size > 1000
    assert (rep / "per_class.png").stat().st_size > 1000


def test_all_skipped_returns_nonzero(tmp_path, fixture_dir):
    assert run_eval.main(["--gate", "llm_judge", "--data", str(fixture_dir), "--out", str(tmp_path)]) == 1
    assert not list(tmp_path.glob("*.csv"))


def test_calibrate_refuses_heldout(capsys):
    assert calibrate.main(["--gate", "tollgate_offline", "--split", "heldout"]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_calibrate_runs_offline(fixture_dir, capsys):
    assert calibrate.main(["--gate", "tollgate_offline", "--data", str(fixture_dir)]) == 0
    assert "Suggested tiers" in capsys.readouterr().out
