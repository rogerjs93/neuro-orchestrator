import json

from pipeline.ingest import (
    build_dcm2bids_command,
    participant_label,
    write_default_config,
    DEFAULT_IMAGE,
)


def test_participant_label_strips_prefix():
    assert participant_label("sub-01") == "01"
    assert participant_label("Sub-control02") == "control02"
    assert participant_label("07") == "07"


def test_build_command_structure():
    cmd = build_dcm2bids_command(
        dicom_dir="/host/dicom", participant="sub-01",
        output_dir="/host/data", config="/host/cfg.json",
    )
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert DEFAULT_IMAGE in cmd
    # mounts present
    joined = " ".join(cmd)
    assert "/host/dicom:/dicom:ro" in joined
    assert "/host/data:/bids" in joined
    assert "/host/cfg.json:/config.json:ro" in joined
    # dcm2bids args, label without sub- prefix
    assert cmd[cmd.index("-p") + 1] == "01"
    assert "-s" not in cmd


def test_build_command_with_session():
    cmd = build_dcm2bids_command(
        dicom_dir="/d", participant="01", output_dir="/o", config="/c.json", session="01",
    )
    assert cmd[cmd.index("-s") + 1] == "01"


def test_write_default_config(tmp_path):
    p = write_default_config(tmp_path / ".dcm2bids_config.json")
    cfg = json.loads(p.read_text())
    suffixes = {d["suffix"] for d in cfg["descriptions"]}
    assert {"T1w", "bold", "dwi"}.issubset(suffixes)
