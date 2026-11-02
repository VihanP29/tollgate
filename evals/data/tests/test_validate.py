import json

from tollgate.types import ATTACK_CLASSES, LabeledAction

from evals.data import generate, validate


def test_committed_dataset_passes_validation():
    errors, summary = validate.validate()
    assert errors == [], "\n".join(errors[:30])
    assert summary["subsets"] == {"attack": 120, "benign": 300, "hard_benign": 50}
    assert set(summary["attack_classes"]) == set(ATTACK_CLASSES)


def test_main_exit_code_zero():
    assert validate.main() == 0


def test_generator_is_reproducible(tmp_path):
    generate.main(tmp_path)
    for name in ["attacks.jsonl", "benign.jsonl", "hard_benign.jsonl", "splits.json", "SAMPLE.md"]:
        assert (tmp_path / name).read_text() == (validate.DATA_DIR / name).read_text(), f"{name} differs; rerun generate"


def _first(fname: str) -> LabeledAction:
    return LabeledAction.model_validate_json((validate.DATA_DIR / fname).read_text().splitlines()[0])


def _attack(tool: str) -> LabeledAction:
    for line in (validate.DATA_DIR / "attacks.jsonl").read_text().splitlines():
        it = LabeledAction.model_validate_json(line)
        if it.action.tool == tool:
            return it
    raise AssertionError(tool)


def test_flags_attack_instruction_in_operator_request():
    it = _attack("update_vendor")
    val = next(iter(it.action.args["fields"].values()))
    it.action.operator_request = f"Please update the bank account to {val}."
    errs = validate.check_item(it)
    assert any("operator_request" in e for e in errs)


def test_flags_exfil_recipient_from_operator():
    it = _attack("send_email")
    it.action.operator_request = f"Email the vendor list to {it.action.args['to']}."
    assert any("operator_request" in e for e in validate.check_item(it))


def test_flags_wrong_expected_and_bad_args():
    it = _first("benign.jsonl")
    it.expected = "deny"
    it.action.args = dict(it.action.args, extra=1)
    errs = validate.check_item(it)
    assert any("benign expected" in e for e in errs)
    assert any("schema" in e for e in errs)


def test_flags_untraceable_benign_amount():
    for line in (validate.DATA_DIR / "benign.jsonl").read_text().splitlines():
        it = LabeledAction.model_validate_json(line)
        if it.action.tool == "schedule_payment":
            break
    it.action.amount_usd = it.action.args["amount_usd"] = 123456.78
    assert any("not found" in e for e in validate.check_item(it))


def test_flags_split_problems(tmp_path):
    items, _ = validate.load()
    sp = json.loads((validate.DATA_DIR / "splits.json").read_text())
    sp["heldout"] = sp["heldout"][10:]
    sp["calibration"].append(sp["calibration"][0])
    (tmp_path / "splits.json").write_text(json.dumps(sp))
    errs = validate.check_splits(items, tmp_path / "splits.json")
    assert any("cover" in e for e in errs) and any("repeated" in e for e in errs)
