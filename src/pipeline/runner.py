"""Pipeline runner — builds docker run commands and streams output line by line."""
from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
from pathlib import Path
from typing import AsyncIterator, List, Optional, Tuple

import nibabel as nib

from pipeline.state import STAGE_ORDER, STAGE_REQUIRES, SubjectState, StageStatus
from pipeline.manifest import ArtifactManifest


class PipelineRunner:
    def __init__(
        self,
        data_dir: Path,
        output_dir: Path,
        fs_license: Path,
        mock: bool = False,
    ) -> None:
        self.data_dir = data_dir
        self.output_dir = output_dir
        self.fs_license = fs_license
        self.mock = mock
        # Shared artifact ledger — used to resolve stage inputs by role.
        self.manifest = ArtifactManifest(output_dir / "derivatives")
        self.fastsurfer_use_gpu = os.getenv("FASTSURFER_USE_GPU", "0").strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.fastsurfer_gpu_device = os.getenv("FASTSURFER_GPU_DEVICE", "all").strip() or "all"
        self.fastsurfer_docker_user = os.getenv("FASTSURFER_DOCKER_USER", "0:0").strip() or "0:0"
        self.mriqc_nprocs = os.getenv("MRIQC_NPROCS", "1").strip() or "1"
        self.mriqc_omp_nthreads = os.getenv("MRIQC_OMP_NTHREADS", "1").strip() or "1"
        self.container_ref = os.getenv("HOSTNAME", "").strip()
        # HOST paths are needed so `docker run -v` resolves on the Docker daemon host,
        # not inside the orchestrator container.
        self.host_root_env = os.getenv("HOST_PROJECT_DIR", "").strip()
        self.host_root, self.host_root_source = self._resolve_host_root()

    # ── Public API ─────────────────────────────────────────────────────────────

    def pending_stages(self, sub: SubjectState) -> List[str]:
        return [
            s for s in STAGE_ORDER
            if sub.stage_status.get(s) in (StageStatus.PENDING, StageStatus.FAILED)
        ]

    def validate_stage_outputs(self, subject_id: str, stage: str) -> Tuple[bool, str]:
        """Verify that expected artifacts exist before declaring a stage completed."""
        if stage == "mriqc":
            out = self.output_dir / "mriqc"
            patterns = [
                f"{subject_id}/**/*",
                f"**/*{subject_id}*",
            ]
            if any(list(out.glob(p)) for p in patterns):
                return True, ""
            return False, f"MRIQC completed but no outputs were found under {out} for {subject_id}."

        if stage == "fastsurfer":
            out = self.output_dir / "fastsurfer"
            patterns = [
                f"{subject_id}/**/aparc.DKTatlas+aseg.deep.mgz",
                f"{subject_id}/**/aparc+aseg.mgz",
                f"{subject_id}/**/aseg.mgz",
                f"{subject_id}/**/*aseg*.mgz",
                f"{subject_id}/**/*aseg*.nii.gz",
                f"{subject_id}/**/*aseg*.nii",
            ]
            if any(list(out.glob(p)) for p in patterns):
                return True, ""
            return False, f"FastSurfer completed but no segmentation file was found under {out} for {subject_id}."

        if stage == "fmriprep":
            out = self.output_dir / "fmriprep"
            patterns = [
                f"{subject_id}/**/*space-MNI*preproc_bold.nii.gz",
                f"{subject_id}/**/*desc-preproc*_bold.nii.gz",
                f"{subject_id}/**/*desc-preproc*_T1w.nii.gz",
            ]
            if any(list(out.glob(p)) for p in patterns):
                return True, ""
            return False, f"fMRIPrep completed but expected preprocessed outputs were not found under {out} for {subject_id}."

        if stage == "mrtrix3":
            tracks = self.output_dir / "mrtrix3" / subject_id / "tracks.tck"
            if tracks.exists():
                return True, ""
            return False, f"MRtrix3 completed but tractography file is missing: {tracks}."

        if stage == "connectivity":
            fc = self.output_dir / "connectivity" / f"{subject_id}_fc_matrix.npy"
            if fc.exists():
                return True, ""
            return False, f"Connectivity completed but FC matrix is missing: {fc}."

        if stage == "network":
            metrics = self.output_dir / "network" / f"{subject_id}_network_metrics.json"
            if metrics.exists():
                return True, ""
            return False, f"Network stage completed but metrics JSON is missing: {metrics}."

        if stage == "mask":
            versions = self.output_dir / "masks" / subject_id / "versions"
            if versions.is_dir() and any(versions.glob("*.nii.gz")):
                return True, ""
            return False, f"Mask stage completed but no mask version was found under {versions} for {subject_id}."

        return True, ""

    async def run_stage(
        self, subject_id: str, stage: str
    ) -> AsyncIterator[str]:
        """Async generator — yields log lines for the given stage."""
        if self.mock:
            async for line in self._mock_stage(subject_id, stage):
                yield line
            return

        if stage == "mriqc":
            ok, msg = await self._mriqc_preflight(subject_id)
            if not ok:
                yield msg
                raise RuntimeError("MRIQC preflight failed")

        if stage == "fastsurfer":
            ok, msg = self._fastsurfer_preflight(subject_id)
            if not ok:
                yield msg
                raise RuntimeError("FastSurfer preflight failed")

        if stage == "fmriprep":
            ok, msg = self._fmriprep_preflight(subject_id)
            if not ok:
                yield msg
                raise RuntimeError("fMRIPrep preflight failed")

        cmd = self._build_command(subject_id, stage)
        if not cmd:
            yield f"[dim]No command for stage '{stage}' — check modalities and config[/dim]"
            return

        # Ensure output dir exists
        stage_out = self.output_dir / stage
        stage_out.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            output_lines: List[str] = []
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                output_lines.append(line)
                yield line

            await proc.wait()
            returncode = proc.returncode

            if returncode == 0:
                valid, err = self.validate_stage_outputs(subject_id, stage)
                if not valid:
                    yield f"[red]{err} Treating stage as failed.[/red]"
                    returncode = 1

            if returncode != 0:
                if stage == "fastsurfer":
                    if self._is_fastsurfer_user_guard(output_lines):
                        fallback_user = "0:0"
                        if self.fastsurfer_docker_user != fallback_user:
                            yield (
                                "[yellow]FastSurfer rejected the configured docker user; "
                                f"retrying once with {fallback_user}...[/yellow]"
                            )
                            retry_cmd = self._build_command(
                                subject_id,
                                stage,
                                force_cpu=False,
                                docker_user=fallback_user,
                            )
                            if not retry_cmd:
                                raise RuntimeError(f"Exit code {returncode}")

                            retry_proc = await asyncio.create_subprocess_exec(
                                *retry_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.STDOUT,
                            )
                            assert retry_proc.stdout is not None
                            retry_lines: List[str] = []
                            async for raw in retry_proc.stdout:
                                line = raw.decode("utf-8", errors="replace").rstrip()
                                retry_lines.append(line)
                                yield line

                            await retry_proc.wait()
                            retry_code = retry_proc.returncode
                            if retry_code == 0:
                                return
                            output_lines = retry_lines

                        if self._is_fastsurfer_user_guard(output_lines):
                            yield (
                                "[red]FastSurfer launch failed because Docker user mapping was not "
                                "applied. Restart with `docker compose up --build orchestrator` "
                                "and confirm `FASTSURFER_DOCKER_USER=0:0` in the startup log.[/red]"
                            )
                            raise RuntimeError("FastSurfer docker user mapping failed")

                    if self.fastsurfer_use_gpu:
                        yield "[yellow]FastSurfer GPU run failed; retrying once on CPU fallback...[/yellow]"
                        cpu_cmd = self._build_command(subject_id, stage, force_cpu=True)
                        if not cpu_cmd:
                            raise RuntimeError(f"Exit code {returncode}")

                        cpu_proc = await asyncio.create_subprocess_exec(
                            *cpu_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT,
                        )
                        assert cpu_proc.stdout is not None
                        async for raw in cpu_proc.stdout:
                            yield raw.decode("utf-8", errors="replace").rstrip()

                        await cpu_proc.wait()
                        cpu_code = cpu_proc.returncode
                        if cpu_code != 0:
                            raise RuntimeError(f"Exit code {cpu_code}")
                        return

                raise RuntimeError(f"Exit code {returncode}")

        except FileNotFoundError:
            yield "[red]'docker' not found — is /var/run/docker.sock mounted?[/red]"
            raise

    # ── Command builders ───────────────────────────────────────────────────────

    def _build_command(
        self,
        subject_id: str,
        stage: str,
        force_cpu: bool = False,
        docker_user: Optional[str] = None,
    ) -> Optional[List[str]]:
        docker_prefix = self._docker_run_prefix()
        data_mount = self._host_bind("data")
        outputs_mount = self._host_bind("outputs")
        licenses_mount = self._host_bind("licenses")

        if stage == "mriqc":
            mriqc_out = "/outputs/mriqc" if self._use_volumes_from() else "/out"
            cmd = [*docker_prefix]
            if not self._use_volumes_from():
                cmd.extend([
                    "-v", f"{data_mount}:/data:ro",
                    "-v", f"{outputs_mount}/mriqc:/out",
                ])
            cmd.extend([
                "nipreps/mriqc:latest",
                "/data", mriqc_out, "participant",
                "--participant-label", subject_id.replace("sub-", ""),
                "--no-sub",
                "--nprocs", self.mriqc_nprocs,
                "--omp-nthreads", self.mriqc_omp_nthreads,
            ])
            return cmd

        if stage == "fastsurfer":
            t1 = self._find_bids_file(subject_id, "anat", "_T1w.nii.gz")
            if not t1:
                return None
            fastsurfer_out = "/outputs/fastsurfer" if self._use_volumes_from() else "/output"
            cmd = [*docker_prefix]
            effective_user = docker_user or self.fastsurfer_docker_user
            cmd.extend(["-u", effective_user])
            use_gpu = self.fastsurfer_use_gpu and not force_cpu
            if use_gpu:
                cmd.extend(["--gpus", self.fastsurfer_gpu_device])
            if not self._use_volumes_from():
                cmd.extend([
                    "-v", f"{data_mount}:/data:ro",
                    "-v", f"{outputs_mount}/fastsurfer:/output",
                    "-v", f"{licenses_mount}:/licenses:ro",
                ])
            cmd.extend([
                "deepmi/fastsurfer:latest",
                "--t1", f"/data/{subject_id}/anat/{t1}",
                "--sid", subject_id,
                "--sd", fastsurfer_out,
                "--fs_license", "/licenses/license.txt",
                "--seg_only",   # surface recon is slow; add --surf_only for full run
            ])
            if not use_gpu:
                # Force CPU inference so the FastSurfer CNN doesn't fall back to
                # CUDA inside the container even when no --gpus flag is passed.
                cmd.append("--device")
                cmd.append("cpu")
            if effective_user.startswith("0:") or effective_user == "0":
                cmd.append("--allow_root")
            return cmd

        if stage == "fmriprep":
            fmriprep_out = "/outputs/fmriprep" if self._use_volumes_from() else "/out"
            cmd = [*docker_prefix]
            if not self._use_volumes_from():
                cmd.extend([
                    "-v", f"{data_mount}:/data:ro",
                    "-v", f"{outputs_mount}/fmriprep:/out",
                    "-v", f"{licenses_mount}:/licenses:ro",
                ])
            cmd.extend([
                "nipreps/fmriprep:latest",
                "/data", fmriprep_out, "participant",
                "--participant-label", subject_id.replace("sub-", ""),
                "--fs-license-file", "/licenses/license.txt",
                "--output-spaces", "MNI152NLin2009cAsym:res-2",
                "--skip-bids-validation",
            ])
            return cmd

        if stage == "mrtrix3":
            mrtrix_out = "/outputs/mrtrix3" if self._use_volumes_from() else "/output"
            cmd = [*docker_prefix]
            if not self._use_volumes_from():
                cmd.extend([
                    "-v", f"{data_mount}:/data:ro",
                    "-v", f"{outputs_mount}/mrtrix3:/output",
                ])
            cmd.extend([
                "mrtrix3/mrtrix3:latest",
                "/bin/bash", "-c",
                self._mrtrix3_script(subject_id, mrtrix_out),
            ])
            return cmd

        # Python-based stages run inside the orchestrator container directly
        if stage == "connectivity":
            cmd = [
                "python", "-m", "pipeline.tasks.connectivity",
                "--subject", subject_id,
                "--fmriprep-dir", str(self.output_dir / "fmriprep"),
                "--output-dir",  str(self.output_dir / "connectivity"),
            ]
            # Resolve inputs by role (decoupled from the preprocessing tool used).
            # Reload so we see artifacts registered by the just-finished fMRIPrep.
            self.manifest.load()
            bold = self.manifest.resolve_path(subject_id, "preproc_bold")
            confounds = self.manifest.resolve_path(subject_id, "confounds")
            if bold and bold.is_file():
                cmd += ["--bold", str(bold)]
            if confounds and confounds.is_file():
                cmd += ["--confounds", str(confounds)]
            return cmd

        if stage == "network":
            return [
                "python", "-m", "pipeline.tasks.network",
                "--subject", subject_id,
                "--connectivity-dir", str(self.output_dir / "connectivity"),
                "--tractography-dir", str(self.output_dir / "mrtrix3"),
                "--output-dir",       str(self.output_dir / "network"),
            ]

        if stage == "mask":
            # Auto path: build the baseline mask from the segmentation and export an STL.
            # masks/ and stl/ are written under the top-level output dir (where the web
            # editor reads versions from), so --output-dir is the outputs root itself.
            return [
                "python", "-m", "pipeline.tasks.mask_stl",
                "--subject", subject_id,
                "--fastsurfer-dir", str(self.output_dir / "fastsurfer"),
                "--output-dir",     str(self.output_dir),
                "--selection",      "whole",
                "--stl-preset",     "standard",
                "--export-stl",
            ]

        return None

    def _resolve_host_root(self) -> Tuple[str, str]:
        env_value = self.host_root_env
        if env_value and "$" not in env_value and "{" not in env_value:
            return env_value.rstrip("/\\"), "HOST_PROJECT_DIR"

        detected = self._detect_host_root_from_docker()
        if detected:
            return detected.rstrip("/\\"), "docker-inspect"

        return str(Path.cwd()).rstrip("/\\"), "cwd-fallback"

    def _detect_host_root_from_docker(self) -> Optional[str]:
        container_id = os.getenv("HOSTNAME", "").strip()
        if not container_id:
            return None
        try:
            inspect = subprocess.run(
                ["docker", "inspect", container_id],
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:
            return None

        try:
            info = json.loads(inspect.stdout)
            mounts = info[0].get("Mounts", [])
        except Exception:
            return None

        for mount in mounts:
            if mount.get("Destination") == "/workspace":
                source = mount.get("Source")
                if source:
                    return str(source)
        return None

    def _host_bind(self, *parts: str) -> str:
        suffix = "/".join(part.strip("/\\") for part in parts if part.strip("/\\"))
        if not suffix:
            return self.host_root
        return f"{self.host_root}/{suffix}"

    def _use_volumes_from(self) -> bool:
        return bool(self.container_ref)

    def _docker_run_prefix(self) -> List[str]:
        cmd = ["docker", "run", "--rm"]
        if self._use_volumes_from():
            cmd.extend(["--volumes-from", self.container_ref])
        return cmd

    def _is_fastsurfer_user_guard(self, output_lines: List[str]) -> bool:
        """True only when FastSurfer blocks execution due to the nonroot/user guard —
        NOT when it only prints the advisory root warning and then continues."""
        joined = "\n".join(output_lines)
        if "run_fastsurfer.sh" not in joined:
            return False
        # The blocking guard (nonroot.sh) prints "default FastSurfer docker user" + "nonroot".
        # The advisory warning "trying to run … as root" is printed even on successful runs;
        # do NOT treat it alone as a hard block.
        return "default FastSurfer docker user" in joined and "nonroot" in joined

    async def _mriqc_preflight(self, subject_id: str) -> Tuple[bool, str]:
        expected_desc = self._host_bind("data", "dataset_description.json")
        expected_sub = self._host_bind("data", subject_id)
        if self._use_volumes_from():
            if (self.data_dir / "dataset_description.json").is_file() and (self.data_dir / subject_id).is_dir():
                return self._validate_subject_nifti_inputs(subject_id)
            return (
                False,
                " ".join([
                    "[red]MRIQC preflight failed: dataset mount is missing required BIDS files.[/red]",
                    f"Expected in container: {self.data_dir / 'dataset_description.json'} and {self.data_dir / subject_id}.",
                    "Check that compose mounts ./data to /data and launch from the project directory.",
                ]),
            )

        data_mount = self._host_bind("data")
        check_code = (
            "import os,sys; "
            "ok=os.path.isfile('/data/dataset_description.json') and os.path.isdir('/data/" + subject_id + "'); "
            "sys.exit(0 if ok else 1)"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "run", "--rm",
                "-v", f"{data_mount}:/data:ro",
                "--entrypoint", "python",
                "nipreps/mriqc:latest",
                "-c", check_code,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except FileNotFoundError:
            return False, "[red]Docker CLI is unavailable for MRIQC preflight.[/red]"

        if proc.returncode == 0:
            return self._validate_subject_nifti_inputs(subject_id)

        return (
            False,
            " ".join([
                "[red]MRIQC preflight failed: dataset mount is missing required BIDS files.[/red]",
                f"Expected on host: {expected_desc} and {expected_sub}.",
                f"Resolved HOST_PROJECT_DIR ({self.host_root_source}): {self.host_root}.",
                "Check docker compose launch directory and HOST_PROJECT_DIR configuration.",
            ]),
        )

    def _validate_subject_nifti_inputs(self, subject_id: str) -> Tuple[bool, str]:
        nifti_files = self._collect_subject_niftis(subject_id)
        if not nifti_files:
            return (
                False,
                " ".join([
                    "[red]MRIQC preflight failed: no NIfTI inputs found for subject.[/red]",
                    f"Checked: {self.data_dir / subject_id / 'anat'} and {self.data_dir / subject_id / 'func'}.",
                    "Ensure BIDS files are present before running MRIQC.",
                ]),
            )

        for nifti_path in nifti_files:
            error = self._validate_nifti_readable(nifti_path)
            if error:
                return (
                    False,
                    " ".join([
                        "[red]MRIQC preflight failed: unreadable or truncated NIfTI input.[/red]",
                        f"Subject: {subject_id}.",
                        f"File: {nifti_path}.",
                        f"Details: {error}.",
                        "Replace or re-download this file, then rerun the pipeline.",
                    ]),
                )

        return True, ""

    def _collect_subject_niftis(self, subject_id: str) -> List[Path]:
        subject_root = self.data_dir / subject_id
        paths: List[Path] = []
        for modality in ("anat", "func"):
            mod_dir = subject_root / modality
            if not mod_dir.is_dir():
                continue
            for candidate in sorted(mod_dir.iterdir()):
                if not candidate.is_file():
                    continue
                if candidate.name.endswith(".nii") or candidate.name.endswith(".nii.gz"):
                    paths.append(candidate)
        return paths

    def _fastsurfer_preflight(self, subject_id: str) -> Tuple[bool, str]:
        if not self.fs_license.is_file():
            return (
                False,
                " ".join([
                    "[red]FastSurfer preflight failed: FreeSurfer license file is missing.[/red]",
                    f"Expected: {self.fs_license}",
                    "Mount ./licenses/license.txt to /licenses/license.txt and rerun.",
                ]),
            )

        t1 = self._find_bids_file(subject_id, "anat", "_T1w.nii.gz")
        if not t1:
            return (
                False,
                " ".join([
                    "[red]FastSurfer preflight failed: no T1w input found.[/red]",
                    f"Expected under: {self.data_dir / subject_id / 'anat'}",
                    "Ensure a file ending with _T1w.nii.gz exists.",
                ]),
            )

        return True, ""

    def _fmriprep_preflight(self, subject_id: str) -> Tuple[bool, str]:
        if not self.fs_license.is_file():
            return (
                False,
                " ".join([
                    "[red]fMRIPrep preflight failed: FreeSurfer license file is missing.[/red]",
                    f"Expected: {self.fs_license}",
                    "Mount ./licenses/license.txt to /licenses/license.txt and rerun.",
                ]),
            )

        subject_root = self.data_dir / subject_id
        if not subject_root.is_dir():
            return (
                False,
                " ".join([
                    "[red]fMRIPrep preflight failed: subject folder is missing.[/red]",
                    f"Expected: {subject_root}",
                ]),
            )

        t1 = self._find_bids_file(subject_id, "anat", "_T1w.nii.gz")
        if not t1:
            return (
                False,
                " ".join([
                    "[red]fMRIPrep preflight failed: no T1w input found.[/red]",
                    f"Expected under: {self.data_dir / subject_id / 'anat'}",
                    "Ensure a file ending with _T1w.nii.gz exists.",
                ]),
            )

        func_dir = self.data_dir / subject_id / "func"
        has_func = False
        if func_dir.is_dir():
            for f in func_dir.iterdir():
                if f.is_file() and (f.name.endswith("_bold.nii.gz") or f.name.endswith("_bold.nii")):
                    has_func = True
                    break
        if not has_func:
            return (
                False,
                " ".join([
                    "[red]fMRIPrep preflight failed: no BOLD functional inputs found.[/red]",
                    f"Expected under: {func_dir}",
                    "Ensure at least one file ending with _bold.nii.gz (or _bold.nii) exists.",
                ]),
            )

        return True, ""

    def _validate_nifti_readable(self, nifti_path: Path) -> Optional[str]:
        try:
            img = nib.load(str(nifti_path))
            if img.ndim >= 4:
                volume_count = img.shape[3]
                if volume_count < 1:
                    return "4D NIfTI contains zero volumes"
                probe_count = min(50, volume_count)
                window_specs = [(0, probe_count)]
                if volume_count > probe_count:
                    middle_start = max(0, (volume_count // 2) - (probe_count // 2))
                    middle_end = min(volume_count, middle_start + probe_count)
                    window_specs.append((middle_start, middle_end))
                    end_start = max(0, volume_count - probe_count)
                    window_specs.append((end_start, volume_count))

                for start, end in window_specs:
                    _ = img.dataobj[:, :, :, start:end]
            else:
                origin = tuple(0 for _ in range(img.ndim))
                _ = img.dataobj[origin]
        except Exception as exc:
            return str(exc)

        return None

    def _find_bids_file(self, subject_id: str, modality: str, suffix: str) -> Optional[str]:
        sub_mod = self.data_dir / subject_id / modality
        if not sub_mod.exists():
            return None
        for f in sub_mod.iterdir():
            if f.name.endswith(suffix):
                return f.name
        return None

    def _mrtrix3_script(self, subject_id: str, output_root: str = "/output") -> str:
        return f"""set -e
cd {output_root} && mkdir -p {subject_id}
dwifslpreproc /data/{subject_id}/dwi/*_dwi.nii.gz \\
  {subject_id}/dwi_preproc.mif -rpe_none -pe_dir AP 2>&1
dwi2response dhollander {subject_id}/dwi_preproc.mif \\
  {subject_id}/wm.txt {subject_id}/gm.txt {subject_id}/csf.txt 2>&1
dwi2fod msmt_csd {subject_id}/dwi_preproc.mif \\
  {subject_id}/wm.txt  {subject_id}/fod_wm.mif \\
  {subject_id}/gm.txt  {subject_id}/fod_gm.mif \\
  {subject_id}/csf.txt {subject_id}/fod_csf.mif 2>&1
tckgen {subject_id}/fod_wm.mif {subject_id}/tracks.tck \\
  -seed_image {subject_id}/fod_wm.mif -select 100000 2>&1
echo "tractography complete for {subject_id}"
"""

    # ── Mock mode ──────────────────────────────────────────────────────────────

    async def _mock_stage(
        self, subject_id: str, stage: str
    ) -> AsyncIterator[str]:
        steps = {
            "mriqc":        ["Initialising MRIQC", "Computing IQMs", "Generating reports"],
            "fastsurfer":   ["Loading CNN model", "Coronal pass", "Axial pass", "Sagittal pass", "Writing segmentation"],
            "fmriprep":     ["Brain extraction (BET)", "T1 registration (ANTs)", "Confound estimation", "Cleaning up"],
            "mrtrix3":      ["DWI denoising", "Preprocessing", "Response function", "FOD estimation", "Tractography (100k streamlines)"],
            "connectivity": ["Loading fMRI", "Parcellation (Schaefer 200)", "Timeseries extraction", "Computing FC matrix"],
            "mask":         ["Loading segmentation", "Building baseline mask", "Saving mask version", "Meshing (marching cubes)", "Exporting STL"],
            "network":      ["Building graph", "Clustering coefficients", "Global efficiency", "Hub detection", "Saving metrics"],
        }
        for step in steps.get(stage, ["Processing..."]):
            await asyncio.sleep(random.uniform(0.4, 1.2))
            yield step
        await asyncio.sleep(0.3)
