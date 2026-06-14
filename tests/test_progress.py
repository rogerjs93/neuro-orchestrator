from pipeline.progress import parse_progress


def test_mrtrix_percent():
    assert parse_progress("mrtrix3", "tckgen: [ 42%] generating tracks") == {"percent": 42}


def test_nipype_finished_node():
    out = parse_progress("fmriprep", '[Node] Finished "fmriprep_wf.single_subject01_wf.bold_reg_wf"')
    assert out["event"] == "finished"
    assert out["node"].endswith("bold_reg_wf")


def test_nipype_running_node():
    out = parse_progress("mriqc", '[ Node ] Running "mriqc_wf.anatMRIQC"')
    assert out == {"event": "running", "node": "mriqc_wf.anatMRIQC"}


def test_fastsurfer_pass():
    assert parse_progress("fastsurfer", "Processing Coronal view ...") == {"phase": "coronal pass"}
    # the same word for a different stage should not be treated as a FS pass
    assert parse_progress("connectivity", "coronal something") is None


def test_unparseable_returns_none():
    assert parse_progress("mriqc", "Loading configuration") is None
    assert parse_progress("mriqc", "") is None


def test_percent_out_of_range_ignored():
    assert parse_progress("mrtrix3", "version 999% wrong") is None or \
           parse_progress("mrtrix3", "value 250 here") is None
