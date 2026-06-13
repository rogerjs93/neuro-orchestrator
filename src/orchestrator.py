"""
neuro-orchestrator · TUI pipeline dashboard
Built with Python Textual (future Rust/tuie migration target for v1.0)
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button, DataTable, Footer, Header, Label, RichLog, Static,
)

from pipeline.runner import PipelineRunner
from pipeline.persistence import SCHEMA_VERSION, load_checkpoint, save_checkpoint
from pipeline.state import PipelineState, StageStatus, STAGE_ORDER
from pipeline.manifest import ArtifactManifest, ensure_dataset_description
from pipeline.adapters import register_stage_outputs
from utils.bids import scan_bids_dataset

# ── Paths from env ─────────────────────────────────────────────────────────────
DATA_DIR   = Path(os.getenv("DATA_DIR",   "/data"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/outputs"))
FS_LICENSE = Path(os.getenv("FS_LICENSE", "/licenses/license.txt"))
MOCK       = os.getenv("MOCK_MODE", "0") == "1"
MAX_LOG_BUFFER = 500
LOG_CHECKPOINT_INTERVAL_SECONDS = 5.0
LOG_CHECKPOINT_BATCH_SIZE = 20

# ── Status display ──────────────────────────────────────────────────────────────
ICONS = {
    StageStatus.PENDING:   ("○", "dim"),
    StageStatus.RUNNING:   ("⟳", "yellow bold"),
    StageStatus.COMPLETED: ("✓", "green"),
    StageStatus.FAILED:    ("✗", "red bold"),
    StageStatus.SKIPPED:   ("—", "dim"),
}


class NeuroPipeline(App):
    """Main TUI application."""

    TITLE = "neuro-orchestrator"
    SUB_TITLE = "neuroimaging pipeline dashboard"
    DARK = True

    CSS = """
    Screen { background: $background; }

    #layout {
        height: 1fr;
        layout: horizontal;
    }

    /* ── Subject panel ── */
    #left {
        width: 38;
        border-right: tall $panel-darken-3;
        padding: 0 1;
    }

    .panel-label {
        color: $text-muted;
        text-style: bold;
        padding: 0 0 0 0;
    }

    #subject-table {
        height: 1fr;
        scrollbar-gutter: stable;
    }

    #subject-table > .datatable--cursor {
        background: $accent 30%;
    }

    #btn-row {
        height: 5;
        layout: horizontal;
        padding: 1 0 0 0;
    }

    #btn-row Button { margin-right: 1; min-width: 10; }

    /* ── Log panel ── */
    #right {
        padding: 0 1;
    }

    #log-title {
        height: 1;
        color: $text-muted;
        padding: 0 0 0 0;
    }

    RichLog {
        height: 1fr;
        border: none;
        scrollbar-gutter: stable;
        padding: 0;
    }

    /* ── Bottom status ── */
    #status-bar {
        height: 1;
        background: $panel;
        padding: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q",     "quit",         "Quit",         show=True),
        Binding("r",     "run_selected",  "Run selected", show=True),
        Binding("a",     "run_all",       "Run all",      show=True),
        Binding("c",     "clear_log",     "Clear log",    show=True),
        Binding("escape","reset_selected","Reset",        show=False),
    ]

    selected_subject: reactive[Optional[str]] = reactive(None)

    # ── Init ────────────────────────────────────────────────────────────────────

    def __init__(self) -> None:
        super().__init__()
        self.state = PipelineState()
        self.runner = PipelineRunner(DATA_DIR, OUTPUT_DIR, FS_LICENSE, mock=MOCK)
        self.manifest = ArtifactManifest(OUTPUT_DIR / "derivatives")
        ensure_dataset_description(OUTPUT_DIR / "derivatives")
        self.log_history: List[Dict[str, Any]] = []
        self._pending_log_events = 0
        self._last_checkpoint_ts = 0.0
        self._bootstrap_from_checkpoint()

    def _scan_subject_modalities(self) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {}
        for sub in scan_bids_dataset(DATA_DIR):
            out[sub.id] = set(sub.modalities)
        return out

    def _checkpoint_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "saved_at": datetime.utcnow().isoformat() + "Z",
            "state": self.state.to_dict(),
            "logs": self.log_history[-MAX_LOG_BUFFER:],
        }

    def _save_checkpoint(self) -> None:
        save_checkpoint(OUTPUT_DIR, self._checkpoint_payload())
        self._pending_log_events = 0
        self._last_checkpoint_ts = time.time()

    def _maybe_flush_logs(self, force: bool = False) -> None:
        if force:
            self._save_checkpoint()
            return
        now = time.time()
        if (
            self._pending_log_events >= LOG_CHECKPOINT_BATCH_SIZE
            or (now - self._last_checkpoint_ts) >= LOG_CHECKPOINT_INTERVAL_SECONDS
        ):
            self._save_checkpoint()

    def _bootstrap_from_checkpoint(self) -> None:
        checkpoint = load_checkpoint(OUTPUT_DIR)
        restored_subjects = 0
        restored_logs = 0

        if checkpoint and isinstance(checkpoint.get("state"), dict):
            self.state = PipelineState.from_dict(checkpoint["state"])
            restored_subjects = len(self.state.subjects)

        if checkpoint and isinstance(checkpoint.get("logs"), list):
            self.log_history = [entry for entry in checkpoint["logs"] if isinstance(entry, dict)][-MAX_LOG_BUFFER:]
            restored_logs = len(self.log_history)

        self.state.reconcile_with_scan(self._scan_subject_modalities())

        interrupted = self.state.mark_interrupted_running_as_failed()
        for subject_id, stage in interrupted:
            self.log_history.append({
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "subject_id": subject_id,
                "message": f"[resume] Previous run interrupted during {stage}; restored as failed.",
                "style": "red",
            })

        if len(self.log_history) > MAX_LOG_BUFFER:
            self.log_history = self.log_history[-MAX_LOG_BUFFER:]

        self._save_checkpoint()
        self._status_resume = (
            f"restored_subjects={restored_subjects} active_subjects={len(self.state.subjects)} "
            f"interrupted_to_failed={len(interrupted)} restored_logs={restored_logs}"
        )

    # ── Layout ──────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            with Vertical(id="left"):
                yield Label("Subjects", classes="panel-label")
                yield DataTable(id="subject-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="btn-row"):
                    yield Button("Run all", id="btn-all",  variant="primary")
                    yield Button("Run",     id="btn-one",  variant="default")
                    yield Button("Reset",   id="btn-reset",variant="warning")
            with Vertical(id="right"):
                yield Label("Select a subject to see logs", id="log-title")
                yield RichLog(id="log-output", highlight=True, markup=True, max_lines=2000)
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#subject-table", DataTable)
        table.add_columns("Subject", "St", "Stage")
        self._refresh_table()

        for entry in self.log_history[-200:]:
            msg = str(entry.get("message", ""))
            style = str(entry.get("style", ""))
            subject = str(entry.get("subject_id", "system"))
            self._log(subject, msg, style=style, persist=False)

        self._log("system", f"[resume] {self._status_resume}", "dim", persist=False)

        if MOCK:
            self._status(f"[yellow]MOCK MODE[/yellow] — simulated runs, no real tools needed")
        elif not DATA_DIR.exists() or not list(DATA_DIR.glob("sub-*")):
            self._status("No subjects found — place BIDS data in ./data/")
        else:
            n = len(self.state.subjects)
            self._status(f"Loaded {n} subject{'s' if n != 1 else ''} from {DATA_DIR}")

    # ── Table management ────────────────────────────────────────────────────────

    def _refresh_table(self) -> None:
        table = self.query_one("#subject-table", DataTable)
        table.clear()
        for sid, sub in self.state.subjects.items():
            icon, style = ICONS[sub.overall_status]
            done, total = sub.progress
            stage_label = sub.current_stage or ("done" if done == total and total > 0 else "—")
            table.add_row(
                sid,
                Text(icon, style=style),
                stage_label,
                key=sid,
            )

    # ── Logging helpers ─────────────────────────────────────────────────────────

    def _log(self, subject_id: str, message: str, style: str = "", persist: bool = True) -> None:
        log = self.query_one("#log-output", RichLog)
        ts  = datetime.now().strftime("%H:%M:%S")
        pfx = f"[dim]{ts}[/dim] "
        if style:
            log.write(f"{pfx}[{style}]{escape(message)}[/{style}]")
        else:
            log.write(f"{pfx}{escape(message)}")

        if persist:
            self.log_history.append({
                "timestamp": ts,
                "subject_id": subject_id,
                "message": message,
                "style": style,
            })
            if len(self.log_history) > MAX_LOG_BUFFER:
                self.log_history = self.log_history[-MAX_LOG_BUFFER:]
            self._pending_log_events += 1
            self._maybe_flush_logs()

    def _status(self, msg: str) -> None:
        self.query_one("#status-bar", Static).update(msg)

    # ── Events ──────────────────────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#subject-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        self.selected_subject = str(event.row_key.value)
        sub = self.state.subjects.get(self.selected_subject)
        if sub:
            done, total = sub.progress
            mods = ", ".join(sorted(sub.modalities)) or "none"
            self.query_one("#log-title", Label).update(
                f"{self.selected_subject}  ·  modalities: {mods}  ·  {done}/{total} stages"
            )

    @on(Button.Pressed, "#btn-all")
    def action_run_all(self) -> None:
        for sid in self.state.subjects:
            self._start_subject(sid)

    @on(Button.Pressed, "#btn-one")
    def action_run_selected(self) -> None:
        if self.selected_subject:
            self._start_subject(self.selected_subject)
        else:
            self._status("Select a subject first")

    @on(Button.Pressed, "#btn-reset")
    def on_reset(self) -> None:
        self.state.reset_all()
        self.log_history.clear()
        self._pending_log_events = 0
        self._refresh_table()
        self.query_one("#log-output", RichLog).clear()
        self._status("Pipeline reset")
        self._save_checkpoint()

    def action_clear_log(self) -> None:
        self.query_one("#log-output", RichLog).clear()

    # ── Pipeline execution ──────────────────────────────────────────────────────

    @work(exclusive=False, thread=False)
    async def _start_subject(self, subject_id: str) -> None:
        sub = self.state.subjects.get(subject_id)
        if not sub or sub.overall_status == StageStatus.RUNNING:
            return

        stages = self.runner.pending_stages(sub)
        if not stages:
            self._log(subject_id, "Nothing to run — all stages complete or skipped", "dim")
            return

        self._log(subject_id, f"Starting pipeline: {' → '.join(stages)}", "cyan")

        for stage in stages:
            self.state.set_running(subject_id, stage)
            self._save_checkpoint()
            self._refresh_table()
            self._status(f"Running {subject_id} · {stage} …")
            self._log(subject_id, f"─── {stage.upper()} ───", "bold cyan")

            failed = False
            try:
                async for line in self.runner.run_stage(subject_id, stage):
                    self._log(subject_id, line)
            except Exception as exc:
                self._log(subject_id, str(exc), "red")
                failed = True

            if failed:
                self.state.set_failed(subject_id, stage)
                self._save_checkpoint()
                self._log(subject_id, f"✗ {stage} failed", "red bold")
                self._refresh_table()
                break

            valid, validation_error = self.runner.validate_stage_outputs(subject_id, stage)
            if not valid:
                self.state.set_failed(subject_id, stage)
                self._save_checkpoint()
                self._log(subject_id, validation_error, "red")
                self._log(subject_id, f"✗ {stage} failed validation", "red bold")
                self._refresh_table()
                break

            self.state.set_completed(subject_id, stage)
            try:
                roles = register_stage_outputs(self.manifest, subject=subject_id, stage=stage, output_dir=OUTPUT_DIR)
            except Exception:
                roles = []
            self._save_checkpoint()
            self._log(subject_id, f"✓ {stage} complete", "green")
            if roles:
                self._log(subject_id, f"[manifest] registered: {', '.join(roles)}", "dim")
            self._refresh_table()

        # Final status
        sub = self.state.subjects[subject_id]
        done, total = sub.progress
        self._status(f"{subject_id} finished — {done}/{total} stages complete")

    async def on_shutdown(self) -> None:
        interrupted = self.state.mark_interrupted_running_as_failed()
        for subject_id, stage in interrupted:
            self.log_history.append({
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "subject_id": subject_id,
                "message": f"[resume] Shutdown interrupted {stage}; restored as failed.",
                "style": "red",
            })
        if len(self.log_history) > MAX_LOG_BUFFER:
            self.log_history = self.log_history[-MAX_LOG_BUFFER:]
        self._save_checkpoint()


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = NeuroPipeline()
    app.run()
