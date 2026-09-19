#!/usr/bin/env python3
"""FurTag desktop GUI — thin PySide6 adapter over the furtag.py engine.

Run:  python furtag_gui.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ensure project root is importable when launched as a script.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import (
    QObject, QThread, Qt, QTimer, Signal, Slot, QUrl, QMimeData, QSize,
)
from PySide6.QtGui import (
    QAction, QDesktopServices, QDragEnterEvent, QDropEvent, QFont, QKeySequence,
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QProgressBar, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget, QInputDialog, QFrame, QSizePolicy,
    QStackedWidget,
)


def _available_screen_rect():
    """Usable desktop area for the primary screen (excludes menu bar / dock)."""
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return None
    return screen.availableGeometry()


def _fit_window_to_screen(window: QWidget, prefer_w: int = 900, prefer_h: int = 640,
                          *, recenter: bool = True) -> None:
    """Size (and optionally place) a window so it fits on-screen and stays resizable."""
    geo = _available_screen_rect()
    if geo is None:
        window.resize(prefer_w, prefer_h)
        return
    # Leave a margin so the frame/title bar never clips off-screen / under the dock.
    margin = 64
    max_w = max(560, geo.width() - margin)
    max_h = max(420, geo.height() - margin)
    w = min(prefer_w, max_w)
    h = min(prefer_h, max_h)
    # If the window somehow grew past the screen, shrink it hard.
    if window.width() > max_w or window.height() > max_h:
        w = min(w, max_w)
        h = min(h, max_h)
    window.setMinimumSize(560, 420)
    # No maximumSize — user can expand to full screen; only the *initial* size is capped.
    window.resize(w, h)
    if recenter:
        x = geo.x() + max(0, (geo.width() - w) // 2)
        y = geo.y() + max(0, (geo.height() - h) // 2)
        window.move(x, y)
    else:
        # Keep top-left on-screen if it drifted
        frame = window.frameGeometry()
        x = min(max(frame.x(), geo.x()), geo.x() + geo.width() - frame.width())
        y = min(max(frame.y(), geo.y()), geo.y() + geo.height() - frame.height())
        window.move(x, y)


class _TypeableSpinBox(QSpinBox):
    """A spin box whose special-value text does not block typing.

    With text such as "no limit" showing, a click lands the caret in the
    middle of the words and every typed digit is rejected as invalid. Taking
    the whole text on focus means the first keystroke replaces it instead.
    """

    def focusInEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        had_focus = self.hasFocus()
        super().mousePressEvent(event)
        if not had_focus and self.specialValueText() and self.value() == self.minimum():
            self.selectAll()


def _wrap_scroll(widget: QWidget) -> QScrollArea:
    """Put *widget* in a scroll area that can shrink below its contents."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setWidget(widget)
    scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return scroll


class PdfMetaDialog(QDialog):
    """Per-PDF comic name + optional artist before page rendering."""

    def __init__(self, parent: Optional[QWidget], pdfs: List[Path]) -> None:
        super().__init__(parent)
        self.setWindowTitle("PDF comic tags")
        self._pdfs = list(pdfs)
        self._rows: List[Tuple[Path, QLineEdit, QLineEdit]] = []

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "Set comic: and optional creator: tags applied to every page "
            "when each PDF is rendered.\n"
            "Comic defaults to the PDF filename; leave artist blank to skip."))

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        for pdf in self._pdfs:
            comic_edit = QLineEdit(pdf.stem)
            creator_edit = QLineEdit()
            creator_edit.setPlaceholderText("optional")
            pair = QWidget()
            pair_lay = QHBoxLayout(pair)
            pair_lay.setContentsMargins(0, 0, 0, 0)
            pair_lay.addWidget(QLabel("comic:"))
            pair_lay.addWidget(comic_edit, 2)
            pair_lay.addWidget(QLabel("creator:"))
            pair_lay.addWidget(creator_edit, 2)
            form.addRow(QLabel(pdf.name), pair)
            self._rows.append((pdf, comic_edit, creator_edit))

        root.addWidget(_wrap_scroll(form_host), 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.resize(640, min(480, 160 + 48 * max(1, len(self._pdfs))))

    def meta_map(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        for pdf, comic_edit, creator_edit in self._rows:
            out[str(pdf.resolve())] = _normalize_pdf_meta(
                comic_edit.text(), creator_edit.text(), pdf.stem)
        return out

from furtag import (
    TagIntegrator, prompt_for_pdf_dpi, _nuke_candidates, _pdf_render_candidates,
    _is_furtag_sidecar, LEDGER_FILE, DUPLICATES_FILE,
    is_filesystem_root, perform_nuke, set_active_observer,
    _normalize_pdf_meta, notify_info,
)
from furtag_settings import (
    Settings, SettingsStore, RunOptions, ScanSummary, HydrusScanSettings,
    normalize_hydrus_scan, validate_run_preflight,
    validate_output_patterns, SidecarPatternError, PACE_FLOORS,
    FLUFFLE_MATCH_CLASSES, FLUFFLE_REVIEW_MODES, DEFAULT_PDF_DPI,
    DEFAULT_PDF_ARCHIVAL_DPI, remember_scan_path,
)
from furtag_credentials import (
    CredentialStore, ALL_FIELDS, SECRET_FIELDS, FIELD_MAP,
    keyring_disabled, env_file_path,
)
from furtag_events import RunEvent, RunObserver
from furtag_review import ReviewQueue, PendingReview


# ── Qt signal bridge ─────────────────────────────────────────────────────────

class QtEventBridge(QObject):
    """Thread-safe event bridge: engine threads → Qt main thread."""
    event = Signal(object)  # RunEvent
    finished = Signal(object)  # ScanSummary
    failed = Signal(str)
    log_line = Signal(str)
    inventory_ready = Signal(object)
    inventory_failed = Signal(object)
    review_changed = Signal(int)


class QtObserver:
    def __init__(self, bridge: QtEventBridge) -> None:
        self.bridge = bridge

    def emit(self, event: RunEvent) -> None:
        self.bridge.event.emit(event)


class ScanWorker(QThread):
    def __init__(self, integrator: TagIntegrator, root: Path,
                 options: RunOptions, cancel_event: threading.Event,
                 bridge: QtEventBridge) -> None:
        super().__init__()
        self.integrator = integrator
        self.root = root
        self.options = options
        self.cancel_event = cancel_event
        self.bridge = bridge

    def run(self) -> None:
        try:
            observer = QtObserver(self.bridge)
            summary = self.integrator.run(
                self.root,
                options=self.options,
                observer=observer,
                cancel_event=self.cancel_event,
                use_terminal_display=False,
            )
            self.bridge.finished.emit(summary)
        except Exception as e:
            self.bridge.failed.emit(str(e))


class HydrusScanWorker(QThread):
    """Runs one database scan off the UI thread, like ScanWorker for folders."""

    def __init__(self, integrator: TagIntegrator, scan: HydrusScanSettings,
                 cancel_event: threading.Event, bridge: QtEventBridge) -> None:
        super().__init__()
        self.integrator = integrator
        self.scan = scan
        self.cancel_event = cancel_event
        self.bridge = bridge

    def run(self) -> None:
        try:
            summary = self.integrator.scan_hydrus_db(
                self.scan,
                observer=QtObserver(self.bridge),
                cancel_event=self.cancel_event,
                use_terminal_display=False,
            )
            self.bridge.finished.emit(summary)
        except Exception as e:
            self.bridge.failed.emit(str(e))


class HydrusScanPanel(QWidget):
    """Selection and budget for a Hydrus database scan.

    Lives on the Scan tab rather than in Settings because it is what you decide
    immediately before pressing Start — the counterpart of choosing a folder.
    The values still persist, so the panel reopens on the last scan's settings.
    """

    changed = Signal()

    def __init__(self, scan: HydrusScanSettings, parent=None) -> None:
        super().__init__(parent)
        self._services: List[Tuple[str, str]] = []
        self._tag_services: List[Tuple[str, str]] = []
        self._dupes = (True, "on_miss", 8)
        self._build()
        self.load_from(scan)

    def _build(self) -> None:
        # Three columns side by side, not one tall stack: the window is wider
        # than it is tall, and a form that shows whole without scrolling is
        # the whole point of giving the scan its own tab.
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        left, mid, right = QVBoxLayout(), QVBoxLayout(), QVBoxLayout()
        for column, stretch in ((left, 4), (mid, 3), (right, 3)):
            column.setSpacing(6)
            root.addLayout(column, stretch=stretch)

        # ── What to scan ────────────────────────────────────────────────────
        what = QGroupBox("Which files")
        wf = QFormLayout(what)
        wf.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        domain_row = QHBoxLayout()
        self.domain = QComboBox()
        self.domain.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.domain.setMinimumContentsLength(24)
        # A hard floor, since a combo's minimum is otherwise tiny and this is
        # the first thing to get squeezed when three columns share a window.
        self.domain.setMinimumWidth(150)
        self.domain.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.refresh_services_btn = QPushButton("↻")
        self.refresh_services_btn.setFixedWidth(30)
        self.refresh_services_btn.setToolTip(
            "Refresh: ask Hydrus which file domains and tag services exist. "
            "Needs a working connection.")
        domain_row.addWidget(self.domain, stretch=1)
        domain_row.addWidget(self.refresh_services_btn)
        wf.addRow("File domain", domain_row)
        self.domain_help = QLabel()
        self.domain_help.setWordWrap(True)
        wf.addRow("", self.domain_help)

        self.max_tags = _TypeableSpinBox()
        self.max_tags.setRange(0, 100_000)
        self.max_tags.setSpecialValueText("no limit")
        self.max_tags.setToolTip(
            "Only look at files with fewer than this many tags — the usual way "
            "to target files nothing has identified yet. 0 scans regardless of "
            "tag count.")
        wf.addRow("Fewer than N tags", self.max_tags)

        self.tag_count_service = QComboBox()
        self.tag_count_service.setToolTip(
            "Which service's tags the count above refers to. The default "
            "counts every service at once, so a file with tags in any of them "
            "no longer looks untagged.")
        wf.addRow("Counting tags in", self.tag_count_service)

        self.limit = _TypeableSpinBox()
        self.limit.setRange(0, 1_000_000)
        self.limit.setSpecialValueText("no cap")
        self.limit.setToolTip(
            "Hydrus applies this after every other filter, so the cap is spent "
            "on files that actually matched. With scan markers on, the next "
            "run continues past them rather than starting over.")
        wf.addRow("Stop after N files", self.limit)

        self.images_only = QCheckBox("Images only")
        self.images_only.setToolTip(
            "The boorus FurTag queries are image galleries, so video and PDFs "
            "would spend lookups on certain misses.")
        wf.addRow(self.images_only)

        self.status_filter = QComboBox()
        self.status_filter.addItem("Inbox and archive", "any")
        self.status_filter.addItem("Inbox only", "inbox")
        self.status_filter.addItem("Archive only", "archive")
        wf.addRow("File status", self.status_filter)
        left.addWidget(what)
        left.addStretch()

        also = QGroupBox("Also require")
        al = QVBoxLayout(also)
        self.extra_predicates = QTextEdit()
        self.extra_predicates.setPlaceholderText(
            "One Hydrus search predicate per line, e.g.\n"
            "system:import time < 30 days ago\n"
            "-system:has notes")
        self.extra_predicates.setMaximumHeight(64)
        self.extra_predicates.setToolTip(
            "Anything you could type into a Hydrus search page. A predicate "
            "Hydrus rejects fails the whole search, so it is reported rather "
            "than skipped.")
        al.addWidget(self.extra_predicates)

        # Deleted duplicates are looked up automatically on a miss and are not
        # mentioned here at all: they cost nothing when a file already
        # matched, can only add tags the boorus hold for that exact picture,
        # and stay editable from the CLI for anyone who wants them off.

        # ── Bookkeeping ─────────────────────────────────────────────────────
        book = QGroupBox("Bookkeeping")
        bf = QFormLayout(book)
        bf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.mark_tags = QCheckBox("Mark scanned files with tags")
        self.mark_tags.setToolTip(
            "Writes furtag:scanned plus furtag:matched or furtag:nomatch. This "
            "is how a capped scan continues through the database instead of "
            "re-examining the same files, and unlike a hidden ledger you can "
            "search and undo it inside Hydrus.")
        bf.addRow(self.mark_tags)

        self.state_prefix = QLineEdit()
        self.state_prefix.setPlaceholderText("furtag")
        bf.addRow("Tag namespace", self.state_prefix)

        self.state_service = QLineEdit()
        self.state_service.setPlaceholderText(
            "the output tag service (or a name, e.g. my tags)")
        self.state_service.setToolTip(
            "A local tag service keeps bookkeeping out of the tags you share.")
        bf.addRow("Marker tag service", self.state_service)

        self.skip_scanned = QCheckBox("Skip files a previous scan marked")
        bf.addRow(self.skip_scanned)

        self.write_report = QCheckBox("Write a JSONL report for each Hydrus run")
        self.write_report.setToolTip(
            "A JSONL line per file plus a text summary, saved beside "
            "settings.json.")
        bf.addRow(self.write_report)
        mid.addWidget(book)
        mid.addStretch()

        # ── Budgets ─────────────────────────────────────────────────────────
        budget = QGroupBox("Budget")
        gf = QFormLayout(budget)
        gf.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.dry_run = QCheckBox("Dry run (report only)")
        self.dry_run.setToolTip(
            "Bulk-tagging a live database is hard to undo. This runs every "
            "lookup and writes the report without touching Hydrus.")
        gf.addRow(self.dry_run)

        self.time_budget = _TypeableSpinBox()
        self.time_budget.setRange(0, 10_080)
        self.time_budget.setSpecialValueText("unlimited")
        self.time_budget.setSuffix(" min")
        gf.addRow("Stop after", self.time_budget)

        self.max_errors = _TypeableSpinBox()
        self.max_errors.setRange(0, 10_000)
        self.max_errors.setSpecialValueText("never")
        gf.addRow("Max failures in a row", self.max_errors)
        right.addWidget(budget)
        right.addWidget(also)
        right.addStretch()

        # Built here but deliberately not added to this layout: the host
        # pins it outside the scroll area, because the query you are about to
        # run is the one thing that must never be scrolled out of sight.
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)

        for widget, signal in (
                (self.domain, "currentIndexChanged"),
                (self.max_tags, "valueChanged"),
                (self.limit, "valueChanged"),
                (self.images_only, "toggled"),
                (self.status_filter, "currentIndexChanged"),
                (self.mark_tags, "toggled"),
                (self.skip_scanned, "toggled"),
                (self.dry_run, "toggled"),
        ):
            getattr(widget, signal).connect(self._on_changed)
        self.tag_count_service.currentIndexChanged.connect(self._on_changed)
        for edit in (self.state_prefix, self.state_service):
            edit.textChanged.connect(self._on_changed)
        self.extra_predicates.textChanged.connect(self._on_changed)
        self.mark_tags.toggled.connect(self._update_enabled)
        self.domain.currentIndexChanged.connect(self._update_enabled)

    def _on_changed(self, *_args) -> None:
        self.changed.emit()

    def _update_enabled(self, *_args) -> None:
        self._update_domain_help()
        marking = self.mark_tags.isChecked()
        for widget in (self.state_prefix, self.state_service,
                       self.skip_scanned):
            widget.setEnabled(marking)

    def has_services(self) -> bool:
        """Whether Hydrus has been asked what file domains exist."""
        return bool(self._services)

    #: Hydrus's own names for its two virtual local domains, and what each
    #: actually covers. Both read like plain domains in the dropdown, and the
    #: difference between them is not guessable from the names.
    DOMAIN_NOTES = {
        "hydrus local file storage":
            "everything Hydrus stores, including the trash and its own "
            "repository update files",
        "combined local file domains":
            "every local domain at once, without the trash — the default",
    }

    def set_services(self, services: List[Tuple[str, str]],
                     tag_services: Optional[List[Tuple[str, str]]] = None
                     ) -> None:
        """Populate both pickers, keeping the current choices if they survive."""
        wanted = self.domain.currentData() or ""
        self._services = list(services)
        self.domain.blockSignals(True)
        self.domain.clear()
        self.domain.addItem("Hydrus default (all your files, no trash)", "")
        for name, key in self._services:
            self.domain.addItem(name, key)
            note = self.DOMAIN_NOTES.get(name.lower())
            if note:
                self.domain.setItemData(
                    self.domain.count() - 1, note, Qt.ItemDataRole.ToolTipRole)
        index = self.domain.findData(wanted)
        self.domain.setCurrentIndex(max(0, index))
        self.domain.blockSignals(False)

        if tag_services is not None:
            self._tag_services = list(tag_services)
        wanted_tags = self.tag_count_service.currentData() or ""
        self.tag_count_service.blockSignals(True)
        self.tag_count_service.clear()
        self.tag_count_service.addItem("All tag services", "")
        for label, name in self._tag_services:
            self.tag_count_service.addItem(label, name)
        index = self.tag_count_service.findData(wanted_tags)
        self.tag_count_service.setCurrentIndex(max(0, index))
        self.tag_count_service.blockSignals(False)
        self._update_domain_help()
        self._on_changed()

    def _update_domain_help(self) -> None:
        note = self.DOMAIN_NOTES.get(self.domain.currentText().lower())
        self.domain_help.setText(f"<i>{note}</i>" if note else "")
        self.domain_help.setVisible(bool(note))

    def load_from(self, scan: HydrusScanSettings) -> None:
        if scan.file_service_key and not self._services:
            # Remember the saved domain even before Hydrus has been asked what
            # exists, so reopening the panel offline does not silently reset it.
            self._services = [
                (scan.file_service_name or scan.file_service_key,
                 scan.file_service_key)]
        self.set_services(self._services)
        index = self.domain.findData(scan.file_service_key or "")
        self.domain.setCurrentIndex(max(0, index))
        self.max_tags.setValue(scan.max_tag_count)
        index = self.tag_count_service.findData(scan.tag_count_service)
        self.tag_count_service.setCurrentIndex(max(0, index))
        self.limit.setValue(scan.limit)
        self.images_only.setChecked(scan.images_only)
        status = ("inbox" if scan.inbox_only
                  else "archive" if scan.archive_only else "any")
        self.status_filter.setCurrentIndex(
            max(0, self.status_filter.findData(status)))
        self.extra_predicates.setPlainText("\n".join(scan.extra_predicates))
        # Not shown, so remembered verbatim and written back unchanged —
        # the CLI can still turn duplicate lookups off.
        self._dupes = (scan.include_deleted_duplicates,
                       scan.deleted_duplicates_when,
                       scan.max_deleted_duplicates)
        self.mark_tags.setChecked(scan.mark_state_tags)
        self.state_prefix.setText(scan.state_tag_prefix)
        self.state_service.setText(scan.state_tag_service)
        self.skip_scanned.setChecked(scan.skip_already_scanned)
        self.write_report.setChecked(scan.write_report)
        self.dry_run.setChecked(scan.dry_run)
        self.time_budget.setValue(scan.time_budget_minutes)
        self.max_errors.setValue(scan.max_consecutive_errors)
        self._update_enabled()

    def to_scan_settings(self) -> HydrusScanSettings:
        scan = HydrusScanSettings()
        scan.file_service_key = self.domain.currentData() or ""
        scan.file_service_name = (
            self.domain.currentText() if scan.file_service_key else "")
        scan.max_tag_count = self.max_tags.value()
        scan.tag_count_service = self.tag_count_service.currentData() or ""
        scan.limit = self.limit.value()
        scan.images_only = self.images_only.isChecked()
        status = self.status_filter.currentData()
        scan.inbox_only = status == "inbox"
        scan.archive_only = status == "archive"
        scan.extra_predicates = [
            line.strip()
            for line in self.extra_predicates.toPlainText().splitlines()
            if line.strip()]
        (scan.include_deleted_duplicates, scan.deleted_duplicates_when,
         scan.max_deleted_duplicates) = self._dupes
        scan.mark_state_tags = self.mark_tags.isChecked()
        scan.state_tag_prefix = self.state_prefix.text().strip() or "furtag"
        scan.state_tag_service = self.state_service.text().strip()
        scan.skip_already_scanned = (
            self.skip_scanned.isChecked() and scan.mark_state_tags)
        scan.write_report = self.write_report.isChecked()
        scan.dry_run = self.dry_run.isChecked()
        scan.time_budget_minutes = self.time_budget.value()
        scan.max_consecutive_errors = self.max_errors.value()
        normalize_hydrus_scan(scan)
        return scan

    def set_preview(self, predicates: List[str]) -> None:
        query = " AND ".join(predicates) if predicates else "everything"
        domain = self.domain.currentText()
        text = f"<b>Hydrus query:</b> {query}<br><b>Domain:</b> {domain}"
        if self.dry_run.isChecked():
            text += "<br><b>Dry run — nothing will be written.</b>"
        self.preview.setText(text)


class DiscoverWorker(QThread):
    def __init__(self, integrator: TagIntegrator, root: Path,
                 bridge: QtEventBridge, generation: int) -> None:
        super().__init__()
        self.integrator = integrator
        self.root = root
        self.bridge = bridge
        self.generation = generation

    def run(self) -> None:
        try:
            inv = self.integrator.discover(self.root)
            self.bridge.inventory_ready.emit(
                (self.generation, self.root, inv))
        except Exception as e:
            self.bridge.inventory_failed.emit(
                (self.generation, self.root, str(e)))


# ── Credentials dialog ───────────────────────────────────────────────────────

class CredentialsDialog(QDialog):
    def __init__(self, store: CredentialStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Credentials")
        self.setMinimumSize(400, 320)
        _fit_window_to_screen(self, prefer_w=480, prefer_h=520)
        layout = QVBoxLayout(self)

        usable, msg = store.keyring_status()
        if keyring_disabled():
            # Not a warning: this is a working backend the dialog writes to.
            status = QLabel(
                f"Saving to <code>{env_file_path()}</code> — the OS keyring is "
                "turned off, so you will not be asked for a keychain password. "
                "Anyone who can read that file can read these credentials.")
        elif usable:
            status = QLabel(msg)
        else:
            status = QLabel(f"⚠️ {msg}\nEnv vars (FURTAG_*) still work.")
        status.setWordWrap(True)
        layout.addWidget(status)

        self.fields: Dict[str, QLineEdit] = {}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_host = QWidget()
        form = QFormLayout(form_host)

        sections = [
            ("e621", ["e621_username", "e621_api_key"]),
            ("InkBunny", ["inkbunny_username", "inkbunny_password"]),
            ("Danbooru", ["danbooru_username", "danbooru_api_key"]),
            ("Gelbooru", ["gelbooru_user_id", "gelbooru_api_key"]),
            ("SauceNAO", ["sauce_nao_api_key"]),
            ("Hydrus", ["hydrus_api_url", "hydrus_access_key"]),
        ]
        snap = store.load_all()
        for title, keys in sections:
            form.addRow(QLabel(f"<b>{title}</b>"))
            for key in keys:
                edit = QLineEdit()
                if key in SECRET_FIELDS:
                    edit.setEchoMode(QLineEdit.EchoMode.Password)
                val = snap.get(key)
                if val:
                    edit.setText(val)
                    edit.setPlaceholderText("(saved)")
                env_name = FIELD_MAP[key][0]
                edit.setToolTip(f"Env: {env_name}")
                self.fields[key] = edit
                form.addRow(key.replace("_", " "), edit)

        scroll.setWidget(form_host)
        layout.addWidget(scroll)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        clear_btn = buttons.addButton(
            "Remove all saved", QDialogButtonBox.ButtonRole.DestructiveRole)
        clear_btn.clicked.connect(self._clear)
        layout.addWidget(buttons)

    def _save(self) -> None:
        updates = {k: e.text() for k, e in self.fields.items()}
        errors = self.store.save_fields(updates)
        if errors:
            QMessageBox.warning(self, "Credential save", "\n".join(errors))
        else:
            self.accept()

    def _clear(self) -> None:
        where = (f"from {env_file_path().name}" if keyring_disabled()
                 else "from the OS keyring")
        if QMessageBox.question(
                self, "Remove credentials",
                f"Remove all FurTag secrets {where}?") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_all()
        for k in ALL_FIELDS:
            self.fields[k].clear()


# ── Reset dialog ─────────────────────────────────────────────────────────────

class ResetDialog(QDialog):
    def __init__(self, parent=None, settings: Optional[Settings] = None) -> None:
        super().__init__(parent)
        # Sidecar name patterns decide which .json files count as FurTag's, so
        # the dialog's preview and its delete must use the live settings.
        self.settings = settings
        self.setWindowTitle("Reset folder (NUKE!)")
        self.root: Optional[Path] = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Choose which FurTag-generated data to permanently remove. "
            "Source media and source PDFs are never deleted."))
        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit)
        row.addWidget(browse)
        layout.addLayout(row)
        self.preview = QLabel("Select a folder to preview.")
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.include_ledgers_reports = QCheckBox("Remove ledgers and reports")
        self.include_ledgers_reports.setChecked(True)
        self.include_sidecars = QCheckBox("Remove sidecars")
        self.include_sidecars.setChecked(True)
        self.include_pdf_pages = QCheckBox("Remove rendered PDF page PNGs")
        for option in (
                self.include_ledgers_reports,
                self.include_sidecars,
                self.include_pdf_pages):
            option.toggled.connect(self._update_preview)
            layout.addWidget(option)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        # Scanning the tree costs a full os.walk, so coalesce keystrokes
        # instead of walking once per typed character.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(300)
        self._preview_timer.timeout.connect(self._update_preview)
        self.path_edit.textChanged.connect(self._preview_timer.start)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder to reset")
        if d:
            self.path_edit.setText(d)

    def _update_preview(self) -> None:
        p = Path(self.path_edit.text().strip()).expanduser()
        if not p.is_dir():
            self.preview.setText("Not a valid directory.")
            return
        if is_filesystem_root(p):
            self.preview.setText("Refusing filesystem root.")
            return
        ledgers, sidecars = _nuke_candidates(p, self.settings)
        pages, _ = _pdf_render_candidates(p)
        selected = (
            (len(ledgers) if self.include_ledgers_reports.isChecked() else 0)
            + (len(sidecars) if self.include_sidecars.isChecked() else 0)
            + (len(pages) if self.include_pdf_pages.isChecked() else 0)
        )
        self.preview.setText(
            f"Found:\n"
            f"  · {len(ledgers)} ledger/report file(s)\n"
            f"  · {len(sidecars)} sidecar file(s)\n"
            f"  · {len(pages)} rendered PDF page file(s)\n"
            f"Selected for removal: {selected} file(s)")

    def _confirm(self) -> None:
        p = Path(self.path_edit.text().strip()).expanduser().resolve()
        if not p.is_dir():
            QMessageBox.warning(self, "Reset", "Not a valid directory.")
            return
        if is_filesystem_root(p):
            QMessageBox.warning(self, "Reset", "Refusing filesystem root.")
            return
        selected = []
        if self.include_ledgers_reports.isChecked():
            selected.append("ledgers and reports")
        if self.include_sidecars.isChecked():
            selected.append("sidecars")
        if self.include_pdf_pages.isChecked():
            selected.append("rendered PDF page PNGs")
        if not selected:
            QMessageBox.warning(
                self, "Reset", "Select at least one category to remove.")
            return
        # Two confirmations, second typed
        if QMessageBox.question(
                self, "Confirm reset",
                f"Permanently remove {', '.join(selected)} under:\n"
                f"{p}\n\nContinue?") != QMessageBox.StandardButton.Yes:
            return
        text, ok = QInputDialog.getText(
            self, "Type to confirm",
            'Type ARE YOU SURE? exactly to proceed:')
        if not ok or text.strip() != "ARE YOU SURE?":
            QMessageBox.information(self, "Reset", "Cancelled — confirmation phrase mismatch.")
            return
        self.root = p
        self.accept()

    def perform_reset(self) -> Tuple[int, List[Tuple[Path, OSError]]]:
        """Delete via the engine's shared nuke, so CLI and GUI stay identical."""
        if self.root is None:
            return 0, []
        return perform_nuke(
            self.root, include_pdf_pages=self.include_pdf_pages.isChecked(),
            settings=self.settings,
            include_ledgers_reports=self.include_ledgers_reports.isChecked(),
            include_sidecars=self.include_sidecars.isChecked())


# ── Review dialog ────────────────────────────────────────────────────────────

class ReviewDialog(QDialog):
    def __init__(self, integrator: TagIntegrator, root: Path, parent=None) -> None:
        super().__init__(parent)
        self.integrator = integrator
        self.root = root
        self.queue = ReviewQueue(root)
        self.queue.load()
        self.setWindowTitle(f"Needs review — {len(self.queue)}")
        self.resize(700, 480)
        layout = QVBoxLayout(self)

        self.list = QListWidget()
        layout.addWidget(self.list)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.detail.setOpenExternalLinks(True)
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        self.approve_btn = QPushButton("Approve (A)")
        self.reject_btn = QPushButton("Reject (R)")
        self.open_btn = QPushButton("Open URL")
        self.bulk_approve = QPushButton("Approve all")
        self.bulk_reject = QPushButton("Reject all")
        for b in (self.approve_btn, self.reject_btn, self.open_btn,
                  self.bulk_approve, self.bulk_reject):
            row.addWidget(b)
        layout.addLayout(row)

        self.approve_btn.clicked.connect(lambda: self._decide(True))
        self.reject_btn.clicked.connect(lambda: self._decide(False))
        self.open_btn.clicked.connect(self._open_url)
        self.bulk_approve.clicked.connect(lambda: self._bulk(True))
        self.bulk_reject.clicked.connect(lambda: self._bulk(False))
        self.list.currentItemChanged.connect(self._show_detail)
        self._reload()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_A:
            self._decide(True)
        elif event.key() == Qt.Key.Key_R:
            self._decide(False)
        else:
            super().keyPressEvent(event)

    def _reload(self) -> None:
        self.list.clear()
        self.queue.load()
        for item in self.queue.list_items():
            li = QListWidgetItem(
                f"{item.relpath}  ·  {item.match_class}  ·  {item.platform}")
            li.setData(Qt.ItemDataRole.UserRole, item.id)
            self.list.addItem(li)
        self.setWindowTitle(f"Needs review — {len(self.queue)}")
        if self.list.count():
            self.list.setCurrentRow(0)

    def _current(self) -> Optional[PendingReview]:
        li = self.list.currentItem()
        if not li:
            return None
        return self.queue.get(li.data(Qt.ItemDataRole.UserRole))

    def _show_detail(self) -> None:
        p = self._current()
        if not p:
            self.detail.setText("")
            return
        url = p.location or ""
        link = f'<a href="{url}">{url}</a>' if url else "(none)"
        self.detail.setText(
            f"<b>{p.relpath}</b><br>"
            f"Match: {p.match_class} on {p.platform}<br>"
            f"URL: {link}<br>"
            f"Tags: {', '.join(p.fluffle_tags) or '(none)'}<br>"
            f"<i>Non-e621 approvals use Fluffle's thinner tags + URL.</i>")

    def _decide(self, approve: bool) -> None:
        p = self._current()
        if not p:
            return
        try:
            completed = self.integrator.resolve_pending_review(
                p, approve=approve, root=self.root)
        except Exception as e:
            completed = False
            QMessageBox.warning(
                self, "Review item deferred",
                f"Could not finish this item yet; it remains in the review "
                f"queue.\n\n{e}")
        if not completed:
            self._reload()
            return
        self._reload()

    def _bulk(self, approve: bool) -> None:
        failed = 0
        for p in list(self.queue.list_items()):
            try:
                completed = self.integrator.resolve_pending_review(
                    p, approve=approve, root=self.root)
            except Exception:
                completed = False
            if not completed:
                failed += 1
        self._reload()
        if failed:
            QMessageBox.warning(
                self, "Some review items were deferred",
                f"{failed} item(s) could not be completed and remain queued. "
                "This is usually a temporary source or output failure.")

    def _open_url(self) -> None:
        p = self._current()
        if p and p.location:
            webbrowser.open(p.location)


# ── Settings tabs ────────────────────────────────────────────────────────────

class SettingsPanel(QWidget):
    """Tabbed settings editor with Save as default / Restore defaults."""

    source_settings_changed = Signal()

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        # No stored copy: the widgets are the state, and to_settings() reads it.
        # A cached Settings here would go stale after the first save.
        self._initial = settings.clone()
        self._build()

    def _add_tab(self, title: str, form_widget: QWidget) -> None:
        """Each settings page scrolls so the window can shrink freely."""
        form_widget.setMinimumWidth(0)
        form_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.tabs.addTab(_wrap_scroll(form_widget), title)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, stretch=1)

        # Output
        out = QWidget()
        of = QFormLayout(out)
        of.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.hydrus_enabled = QCheckBox("Enable Hydrus Client API")
        self.hydrus_import = QCheckBox("Import files (off = tag-only)")
        self.hydrus_import_unmatched = QCheckBox("Import unmatched files")
        self.hydrus_tag_service = QLineEdit()
        self.hydrus_tag_deleted = QCheckBox("Tag deleted-file duplicate groups")
        self.sidecars_enabled = QCheckBox("Also write sidecars when Hydrus is on")
        self.show_run_stats = QCheckBox("Show end-of-run stats recap")
        self.write_folder_json_report = QCheckBox(
            "Save a JSON report after every folder scan")
        self.sidecar_format = QComboBox()
        self.sidecar_format.addItems(["txt", "json"])
        self.sidecar_tag_fn = QLineEdit()
        self.sidecar_url_fn = QLineEdit()
        self.sidecar_json_fn = QLineEdit()
        of.addRow(self.hydrus_enabled)
        of.addRow(self.hydrus_import)
        of.addRow(self.hydrus_import_unmatched)
        of.addRow("Tag service", self.hydrus_tag_service)
        of.addRow(self.hydrus_tag_deleted)
        of.addRow(self.sidecars_enabled)
        of.addRow(self.show_run_stats)
        of.addRow(self.write_folder_json_report)
        of.addRow("Sidecar format", self.sidecar_format)
        of.addRow("Tag filename", self.sidecar_tag_fn)
        of.addRow("URL filename", self.sidecar_url_fn)
        of.addRow("JSON filename", self.sidecar_json_fn)
        self._add_tab("Output", out)

        # Hydrus pages
        hy = QWidget()
        hf = QFormLayout(hy)
        hf.setVerticalSpacing(4)
        self.exact_url_enrichment = QCheckBox(
            "Also scrape exact URLs through Hydrus (slow)")
        self.exact_url_enrichment.setToolTip(
            "Optional legacy enrichment for timestamps and other parser-only "
            "metadata. Descriptions can be imported directly without this "
            "slow downloader queue.")
        self.direct_source_notes = QCheckBox(
            "Import source descriptions directly")
        self.direct_source_notes.setToolTip(
            "Reuse source API responses FurTag already fetched and write their "
            "descriptions straight to Hydrus notes. Requires the Hydrus "
            "'Add Notes / Edit File Notes' permission.")
        self.exact_url_enrichment_page_name = QLineEdit()
        self.results_pages = QCheckBox("Enable Hydrus review pages")
        self.page_controls: Dict[str, Dict[str, object]] = {}

        def make_page_group(
                prefix: str, title: str, *, result_page: bool) -> QGroupBox:
            group = QGroupBox(title)
            form = QFormLayout(group)
            enabled = QCheckBox("Enabled")
            name = QLineEdit()
            limit = QSpinBox()
            limit.setRange(0, 1_000_000)
            limit.setSpecialValueText("Unlimited")
            controls: Dict[str, object] = {
                "enabled": enabled, "name": name, "limit": limit}
            form.addRow(enabled)
            form.addRow("Page name", name)
            form.addRow("Limit (0 = unlimited)", limit)
            if result_page:
                mode = QComboBox()
                mode.addItem("Live — first N results", "live")
                mode.addItem("End of run — newest N results", "end_of_run")
                mode.setToolTip(
                    "Live pages append during the scan and finite limits keep "
                    "the first N. End-of-run pages are created when the scan "
                    "finishes and finite limits keep the newest N.")
                controls["mode"] = mode
                form.addRow("Publication", mode)
                mode.currentIndexChanged.connect(
                    self._update_page_interval_visibility)
            enabled.toggled.connect(self._update_page_interval_visibility)
            self.page_controls[prefix] = controls
            return group

        page_groups = (
            make_page_group("new_imports", "New Imports", result_page=True),
            make_page_group("newly_tagged", "Newly Tagged", result_page=True),
            make_page_group("duplicate_tagged", "Duplicate Tagged", result_page=True),
            make_page_group("already_tagged", "Already Tagged", result_page=False),
        )
        self.live_page_interval = QSpinBox()
        self.live_page_interval.setRange(0, 60)
        self.live_page_interval.setSuffix(" seconds")
        self.live_page_interval.setSpecialValueText("Immediately")
        self.live_page_interval_box = QWidget()
        live_interval_layout = QFormLayout(self.live_page_interval_box)
        live_interval_layout.setContentsMargins(0, 0, 0, 0)
        live_interval_layout.addRow(
            "Live update interval", self.live_page_interval)
        self.hydrus_profile_label = QLabel()
        self.rotate_hydrus_profile = QPushButton("Reset…")
        self.rotate_hydrus_profile.setToolTip(
            "Use a new or replaced Hydrus database: rotates FurTag's "
            "non-secret Hydrus database identity. Use this "
            "only after replacing the Hydrus database at this API address; "
            "existing completion checkpoints will be revalidated.")
        self.rotate_hydrus_profile.clicked.connect(
            self._rotate_hydrus_profile_uuid)
        metadata_group = QGroupBox("Metadata downloader (separate Hydrus-owned page)")
        metadata_form = QFormLayout(metadata_group)
        metadata_form.addRow(self.direct_source_notes)
        metadata_form.addRow(self.exact_url_enrichment)
        metadata_form.addRow("Downloader page name",
                             self.exact_url_enrichment_page_name)
        metadata_group.setToolTip(
            "Hydrus owns this downloader page, so review-page limits and "
            "publication modes do not apply to it.")
        hf.addRow(metadata_group)
        hf.addRow(self.results_pages)
        # The four review pages are the same short form four times over, so
        # they go two by two rather than end to end. Stacked, they were most
        # of the reason this page had to be scrolled.
        page_grid = QGridLayout()
        page_grid.setContentsMargins(0, 0, 0, 0)
        page_grid.setHorizontalSpacing(8)
        for i, group in enumerate(page_groups):
            page_grid.addWidget(group, i // 2, i % 2)
        page_grid.setColumnStretch(0, 1)
        page_grid.setColumnStretch(1, 1)
        hf.addRow(page_grid)
        hf.addRow(self.live_page_interval_box)
        identity_row = QHBoxLayout()
        identity_row.addWidget(self.hydrus_profile_label, stretch=1)
        identity_row.addWidget(self.rotate_hydrus_profile)
        hf.addRow("Database identity", identity_row)
        self._add_tab("Hydrus", hy)
        self.results_pages.toggled.connect(
            self._update_page_interval_visibility)

        # Sources
        src = QWidget()
        sf = QFormLayout(src)
        self.src_checks: Dict[str, QCheckBox] = {}
        for name, label in (
            ("e621", "e621"), ("inkbunny", "InkBunny"),
            ("danbooru", "Danbooru"), ("gelbooru", "Gelbooru"),
            ("fluffle", "Fluffle"), ("saucenao", "SauceNAO"),
        ):
            cb = QCheckBox(f"Enable {label}")
            cb.toggled.connect(
                lambda _checked: self.source_settings_changed.emit())
            self.src_checks[name] = cb
            sf.addRow(cb)
        self._add_tab("Sources", src)

        # Matching
        mat = QWidget()
        mf = QFormLayout(mat)
        self.sn_min = QDoubleSpinBox()
        self.sn_min.setRange(0, 100)
        self.sn_auth = QDoubleSpinBox()
        self.sn_auth.setRange(0, 100)
        self.fluffle_checks: Dict[str, QCheckBox] = {}
        fl_box = QVBoxLayout()
        for cls in FLUFFLE_MATCH_CLASSES:
            cb = QCheckBox(cls)
            self.fluffle_checks[cls] = cb
            fl_box.addWidget(cb)
        fl_w = QWidget()
        fl_w.setLayout(fl_box)
        self.fluffle_tossup_e621 = QCheckBox("Auto-accept tossUp only on e621")
        self.fluffle_review = QComboBox()
        self.fluffle_review.addItems(list(FLUFFLE_REVIEW_MODES))
        mf.addRow("SauceNAO min %", self.sn_min)
        mf.addRow("SauceNAO auth %", self.sn_auth)
        mf.addRow("Fluffle auto-accept", fl_w)
        mf.addRow(self.fluffle_tossup_e621)
        mf.addRow("Fluffle review mode", self.fluffle_review)
        self._add_tab("Matching", mat)

        # PDF
        pdf = QWidget()
        pf = QFormLayout(pdf)
        self.pdf_enabled = QCheckBox("Render PDFs")
        self.pdf_dpi = QSpinBox()
        self.pdf_dpi.setRange(72, 2400)
        self.pdf_write_sc = QCheckBox("Write comic:/page:/creator: base sidecars")
        self.pdf_write_sc.setToolTip(
            "On render, each page gets comic: and page: tags (and creator: if "
            "you set an artist in the pre-render dialog). Also writes "
            ".furtag_pdf.json so later runs keep those names.")
        pf.addRow(self.pdf_enabled)
        pf.addRow("Default DPI", self.pdf_dpi)
        pf.addRow(self.pdf_write_sc)
        note = QLabel(
            "When a scan needs to render PDFs, FurTag asks for a comic name "
            "and optional artist for each file before breaking them into pages.")
        note.setWordWrap(True)
        pf.addRow(note)
        self._add_tab("PDF", pdf)

        # Advanced performance
        adv = QWidget()
        af = QFormLayout(adv)
        warn = QLabel("⚠️ Lowering intervals below shipped defaults may get you banned.")
        warn.setWordWrap(True)
        af.addRow(warn)
        self.pace_spins: Dict[str, QDoubleSpinBox] = {}
        for name, floor in PACE_FLOORS.items():
            sp = QDoubleSpinBox()
            sp.setRange(floor, 60.0)
            sp.setSingleStep(0.1)
            sp.setDecimals(2)
            self.pace_spins[name] = sp
            af.addRow(f"{name} interval (s)", sp)
        self.hash_workers = QSpinBox()
        self.hash_workers.setRange(0, 32)
        self.hash_workers.setSpecialValueText("Auto")
        af.addRow("Hash workers (0=auto)", self.hash_workers)
        self._add_tab("Advanced", adv)

        # Buttons stay pinned under the tab pages
        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save as default")
        restore_btn = QPushButton("Restore defaults")
        save_btn.clicked.connect(self._save_defaults)
        restore_btn.clicked.connect(self._restore_defaults)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(restore_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.load_from(self._initial)

    def _update_page_interval_visibility(self, *_args) -> None:
        """Only show cadence when an enabled live result page can use it."""
        visible = self.results_pages.isChecked() and any(
            bool(controls["enabled"].isChecked())
            and controls["mode"].currentData() == "live"
            for prefix, controls in self.page_controls.items()
            if prefix != "already_tagged")
        self.live_page_interval_box.setVisible(visible)

    def load_from(self, s: Settings) -> None:
        o, h, src, m, p, perf = (
            s.output, s.hydrus, s.sources, s.matching, s.pdf, s.performance)
        self.hydrus_enabled.setChecked(o.hydrus_enabled)
        self.hydrus_import.setChecked(o.hydrus_import)
        self.hydrus_import_unmatched.setChecked(o.hydrus_import_unmatched)
        self.hydrus_tag_service.setText(o.hydrus_tag_service)
        self.hydrus_tag_deleted.setChecked(o.hydrus_tag_deleted_duplicates)
        self.sidecars_enabled.setChecked(o.sidecars_enabled)
        self.show_run_stats.setChecked(o.show_run_stats_dialog)
        self.write_folder_json_report.setChecked(o.write_folder_json_report)
        self.sidecar_format.setCurrentText(o.sidecar_format)
        self.sidecar_tag_fn.setText(o.sidecar_tag_filename)
        self.sidecar_url_fn.setText(o.sidecar_url_filename)
        self.sidecar_json_fn.setText(o.sidecar_json_filename)
        self.results_pages.setChecked(h.results_pages_enabled)
        self.direct_source_notes.setChecked(h.direct_source_notes)
        self.exact_url_enrichment.setChecked(h.exact_url_enrichment)
        self.exact_url_enrichment_page_name.setText(
            h.exact_url_enrichment_page_name)
        for prefix, controls in self.page_controls.items():
            controls["enabled"].setChecked(
                getattr(h, f"{prefix}_page_enabled"))
            controls["name"].setText(getattr(h, f"{prefix}_page_name"))
            controls["limit"].setValue(getattr(h, f"{prefix}_page_limit"))
            if "mode" in controls:
                mode = getattr(h, f"{prefix}_page_mode")
                idx = controls["mode"].findData(mode)
                controls["mode"].setCurrentIndex(max(0, idx))
        self.live_page_interval.setValue(h.live_page_update_interval)
        self._update_page_interval_visibility()
        self.hydrus_profile_label.setText(
            h.hydrus_profile_uuid[:8] + "…" if h.hydrus_profile_uuid else "unset")
        for name, cb in self.src_checks.items():
            cb.setChecked(getattr(src, f"{name}_enabled", True))
        self.sn_min.setValue(m.saucenao_min_similarity)
        self.sn_auth.setValue(m.saucenao_auth_similarity)
        accepted = set(m.fluffle_accepted_matches or ["exact"])
        for cls, cb in self.fluffle_checks.items():
            cb.setChecked(cls in accepted)
        self.fluffle_tossup_e621.setChecked(m.fluffle_tossup_e621_only)
        idx = self.fluffle_review.findText(m.fluffle_review_mode)
        self.fluffle_review.setCurrentIndex(max(0, idx))
        self.pdf_enabled.setChecked(p.pdf_enabled)
        self.pdf_dpi.setValue(p.pdf_dpi)
        self.pdf_write_sc.setChecked(p.pdf_write_sidecars)
        for name, sp in self.pace_spins.items():
            sp.setValue(getattr(perf, f"{name}_interval"))
        self.hash_workers.setValue(perf.hash_worker_count)

    def to_settings(self) -> Settings:
        # Preserve persistent state not represented by widgets (recent scan
        # folders today, and future settings tomorrow) instead of rebuilding a
        # lossy object from defaults on every save.
        s = self._initial.clone()
        s.output.hydrus_enabled = self.hydrus_enabled.isChecked()
        s.output.hydrus_import = self.hydrus_import.isChecked()
        s.output.hydrus_import_unmatched = self.hydrus_import_unmatched.isChecked()
        s.output.hydrus_tag_service = self.hydrus_tag_service.text().strip() or "downloader tags"
        s.output.hydrus_tag_deleted_duplicates = self.hydrus_tag_deleted.isChecked()
        s.output.sidecars_enabled = self.sidecars_enabled.isChecked()
        s.output.show_run_stats_dialog = self.show_run_stats.isChecked()
        s.output.write_folder_json_report = (
            self.write_folder_json_report.isChecked())
        s.output.sidecar_format = self.sidecar_format.currentText()
        s.output.sidecar_tag_filename = self.sidecar_tag_fn.text().strip()
        s.output.sidecar_url_filename = self.sidecar_url_fn.text().strip()
        s.output.sidecar_json_filename = self.sidecar_json_fn.text().strip()
        s.hydrus.direct_source_notes = self.direct_source_notes.isChecked()
        s.hydrus.exact_url_enrichment = (
            self.exact_url_enrichment.isChecked())
        s.hydrus.exact_url_enrichment_page_name = (
            self.exact_url_enrichment_page_name.text().strip()
            or "FurTag Metadata")
        s.hydrus.results_pages_enabled = self.results_pages.isChecked()
        for prefix, controls in self.page_controls.items():
            setattr(s.hydrus, f"{prefix}_page_enabled",
                    controls["enabled"].isChecked())
            setattr(s.hydrus, f"{prefix}_page_name",
                    controls["name"].text().strip())
            setattr(s.hydrus, f"{prefix}_page_limit",
                    controls["limit"].value())
            if "mode" in controls:
                setattr(s.hydrus, f"{prefix}_page_mode",
                        controls["mode"].currentData())
        s.hydrus.live_page_update_interval = self.live_page_interval.value()
        for name, cb in self.src_checks.items():
            setattr(s.sources, f"{name}_enabled", cb.isChecked())
        s.matching.saucenao_min_similarity = self.sn_min.value()
        s.matching.saucenao_auth_similarity = self.sn_auth.value()
        s.matching.fluffle_accepted_matches = [
            c for c, cb in self.fluffle_checks.items() if cb.isChecked()] or ["exact"]
        s.matching.fluffle_tossup_e621_only = self.fluffle_tossup_e621.isChecked()
        s.matching.fluffle_review_mode = self.fluffle_review.currentText()
        s.pdf.pdf_enabled = self.pdf_enabled.isChecked()
        s.pdf.pdf_dpi = self.pdf_dpi.value()
        s.pdf.pdf_write_sidecars = self.pdf_write_sc.isChecked()
        for name, sp in self.pace_spins.items():
            setattr(s.performance, f"{name}_interval", sp.value())
        s.performance.hash_worker_count = self.hash_workers.value()
        return s

    def set_recent_scan_paths(self, paths: List[str]) -> None:
        """Keep hidden persistent history in sync with the main window."""
        self._initial.history.recent_scan_paths = list(paths)

    def set_hydrus_scan(self, scan: HydrusScanSettings) -> None:
        """Database-scan settings are edited on the Scan tab, not here."""
        self._initial.hydrus_scan = scan

    def _rotate_hydrus_profile_uuid(self) -> None:
        answer = QMessageBox.question(
            self, "New Hydrus database?",
            "Only do this if the Hydrus database was replaced or rebuilt. "
            "FurTag will distrust old Hydrus completion checkpoints and "
            "revalidate files on later scans. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._initial.hydrus.hydrus_profile_uuid = str(uuid.uuid4())
        self.hydrus_profile_label.setText(
            self._initial.hydrus.hydrus_profile_uuid[:8] + "…")
        QMessageBox.information(
            self, "Hydrus identity rotated",
            "The new identity is active for this session. Choose "
            "'Save as default' to keep it for future launches.")

    def _save_defaults(self) -> None:
        try:
            s = self.to_settings()
            validate_output_patterns(s.output)
        except SidecarPatternError as e:
            QMessageBox.warning(self, "Invalid pattern", str(e))
            return
        SettingsStore().save(s)
        self._initial = s.clone()
        QMessageBox.information(self, "Settings", "Defaults saved.")

    def _restore_defaults(self) -> None:
        self.load_from(Settings())


# ── Main window ──────────────────────────────────────────────────────────────

class DropFolderLabel(QLabel):
    folder_dropped = Signal(str)

    def __init__(self) -> None:
        super().__init__("Drop a folder here, or use Browse…")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Sunken)
        self.setMinimumHeight(40)
        self.setMaximumHeight(56)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAcceptDrops(True)
        self.setStyleSheet(
            "QLabel { background: #2a2a2a; color: #ccc; border-radius: 6px; padding: 6px; }")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and Path(path).is_dir():
                self.folder_dropped.emit(path)
                return


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FurTag")
        # Allow free resize; initial geometry is applied after the UI is built
        # so we know screen availableGeometry (and avoid overflowing the dock).
        self.setMinimumSize(560, 420)

        self.settings_store = SettingsStore()
        self.settings = self.settings_store.load()
        self.cred_store = CredentialStore()
        self.integrator = TagIntegrator(settings=self.settings)

        self.folder: Optional[Path] = None
        self.inventory: Optional[dict] = None
        self.scan_worker: Optional[QThread] = None  # folder or Hydrus scan
        self.discover_workers: List[DiscoverWorker] = []
        self._folder_generation = 0
        self.cancel_event = threading.Event()
        self.bridge = QtEventBridge()
        self.bridge.event.connect(self._on_event)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.inventory_ready.connect(self._on_inventory)
        self.bridge.inventory_failed.connect(self._on_inventory_failed)
        self._closing = False
        self._review_count = 0
        # Both are read while the UI is built, so they must exist first.
        self._last_report_path = ""
        self._scan_tab = 0
        # addTab() emits currentChanged while the run surface below the tabs
        # does not exist yet, so the handler has to know to stand down.
        self._ui_ready = False
        self._split_user_set = False

        self._build_ui()
        _fit_window_to_screen(self, prefer_w=900, prefer_h=640)
        # Route engine warnings (notify()) into the issue pane for the whole
        # session, not just during a scan — credential / Hydrus problems are
        # reported while loading credentials, well before any run starts. The
        # UI must exist first, since the bridge delivers in-thread signals
        # synchronously when emitted from the GUI thread.
        set_active_observer(QtObserver(self.bridge))
        self._migrate_legacy_credentials()
        self.integrator.load_credentials_from_store(self.cred_store)
        self._load_last_used_folder()
        self._refresh_source_status()

    def _migrate_legacy_credentials(self) -> None:
        """Fold pre-consolidation per-field keyring items into one item.

        Older builds stored one keyring item per credential, so macOS asked for
        authorization once per field on every launch. Reading those items one
        last time costs a single burst of prompts; everything written afterwards
        lives in one item created by this app, which is trusted automatically.
        """
        try:
            if not self.cred_store.needs_migration():
                return
        except Exception:
            return

        if sys.platform == "darwin":
            QMessageBox.information(
                self, "Updating saved credentials",
                "FurTag is consolidating your saved credentials into a single "
                "keychain entry so macOS stops asking on every launch.\n\n"
                "macOS will ask for your login keychain password once per saved "
                "credential during this one-time step. After it finishes, it "
                "should not ask again.")

        count, errors = self.cred_store.migrate_legacy_items()
        if errors:
            QMessageBox.warning(
                self, "Credential migration",
                "Some credentials could not be migrated. They are still saved "
                "in the old format and you can re-enter them under "
                "Hydrus → Credentials…\n\n" + "\n".join(errors[:10]))
        elif count:
            notify_info(
                f"Consolidated {count} saved credential(s) into one keychain entry.")

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Menu
        file_menu = self.menuBar().addMenu("&File")
        act_settings = QAction("Settings…", self)
        act_settings.setToolTip("Open the Settings tab (scan defaults, sources, Hydrus options).")
        act_settings.triggered.connect(
            lambda: self.main_tabs.setCurrentIndex(self.TAB_SETTINGS))
        act_quit = QAction("Quit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_settings)
        file_menu.addSeparator()
        file_menu.addAction(act_quit)

        # Source status (always visible) — colored dots so they read as
        # indicators, not clickable buttons. The two actions that change what
        # those dots say sit on the same row: they used to be a Hydrus menu,
        # which hid the only two settings that were not in the window.
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.creds_btn = QPushButton("Credentials…")
        self.creds_btn.setToolTip(
            "Set the Hydrus Client API address and access key.")
        self.creds_btn.clicked.connect(self._edit_credentials)
        self.reconnect_btn = QPushButton("Reconnect")
        self.reconnect_btn.setToolTip(
            "Re-check Hydrus and other configured sources right now, without "
            "restarting FurTag. The connection is otherwise only re-verified "
            "before a scan or after editing credentials.")
        self.reconnect_btn.clicked.connect(self._reconnect_sources)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.creds_btn)
        status_row.addWidget(self.reconnect_btn)
        root.addLayout(status_row)

        # Configuration on top, run output below, with a splitter between so
        # either half can be given the room. Each scan mode is a tab of its
        # own rather than a stack inside one: the database options are a tall
        # form, and nesting them in a short pane meant scrolling a box inside
        # a window that had space to spare.
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        root.addWidget(self.split, stretch=1)

        self.main_tabs = QTabWidget()
        self.main_tabs.setDocumentMode(True)
        self.main_tabs.currentChanged.connect(self._on_tab_changed)
        self.split.addWidget(self.main_tabs)

        # ── Folder scan tab ──────────────────────────────────────────────
        scan = QWidget()
        scan_lay = QVBoxLayout(scan)
        scan_lay.setContentsMargins(4, 8, 4, 4)
        scan_lay.setSpacing(6)
        folder_lay = scan_lay

        folder_row = QHBoxLayout()
        self.drop = DropFolderLabel()
        self.drop.folder_dropped.connect(self._set_folder)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse_folder)
        self.index_btn = QPushButton("Index")
        self.index_btn.setToolTip(
            "Count what is in this folder so a scan can start. Runs by itself "
            "when you pick a folder; the folder restored at launch waits for "
            "you to press this, so a large library does not tie up the window "
            "on every start.")
        self.index_btn.setEnabled(False)
        self.index_btn.clicked.connect(self._begin_discovery)
        folder_row.addWidget(self.drop, stretch=1)
        folder_row.addWidget(self.browse_btn)
        folder_row.addWidget(self.index_btn)
        folder_lay.addLayout(folder_row)

        recent_row = QHBoxLayout()
        recent_row.addWidget(QLabel("Recent:"))
        self.recent_folders = QComboBox()
        self.recent_folders.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.recent_folders.setMinimumContentsLength(28)
        self.recent_folders.activated.connect(self._select_recent_folder)
        self.clear_recents_btn = QPushButton("Clear")
        self.clear_recents_btn.setToolTip(
            "Forget the local recent-folder list. No media or scan results "
            "are removed.")
        self.clear_recents_btn.clicked.connect(self._clear_recent_folders)
        recent_row.addWidget(self.recent_folders, stretch=1)
        recent_row.addWidget(self.clear_recents_btn)
        folder_lay.addLayout(recent_row)
        self._refresh_recent_folders()

        self.inventory_label = QLabel("Choose a folder to scan.")
        self.inventory_label.setWordWrap(True)
        folder_lay.addWidget(self.inventory_label)

        # Session options — wrap on narrow windows via a flow-like row
        opts = QHBoxLayout()
        opts.addWidget(QLabel("This run:"))
        self.opt_import_unmatched = QCheckBox("Import unmatched")
        self.opt_sync_sidecars = QCheckBox("Sync sidecars first")
        self.opt_import_unmatched.setChecked(self.settings.output.hydrus_import_unmatched)
        for option in (
                self.opt_import_unmatched, self.opt_sync_sidecars):
            option.setToolTip(
                "Session-only override for this scan; saved defaults remain "
                "on the Settings tab.")
        opts.addWidget(self.opt_import_unmatched)
        opts.addWidget(self.opt_sync_sidecars)
        opts.addStretch()
        folder_lay.addLayout(opts)
        folder_lay.addStretch()
        self.main_tabs.addTab(scan, "Folder Scan")

        # ── Hydrus database tab ──────────────────────────────────────────
        hydrus_tab = QWidget()
        hydrus_lay = QVBoxLayout(hydrus_tab)
        hydrus_lay.setContentsMargins(4, 8, 4, 4)
        hydrus_lay.setSpacing(6)
        self.hydrus_panel = HydrusScanPanel(self.settings.hydrus_scan)
        self.hydrus_panel.changed.connect(self._refresh_scan_preview)
        self.hydrus_panel.refresh_services_btn.clicked.connect(
            self._refresh_hydrus_services)
        # No scroll area on purpose. The form is laid out to fit, and when
        # the window is shorter than it the window grows (see
        # _apply_scan_mode) rather than hiding half the options in a box.
        hydrus_lay.addWidget(self.hydrus_panel, stretch=1)
        hydrus_lay.addWidget(self.hydrus_panel.preview)
        self.main_tabs.addTab(hydrus_tab, "Hydrus Scan")

        # ── Shared run surface ───────────────────────────────────────────
        run = QWidget()
        scan_lay = QVBoxLayout(run)
        scan_lay.setContentsMargins(4, 4, 4, 4)
        scan_lay.setSpacing(6)

        # Progress cards
        prog = QHBoxLayout()
        self.hash_card = self._make_track_card("Hash tier")
        self.perc_card = self._make_track_card("Perceptual tier")
        prog.addWidget(self.hash_card["box"])
        prog.addWidget(self.perc_card["box"])
        scan_lay.addLayout(prog)

        self.source_totals_label = QLabel()
        self.source_totals_label.setWordWrap(True)
        self._set_source_totals({})
        scan_lay.addWidget(self.source_totals_label)

        self.review_badge = QPushButton("Needs review — 0")
        self.review_badge.clicked.connect(self._open_review)
        scan_lay.addWidget(self.review_badge)

        # Issues + log share remaining vertical space and can shrink
        bottom = QSplitter(Qt.Orientation.Vertical)
        bottom.setChildrenCollapsible(True)

        issues_wrap = QWidget()
        iw = QVBoxLayout(issues_wrap)
        iw.setContentsMargins(0, 0, 0, 0)
        iw.addWidget(QLabel("Recent issues"))
        self.issues = QListWidget()
        self.issues.setMinimumHeight(40)
        self.issues.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        iw.addWidget(self.issues)

        log_wrap = QWidget()
        lw = QVBoxLayout(log_wrap)
        lw.setContentsMargins(0, 0, 0, 0)
        lw.addWidget(QLabel("Run log"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        # Bound the document so a long scan can't grow it without limit.
        self.log.document().setMaximumBlockCount(2000)
        self.log.setMinimumHeight(40)
        self.log.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lw.addWidget(self.log)

        bottom.addWidget(issues_wrap)
        bottom.addWidget(log_wrap)
        bottom.setStretchFactor(0, 1)
        bottom.setStretchFactor(1, 2)
        bottom.setSizes([80, 140])
        scan_lay.addWidget(bottom, stretch=1)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        scan_lay.addWidget(self.summary_label)

        # Actions always visible at bottom of scan tab
        actions = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.cancel_btn = QPushButton("Cancel")
        self.another_btn = QPushButton("Scan Another Folder")
        self.reveal_btn = QPushButton("Reveal Results")
        self.reset_btn = QPushButton("Reset…")
        self.open_report_btn = QPushButton("Open Report")
        self.open_report_btn.setToolTip(
            "Open the summary this database scan wrote.")
        self.open_report_btn.setEnabled(False)
        self.open_report_btn.clicked.connect(self._open_scan_report)
        self.another_btn.setToolTip(
            "Keep current settings and pick a different folder to scan.")
        self.reveal_btn.setToolTip(
            "Open the scanned folder in your file manager.")
        self.reset_btn.setToolTip(
            "Remove generated sidecar/tag files for this folder so it can be "
            "rescanned from a clean slate. Choose which files in the dialog "
            "that follows.")
        self.cancel_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn.clicked.connect(self._cancel)
        self.another_btn.clicked.connect(self._scan_another)
        self.reveal_btn.clicked.connect(self._reveal)
        self.reset_btn.clicked.connect(self._reset)
        for b in (self.start_btn, self.cancel_btn, self.another_btn,
                  self.reveal_btn, self.reset_btn, self.open_report_btn):
            actions.addWidget(b)
        scan_lay.addLayout(actions)

        self.split.addWidget(run)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        # A QTabWidget takes its height from its tallest page, so the short
        # folder form would otherwise sit above the same gap the database
        # form needs. Each mode gets its own default split, until the user
        # drags the divider — after which their choice is what matters.
        self.split.splitterMoved.connect(self._remember_split)

        # ── Settings tab (scrollable pages) ──────────────────────────────
        self.settings_panel = SettingsPanel(self.settings)
        self.settings_panel.source_settings_changed.connect(
            self._refresh_source_status)
        self.main_tabs.addTab(self.settings_panel, "Settings")

        # Runs last: it toggles widgets from both scan tabs and the panel.
        self._ui_ready = True
        self._on_tab_changed(self.main_tabs.currentIndex())

    # ── Scan source ─────────────────────────────────────────────────────────

    #: Tab indices of the two scan modes. Settings is a third tab and does
    #: not change which mode the shared Start button acts on.
    TAB_FOLDER, TAB_HYDRUS, TAB_SETTINGS = 0, 1, 2

    def _scan_source(self) -> str:
        return "hydrus" if self._scan_tab == self.TAB_HYDRUS else "folder"

    def _on_tab_changed(self, index: int) -> None:
        # Selecting Settings must not retarget Start at nothing, so the last
        # scan tab stays the active mode until the other one is chosen.
        if index in (self.TAB_FOLDER, self.TAB_HYDRUS):
            self._scan_tab = index
        # A QTabWidget asks every page for its size hint, so the tallest one
        # sets the height of all of them. Ignoring the hidden pages lets the
        # short folder form actually be short.
        for i in range(self.main_tabs.count()):
            page = self.main_tabs.widget(i)
            page.setSizePolicy(
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Preferred if i == index
                else QSizePolicy.Policy.Ignored)
        self.main_tabs.widget(index).adjustSize()
        # Nothing on the run surface applies while you are editing settings,
        # and it was taking two thirds of the window — which is what forced
        # the settings pages to scroll. Hand the whole window over instead.
        # A run cannot be in progress here: the tab bar is disabled while one
        # is, so this can only be reached between runs.
        #
        # Guarded because addTab() emits currentChanged for the first tab,
        # which happens before the run surface joins the splitter.
        if self._ui_ready:
            self.split.widget(1).setVisible(index != self.TAB_SETTINGS)
        self._apply_scan_mode()

    def _remember_split(self, *_args) -> None:
        self._split_user_set = True

    def _apply_scan_mode(self) -> None:
        if not self._ui_ready:
            return
        # The splitter has one visible half on Settings, so there are no
        # sizes to divide and the scan controls below are not on screen.
        if self.main_tabs.currentIndex() == self.TAB_SETTINGS:
            return
        hydrus = self._scan_source() == "hydrus"
        # A database scan is hash-tier only and its results live in Hydrus, so
        # the perceptual card and every folder-shaped action would be lying.
        self.perc_card["box"].setVisible(not hydrus)
        self.review_badge.setVisible(not hydrus)
        for button in (self.another_btn, self.reveal_btn, self.reset_btn):
            button.setVisible(not hydrus)
        self.open_report_btn.setVisible(hydrus)
        self.open_report_btn.setEnabled(bool(self._last_report_path))
        if not self._split_user_set:
            total = max(400, self.split.height() or 880)
            if hydrus:
                # Give the form exactly the room it needs so it shows whole.
                # If the window is too short for that plus the run surface,
                # grow the window once (as far as the screen allows) rather
                # than hide options behind a scrollbar.
                top = self.main_tabs.widget(self.TAB_HYDRUS).sizeHint().height()
                top += self.main_tabs.tabBar().sizeHint().height() + 12
                bottom = self.split.widget(1).minimumSizeHint().height()
                short = top + bottom + self.split.handleWidth() - total
                if short > 0 and self.isVisible():
                    screen = self.screen().availableGeometry().height()
                    grow = min(short, max(0, screen - self.height()))
                    if grow:
                        self.resize(self.width(), self.height() + grow)
                        total += grow
            else:
                top = int(total * 0.28)
            self.split.setSizes([top, total - top])
        # Switching tabs mid-run must not clear the summary or re-enable Start.
        if self.scan_worker and self.scan_worker.isRunning():
            return
        self.summary_label.setText("")
        if hydrus:
            self.start_btn.setEnabled(True)
            if not self.hydrus_panel.has_services() and self.integrator.has_hydrus:
                self._refresh_hydrus_services()
            self._refresh_scan_preview()
        else:
            self.start_btn.setEnabled(bool(self.inventory))

    def _refresh_scan_preview(self) -> None:
        if self._scan_source() != "hydrus":
            return
        scan = self.hydrus_panel.to_scan_settings()
        self.hydrus_panel.set_preview(
            self.integrator.hydrus_scan_predicates(scan))

    def _refresh_hydrus_services(self) -> None:
        """Ask Hydrus which file domains and tag services exist.

        The API is local, so this runs inline rather than on a worker.
        """
        if not self.integrator.has_hydrus:
            QMessageBox.information(
                self, "Hydrus", "Connect to Hydrus first "
                "(Hydrus → Credentials…, then Reconnect).")
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            domains = self.integrator.hydrus_file_services()
            tag_services = self.integrator.hydrus_tag_services()
        finally:
            QGuiApplication.restoreOverrideCursor()
        if not domains:
            self._add_issue("Hydrus returned no file domains.")
            return
        self.hydrus_panel.set_services(domains, tag_services)
        self._log(
            f"Hydrus: {len(domains)} file domain(s), "
            f"{len(tag_services)} tag service(s).")

    def _start_hydrus_scan(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            return
        s = self.settings_panel.to_settings()
        scan = self.hydrus_panel.to_scan_settings()
        s.hydrus_scan = scan
        # Keep the panel's copy authoritative so Save as default persists it.
        self.settings_panel.set_hydrus_scan(scan)
        self.settings.hydrus_scan = scan
        self.integrator.apply_settings(s)
        self.integrator.cancel_event.clear()
        self.integrator.load_credentials_from_store(self.cred_store)
        self._refresh_source_status()

        problems: List[str] = []
        if not self.integrator.has_hydrus:
            problems.append(
                "Hydrus is not connected. A database scan reads from and "
                "writes to Hydrus, so there is nothing to scan without it.")
        elif not self.integrator.hydrus_can_search_files:
            problems.append(
                "The Hydrus access key needs the 'Search for and Fetch "
                "Files' permission.")
        if not self.integrator.enabled_hash_services():
            problems.append(
                "No exact-hash source is enabled and available. Enable e621, "
                "InkBunny, Danbooru, or Gelbooru and check its credentials.")
        if (scan.include_deleted_duplicates
                and not self.integrator.hydrus_can_manage_relationships):
            self._add_issue(
                "Deleted-duplicate lookups need the Hydrus 'Manage File "
                "Relationships' permission; kept files will still be scanned.")
        if problems:
            QMessageBox.warning(self, "Cannot start", "\n\n".join(problems))
            return

        if not scan.dry_run and scan.limit == 0:
            answer = QMessageBox.question(
                self, "Scan the whole domain?",
                "No file cap is set, so this will look up every matching file "
                "in the domain and write tags to Hydrus as it goes.\n\n"
                "Consider a dry run, or a cap, for the first pass. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.cancel_event = threading.Event()
        self._set_running(True)
        self.summary_label.setText("")
        self._set_source_totals({})
        self.issues.clear()
        self._last_report_path = ""
        self.open_report_btn.setEnabled(False)
        self._log(
            "Starting Hydrus database scan"
            + (" (dry run)" if scan.dry_run else "")
            + f" — {scan.file_service_name or 'default domain'}")
        self.scan_worker = HydrusScanWorker(
            self.integrator, scan, self.cancel_event, self.bridge)
        self.scan_worker.start()

    def _hydrus_summary_text(self, summary: ScanSummary) -> str:
        label = "CANCELLED" if summary.cancelled else "DONE"
        verb = "would tag" if self.hydrus_panel.dry_run.isChecked() else "tagged"
        parts = [
            f"{label} ({summary.stop_reason or 'completed'}): "
            f"{summary.scanned} looked up · {verb} {summary.tagged} · "
            f"no match {summary.unmatched} · retryable errors "
            f"{summary.errors} · skipped {summary.skipped}"
        ]
        if summary.deleted_duplicates_examined:
            parts.append(
                f"{summary.matched_via_deleted_duplicate} matched only via a "
                f"deleted duplicate ({summary.deleted_duplicates_examined} "
                f"looked up)")
        return " · ".join(parts)

    def _open_scan_report(self) -> None:
        if not self._last_report_path:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_report_path))

    def _make_track_card(self, title: str) -> dict:
        box = QGroupBox(title)
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(2)
        current = QLabel("—")
        current.setWordWrap(True)
        sub = QLabel("")
        sub.setWordWrap(True)
        bar = QProgressBar()
        bar.setRange(0, 1)
        bar.setValue(0)
        bar.setTextVisible(True)
        eta = QLabel("")
        lay.addWidget(current)
        lay.addWidget(sub)
        lay.addWidget(bar)
        lay.addWidget(eta)
        return {"box": box, "current": current, "sub": sub, "bar": bar, "eta": eta,
                "start": None, "total": 0, "done": 0}

    def _refresh_source_status(self) -> None:
        self.integrator.apply_settings(self.settings_panel.to_settings())
        status = self.integrator.source_status_map()
        # Green = on, red = off/unavailable — filled dots read as status lights,
        # not radio buttons (the old white ● / empty ○ looked clickable).
        style = {
            "active": ("●", "#3dd68c"),
            "disabled": ("●", "#f07178"),
            "unavailable": ("✗", "#f07178"),
        }
        parts = []
        for s, st in status.items():
            symbol, color = style.get(st, ("?", "#aaa"))
            parts.append(
                f'<span style="color:{color}">{symbol}</span> {s} ({st})')
        if self.integrator.has_hydrus:
            hydrus = '<span style="color:#3dd68c">Hydrus ✓</span>'
        else:
            hydrus = '<span style="color:#f07178">Hydrus ✗</span>'
        self.status_label.setText(f"{hydrus}  ·  " + "  ".join(parts))

    def _refresh_recent_folders(self) -> None:
        self.recent_folders.blockSignals(True)
        self.recent_folders.clear()
        self.recent_folders.addItem("Choose a recent folder…", "")
        for raw in self.settings.history.recent_scan_paths:
            path = Path(raw)
            label = str(path)
            if not path.is_dir():
                label += "  (unavailable)"
            self.recent_folders.addItem(label, str(path))
            self.recent_folders.setItemData(
                self.recent_folders.count() - 1, str(path),
                Qt.ItemDataRole.ToolTipRole)
        self.recent_folders.setCurrentIndex(0)
        self.clear_recents_btn.setEnabled(
            bool(self.settings.history.recent_scan_paths))
        self.recent_folders.blockSignals(False)

    def _load_last_used_folder(self) -> None:
        """Pre-select the most recently scanned folder on launch, if it's
        still available, so the user doesn't have to re-browse or re-pick
        it from the Recent dropdown every session."""
        for raw in self.settings.history.recent_scan_paths:
            path = Path(raw)
            if path.is_dir():
                self._set_folder(str(path), auto_index=False)
                return

    def _remember_folder(self, folder: Path) -> None:
        """Persist MRU history without saving unsaved Settings-tab edits."""
        persisted = self.settings_store.load()
        paths = remember_scan_path(
            persisted.history.recent_scan_paths, folder)
        persisted.history.recent_scan_paths = paths
        self.settings.history.recent_scan_paths = list(paths)
        self.settings_panel.set_recent_scan_paths(paths)
        try:
            self.settings_store.save(persisted)
        except OSError as e:
            self._add_issue(f"Could not save recent folders: {e}")
        self._refresh_recent_folders()

    def _clear_recent_folders(self) -> None:
        answer = QMessageBox.question(
            self, "Clear recent folders",
            "Forget all recently selected scan folders?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        persisted = self.settings_store.load()
        persisted.history.recent_scan_paths = []
        try:
            self.settings_store.save(persisted)
        except OSError as e:
            self._add_issue(f"Could not clear recent folders: {e}")
            return
        self.settings.history.recent_scan_paths = []
        self.settings_panel.set_recent_scan_paths([])
        self._refresh_recent_folders()

    @Slot(int)
    def _select_recent_folder(self, index: int) -> None:
        path = self.recent_folders.itemData(index)
        self.recent_folders.setCurrentIndex(0)
        if not path:
            return
        if not Path(path).is_dir():
            QMessageBox.information(
                self, "Folder unavailable",
                "That recent folder is not currently available. It will remain "
                "in the list in case a removable or network volume reconnects.")
            return
        self._set_folder(str(path))

    def _set_folder(self, path: str, *, auto_index: bool = True) -> None:
        """Select *path*. With ``auto_index=False`` the folder is only shown —
        indexing waits for the Index button. Startup uses that so restoring the
        last folder cannot grey out the whole window before it is usable."""
        if self.scan_worker and self.scan_worker.isRunning():
            QMessageBox.information(
                self, "Scan running",
                "Finish or cancel the current scan before changing folders.")
            return
        folder = Path(path).expanduser().resolve(strict=False)
        if not folder.is_dir():
            QMessageBox.warning(
                self, "Folder unavailable",
                f"Cannot scan this folder because it is not available:\n{folder}")
            return
        self.folder = folder
        # Bump first so any in-flight worker for the previous folder is stale.
        self._folder_generation += 1
        self.drop.setText(str(self.folder))
        self._remember_folder(self.folder)
        self._refresh_review_badge()
        self.inventory = None
        self.start_btn.setEnabled(False)
        if not auto_index:
            self.inventory_label.setText(
                "Not indexed yet — press Index to count what is here.")
            self.index_btn.setEnabled(True)
            return
        self._begin_discovery()

    def _begin_discovery(self) -> None:
        """Index the selected folder on a worker thread."""
        if not self.folder or not self.folder.is_dir():
            return
        if self.scan_worker and self.scan_worker.isRunning():
            return
        self._folder_generation += 1
        generation = self._folder_generation
        self.inventory = None
        self.inventory_label.setText("Indexing…")
        self.start_btn.setEnabled(False)
        self._set_indexing(True)
        # Honor current Settings-tab toggles (e.g. PDF off) during discovery.
        self.integrator.apply_settings(self.settings_panel.to_settings())
        worker = DiscoverWorker(
            self.integrator, self.folder, self.bridge, generation)
        self.discover_workers.append(worker)
        worker.finished.connect(
            lambda worker=worker: self._discovery_finished(worker))
        worker.start()

    def _browse_folder(self) -> None:
        start = self.folder if self.folder and self.folder.is_dir() else None
        if start is None:
            start = next((
                Path(raw) for raw in self.settings.history.recent_scan_paths
                if Path(raw).is_dir()), Path.home())
        d = QFileDialog.getExistingDirectory(
            self, "Folder to scan", str(start))
        if d:
            self._set_folder(d)

    @Slot(object)
    def _on_inventory(self, payload: object) -> None:
        generation, root, inv = payload
        if generation != self._folder_generation or root != self.folder:
            return
        self.inventory = inv
        n = len(inv["items"])
        pdf_on = self.settings_panel.pdf_enabled.isChecked()
        pdfs = len(inv["pdf_jobs"]) if pdf_on else 0
        if pdf_on:
            self.inventory_label.setText(
                f"To process: {n}  ·  PDFs to render: {pdfs}")
        else:
            self.inventory_label.setText(
                f"To process: {n}  ·  PDF rendering disabled")
        self.start_btn.setEnabled(True)
        self._log(
            f"Indexed {n} file(s)"
            + (f", {pdfs} PDF job(s)." if pdf_on else " (PDF rendering off)."))

    @Slot(object)
    def _on_inventory_failed(self, payload: object) -> None:
        generation, root, msg = payload
        if generation != self._folder_generation or root != self.folder:
            return
        self.inventory = None
        self.inventory_label.setText("Indexing failed.")
        self.start_btn.setEnabled(False)
        self._add_issue(msg)
        QMessageBox.critical(self, "Folder indexing failed", msg)

    def _discovery_finished(self, worker: DiscoverWorker) -> None:
        was_current = worker.generation == self._folder_generation
        try:
            self.discover_workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()
        if was_current:
            self._set_indexing(False)
        if self._closing and not self.discover_workers and not (
                self.scan_worker and self.scan_worker.isRunning()):
            self.close()

    def _set_indexing(self, indexing: bool) -> None:
        if (not indexing and self.scan_worker
                and self.scan_worker.isRunning()):
            return
        self.browse_btn.setEnabled(not indexing)
        self.index_btn.setEnabled(not indexing and self.folder is not None)
        self.drop.setEnabled(not indexing)
        self.recent_folders.setEnabled(not indexing)
        self.clear_recents_btn.setEnabled(
            not indexing and bool(self.settings.history.recent_scan_paths))
        self.reset_btn.setEnabled(not indexing)
        self.another_btn.setEnabled(not indexing)
        self.settings_panel.setEnabled(not indexing)

    def _start(self) -> None:
        if self._scan_source() == "hydrus":
            self._start_hydrus_scan()
            return
        if not self.folder:
            QMessageBox.information(self, "Start", "Choose a folder first.")
            return
        if self.scan_worker and self.scan_worker.isRunning():
            return

        s = self.settings_panel.to_settings()
        self.integrator.apply_settings(s)
        # A completed/cancelled worker leaves its event on the integrator.
        # Credential probes (notably InkBunny login) also consult cancelled(),
        # so clear the previous run before reloading credentials or a cancelled
        # scan can make a healthy source look unavailable on the next run.
        self.integrator.cancel_event.clear()
        # Reload credentials in case user updated them
        self.integrator.load_credentials_from_store(self.cred_store)
        self._refresh_source_status()

        errs = validate_run_preflight(
            s,
            hydrus_available=self.integrator.has_hydrus,
            any_source_available=self.integrator.any_source_available(),
        )
        if errs:
            QMessageBox.warning(self, "Cannot start", "\n".join(errs))
            return

        opts = RunOptions.from_settings(s)
        opts.import_unmatched = self.opt_import_unmatched.isChecked()
        opts.sync_sidecars = self.opt_sync_sidecars.isChecked()
        # Always set, so the engine never falls through to an interactive prompt.
        # The PDF-quality dialog below may override it.
        opts.pdf_dpi = s.pdf.pdf_dpi
        opts.settings_override = s

        # PDF quality + comic/artist tags only when rendering is enabled AND
        # jobs remain. Re-discover under current settings so a disabled PDF
        # toggle is honored even if inventory was built while rendering was on.
        inv = self.inventory or {}
        pdf_jobs = inv.get("pdf_jobs") or []
        if s.pdf.pdf_enabled and pdf_jobs:
            dpi, ok = QInputDialog.getInt(
                self, "PDF quality",
                f"{len(pdf_jobs)} PDF(s) need rendering.\nDPI:",
                value=s.pdf.pdf_dpi, minValue=72, maxValue=2400)
            if not ok:
                return
            opts.pdf_dpi = dpi
            meta_dlg = PdfMetaDialog(self, list(pdf_jobs))
            if meta_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            opts.pdf_meta = meta_dlg.meta_map()

        self.cancel_event = threading.Event()
        self._set_running(True)
        self.summary_label.setText("")
        self._set_source_totals({})
        self.issues.clear()
        self._log(f"Starting scan of {self.folder}")
        self.scan_worker = ScanWorker(
            self.integrator, self.folder, opts, self.cancel_event, self.bridge)
        self.scan_worker.start()

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.integrator.request_cancel()
        self._log("Cancel requested — finishing current request…")
        self.cancel_btn.setEnabled(False)

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.hydrus_panel.setEnabled(not running)
        # The tab bar is the mode switch now: changing modes mid-run would
        # point Cancel and the summary at a scan that is not the live one.
        self.main_tabs.tabBar().setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.reset_btn.setEnabled(not running)
        self.settings_panel.setEnabled(not running)
        self.another_btn.setEnabled(not running)
        self.browse_btn.setEnabled(not running)
        self.drop.setEnabled(not running)
        self.recent_folders.setEnabled(not running)
        self.clear_recents_btn.setEnabled(
            not running and bool(self.settings.history.recent_scan_paths))
        # Both re-open connections the running scan is using.
        self.creds_btn.setEnabled(not running)
        self.reconnect_btn.setEnabled(not running)

    @Slot(object)
    def _on_event(self, event: RunEvent) -> None:
        if event.kind == "issue":
            self._add_issue(event.message)
            return
        if event.kind == "log":
            # Informational/success (BF-12) — run log only, not the issue pane.
            if event.message:
                self._log(event.message)
            return
        if event.kind == "index_progress":
            # Indexing runs before a scan, so it owns the inventory line.
            self.inventory_label.setText(
                f"Indexing… {event.index:,} folders · "
                f"{event.total:,} media found")
            return
        if event.kind == "print" and event.message:
            self._log(event.message)
            return
        if event.kind == "sidecar_sync":
            x = event.extra
            counts = (
                f"completed {x.get('successful', 0)} · "
                f"already synced {x.get('skipped', 0)} · "
                f"failed {x.get('failed', 0)}")
            current = event.current or event.message
            self.summary_label.setText(
                f"SYNCING SIDECARS {event.index}/{event.total}: "
                f"{current} · {counts}")
            if event.extra.get("checkpoint") or event.extra.get("final"):
                self._log(event.message)
            return
        if event.source_hits:
            self._set_source_totals(event.source_hits)
        card = self.hash_card if event.track == "hash" else self.perc_card
        if event.kind == "begin_phase":
            card["total"] = event.total
            card["done"] = 0
            card["start"] = time.monotonic()
            card["bar"].setRange(0, max(1, event.total))
            card["bar"].setValue(0)
            card["box"].setTitle(event.phase or event.track)
        elif event.kind == "start_file":
            card["current"].setText(event.current or "—")
            card["sub"].setText(event.sub or "")
            if event.total:
                card["bar"].setRange(0, event.total)
            card["bar"].setValue(event.index)
        elif event.kind == "status":
            card["sub"].setText(event.sub or event.message)
        elif event.kind == "finish_file":
            card["done"] = event.index or card["done"] + 1
            card["bar"].setValue(card["done"])
            card["sub"].setText(event.result or event.message)
            if card["start"] and card["total"]:
                elapsed = time.monotonic() - card["start"]
                card["eta"].setText(f"elapsed {int(elapsed)}s · {card['done']}/{card['total']}")
        elif event.kind == "grow":
            card["total"] = card["total"] + int(event.extra.get("by") or 1)
            card["bar"].setRange(0, max(1, card["total"]))
        elif event.kind == "freeze_total":
            pass
        # The engine already tells us when a file was queued for review, so
        # count from the event stream rather than re-reading the queue file
        # on the UI thread for every progress tick.
        if event.extra.get("pending_review"):
            self._review_count += 1
            self._set_review_badge(self._review_count)

    @Slot(object)
    def _on_finished(self, summary: ScanSummary) -> None:
        self._set_running(False)
        self._set_source_totals(summary.source_hits)
        if self._scan_source() == "hydrus":
            self.summary_label.setText(self._hydrus_summary_text(summary))
            self._log(self.summary_label.text())
            self._last_report_path = summary.report_path
            self.open_report_btn.setEnabled(bool(summary.report_path))
            if summary.report_path:
                self._log(f"Report: {summary.report_path}")
            if self._closing and not self.discover_workers:
                self.close()
            return
        label = "CANCELLED" if summary.cancelled else "DONE"
        self.summary_label.setText(
            f"{label}: tagged {summary.tagged} · unmatched {summary.unmatched} · "
            f"duplicates {summary.duplicates} · pending review {summary.pending_review}")
        self._log(self.summary_label.text())
        # Authoritative count at end of run — reconcile the event-driven tally.
        self._review_count = summary.pending_review
        self._set_review_badge(self._review_count)
        if self.settings.output.show_run_stats_dialog:
            QMessageBox.information(
                self, "FurTag run stats", self._run_stats_text(summary))
        if self._closing and not self.discover_workers:
            self.close()

    @staticmethod
    def _run_stats_text(summary: ScanSummary) -> str:
        """Human-friendly post-run numbers; all tag counts are this run only."""
        label = "Cancelled — results so far" if summary.cancelled else "Finished"
        counts = summary.tag_counts
        # These are technically tags, but not the kind of thing that makes a
        # recap feel like a recap. Keep the leaderboard for actual scene,
        # wardrobe, prop, setting, and style details instead of anatomy/porn
        # taxonomy, broad species/body labels, ratings, and basic composition.
        generic = {
            "male", "female", "intersex", "ambiguous_gender", "solo",
            "duo", "group", "sex", "oral", "anal", "vaginal",
            "masturbation", "cum", "cum_in_mouth", "cum_on_body",
            "penetration", "erection", "genitalia", "penis", "vagina",
            "breasts", "nipple", "ass", "butt", "testicles", "balls",
            "anus", "pussy", "cock", "dick", "gape", "sex_toy",
            "furry", "anthro", "feral", "human", "mammal", "canine",
            "feline", "equine", "dragon", "wolf", "fox", "cat", "dog",
            "explicit", "questionable", "safe", "rating", "hi_res",
            "lowres", "highres", "animated", "video", "sound", "flash",
            "image", "digital_media", "simple_background", "white_background",
            "black_background", "looking_at_viewer", "open_mouth", "smile",
            "standing", "sitting", "lying", "kneeling", "close-up",
            "close_up", "from_behind", "spread_legs", "tail", "fur",
            "scalie", "big_breasts", "big_penis", "hair", "clothing",
        }
        anatomy_words = {
            "penis", "vagina", "genital", "breast", "nipple", "anus",
            "testicle", "scrotum", "cum", "semen", "dick", "cock",
            "pussy", "butt", "ass", "oral", "anal", "sex", "penetrat",
        }

        def top(namespace: str, n: int = 1) -> str:
            prefix = namespace + ":"
            rows = [(tag[len(prefix):], count) for tag, count in counts.items()
                    if tag.startswith(prefix)]
            rows.sort(key=lambda row: (-row[1], row[0].casefold()))
            return ", ".join(f"{name} ({count})" for name, count in rows[:n]) or "none"

        def is_interesting(tag: str, count: int) -> bool:
            folded = tag.casefold()
            words = set(folded.replace("-", "_").split("_"))
            if ":" in folded or folded in generic or words & anatomy_words:
                return False
            # A tag occurring on almost everything is usually a library-wide
            # descriptor, not a fun fact about this particular run.
            return count < max(6, summary.tagged * 0.65)

        interesting = [(tag, count) for tag, count in counts.items()
                       if is_interesting(tag, count)]
        interesting.sort(key=lambda row: (-row[1], row[0].casefold()))
        top_tags = ", ".join(
            f"{tag} ({count})" for tag, count in interesting[:5]) or "none"
        return (
            f"{label}\n\n"
            f"Tagged media: {summary.tagged}\n"
            f"  Images: {summary.tagged_images}\n"
            f"  Videos: {summary.tagged_videos}\n"
            f"Duplicate copies inherited tags: {summary.duplicate_copies_tagged}\n"
            f"Tag assignments generated: {summary.tag_assignments}\n"
            f"Unmatched: {summary.unmatched}\n"
            f"Pending review: {summary.pending_review}\n\n"
            f"Top interesting tags: {top_tags}\n"
            f"Top artist: {top('artist') if top('artist') != 'none' else top('creator')}\n"
            f"Top series: {top('copyright') if top('copyright') != 'none' else top('series')}\n"
            f"Top character: {top('character')}\n\n"
            f"Source hits: " + (
                ", ".join(f"{name} {count}" for name, count in
                          sorted(summary.source_hits.items()) if count)
                or "none"))

    @Slot(str)
    def _on_failed(self, msg: str) -> None:
        self._set_running(False)
        self._add_issue(msg)
        QMessageBox.critical(self, "Error", msg)
        if self._closing and not self.discover_workers:
            self.close()

    def _add_issue(self, msg: str) -> None:
        self.issues.insertItem(0, msg)
        while self.issues.count() > 50:
            self.issues.takeItem(self.issues.count() - 1)
        self._log(msg)

    def _log(self, msg: str) -> None:
        self.log.append(msg)

    def _set_source_totals(self, counts: dict) -> None:
        labels = (
            ("e621", "e621"),
            ("inkbunny", "InkBunny"),
            ("danbooru", "Danbooru"),
            ("gelbooru", "Gelbooru"),
            ("fluffle", "Fluffle"),
            ("saucenao", "SauceNAO"),
            ("furarchiver", "FurArchiver"),
        )
        self.source_totals_label.setText(
            "Tagged files by source · "
            + "  ·  ".join(
                f"{label}: {int(counts.get(key, 0))}"
                for key, label in labels))

    def _set_review_badge(self, count: int) -> None:
        self.review_badge.setText(f"Needs review — {count}")

    def _refresh_review_badge(self) -> None:
        """Re-read the queue file. Only for folder changes and dialog close."""
        if not self.folder:
            self._review_count = 0
        else:
            rq = ReviewQueue(self.folder)
            rq.load()
            self._review_count = len(rq)
        self._set_review_badge(self._review_count)

    def _open_review(self) -> None:
        if not self.folder:
            return
        dlg = ReviewDialog(self.integrator, self.folder, self)
        dlg.exec()
        self._refresh_review_badge()

    def _scan_another(self) -> None:
        self._folder_generation += 1  # ignore any late discovery result
        self.folder = None
        self.inventory = None
        self._refresh_review_badge()
        self.drop.setText("Drop a folder here, or use Browse…")
        self.inventory_label.setText("Choose a folder to scan.")
        self.summary_label.setText("")
        self.start_btn.setEnabled(False)
        self.index_btn.setEnabled(False)

    def _reveal(self) -> None:
        if self.folder and self.folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def _reset(self) -> None:
        dlg = ResetDialog(self, settings=self.settings_panel.to_settings())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            n, failures = dlg.perform_reset()
            msg = f"Removed {n} file(s)."
            if failures:
                msg += (f"\n\n{len(failures)} file(s) could not be removed and "
                        "may still be skipped:\n"
                        + "\n".join(f"· {p}: {e}" for p, e in failures[:10]))
            QMessageBox.information(self, "Reset", msg)
            if dlg.root:
                self._set_folder(str(dlg.root))

    def _edit_credentials(self) -> None:
        dlg = CredentialsDialog(self.cred_store, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.integrator = TagIntegrator(
                settings=self.settings_panel.to_settings())
            self.integrator.load_credentials_from_store(self.cred_store)
            self._refresh_source_status()

    def _reconnect_sources(self) -> None:
        """Manually re-check Hydrus/source availability without restarting."""
        self.integrator.load_credentials_from_store(self.cred_store)
        self._refresh_source_status()
        self._log("Rechecked Hydrus and source connections.")

    def closeEvent(self, event) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            self._closing = True
            self._cancel()
            self._log("Finishing current request before quit…")
            event.ignore()
            return
        if self.discover_workers:
            self._closing = True
            self._log("Finishing folder indexing before quit…")
            event.ignore()
            return
        event.accept()


def main() -> None:
    # High-DPI policy must be selected before constructing QApplication.
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    # Avoid Qt plugin issues when packaged
    app = QApplication(sys.argv)
    app.setApplicationName("FurTag")
    app.setOrganizationName("FurTag")
    app.setOrganizationDomain("furtag.org")
    win = MainWindow()
    win.show()

    def _clamp_if_overflow() -> None:
        geo = _available_screen_rect()
        if geo is None:
            return
        # Frame geometry includes the macOS title bar; content alone can look fine
        # while the window still hangs under the Dock.
        frame = win.frameGeometry()
        if (frame.width() > geo.width() - 16 or frame.height() > geo.height() - 16
                or frame.top() < geo.top() or frame.left() < geo.left()):
            _fit_window_to_screen(win, prefer_w=900, prefer_h=640, recenter=True)

    QTimer.singleShot(0, _clamp_if_overflow)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
