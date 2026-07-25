#!/usr/bin/env python3
"""FurTag desktop GUI — thin PySide6 adapter over the furtag.py engine.

Run:  python furtag_gui.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
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
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QProgressBar, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget, QInputDialog, QFrame, QSizePolicy,
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

from furtag import (
    TagIntegrator, prompt_for_pdf_dpi, _nuke_candidates, _pdf_render_candidates,
    _is_furtag_sidecar, LEDGER_FILE, DUPLICATES_FILE,
    is_filesystem_root, perform_nuke, set_active_observer,
)
from furtag_settings import (
    Settings, SettingsStore, RunOptions, ScanSummary, validate_run_preflight,
    validate_output_patterns, SidecarPatternError, PACE_FLOORS,
    FLUFFLE_MATCH_CLASSES, FLUFFLE_REVIEW_MODES, DEFAULT_PDF_DPI,
    DEFAULT_PDF_ARCHIVAL_DPI,
)
from furtag_credentials import CredentialStore, ALL_FIELDS, SECRET_FIELDS, FIELD_MAP
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


class DiscoverWorker(QThread):
    def __init__(self, integrator: TagIntegrator, root: Path,
                 bridge: QtEventBridge) -> None:
        super().__init__()
        self.integrator = integrator
        self.root = root
        self.bridge = bridge

    def run(self) -> None:
        try:
            inv = self.integrator.discover(self.root)
            self.bridge.inventory_ready.emit(inv)
        except Exception as e:
            self.bridge.failed.emit(str(e))


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
        status = QLabel(msg if usable else f"⚠️ {msg}\nEnv vars (FURTAG_*) still work.")
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
        if QMessageBox.question(
                self, "Remove credentials",
                "Remove all FurTag secrets from the OS keyring?") != QMessageBox.StandardButton.Yes:
            return
        for k in ALL_FIELDS:
            self.store.delete(k)
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
            "Permanently remove FurTag ledgers, sidecars, reports, and "
            "optionally rendered PDF page PNGs. Source PDFs are never deleted."))
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
        self.include_pdf_pages = QCheckBox("Also remove rendered PDF page PNGs")
        layout.addWidget(self.include_pdf_pages)
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
        self.preview.setText(
            f"Would remove:\n"
            f"  · {len(ledgers)} ledger/report file(s)\n"
            f"  · {len(sidecars)} sidecar file(s)\n"
            f"  · {len(pages)} rendered PDF page file(s) (if checked)")

    def _confirm(self) -> None:
        p = Path(self.path_edit.text().strip()).expanduser().resolve()
        if not p.is_dir():
            QMessageBox.warning(self, "Reset", "Not a valid directory.")
            return
        if is_filesystem_root(p):
            QMessageBox.warning(self, "Reset", "Refusing filesystem root.")
            return
        # Two confirmations, second typed
        if QMessageBox.question(
                self, "Confirm reset",
                f"Reset all FurTag data under:\n{p}\n\nContinue?") != QMessageBox.StandardButton.Yes:
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
            settings=self.settings)


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
        self.integrator.resolve_pending_review(p, approve=approve, root=self.root)
        self._reload()

    def _bulk(self, approve: bool) -> None:
        for p in list(self.queue.list_items()):
            self.integrator.resolve_pending_review(p, approve=approve, root=self.root)
        self._reload()

    def _open_url(self) -> None:
        p = self._current()
        if p and p.location:
            webbrowser.open(p.location)


# ── Settings tabs ────────────────────────────────────────────────────────────

class SettingsPanel(QWidget):
    """Tabbed settings editor with Save as default / Restore defaults."""

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
        of.addRow("Sidecar format", self.sidecar_format)
        of.addRow("Tag filename", self.sidecar_tag_fn)
        of.addRow("URL filename", self.sidecar_url_fn)
        of.addRow("JSON filename", self.sidecar_json_fn)
        self._add_tab("Output", out)

        # Hydrus pages
        hy = QWidget()
        hf = QFormLayout(hy)
        self.results_pages = QCheckBox("Enable result pages")
        self.new_imports_name = QLineEdit()
        self.newly_tagged_name = QLineEdit()
        self.duplicate_tagged_name = QLineEdit()
        self.already_tagged_name = QLineEdit()
        self.build_already = QCheckBox("Build Already Tagged page")
        self.page_limit = QSpinBox()
        self.page_limit.setRange(0, 1_000_000)
        self.page_limit.setSpecialValueText("Unlimited")
        hf.addRow(self.results_pages)
        hf.addRow("New Imports name", self.new_imports_name)
        hf.addRow("Newly Tagged name", self.newly_tagged_name)
        hf.addRow("Duplicate Tagged name", self.duplicate_tagged_name)
        hf.addRow("Already Tagged name", self.already_tagged_name)
        hf.addRow(self.build_already)
        hf.addRow("Page limit (0=all)", self.page_limit)
        self._add_tab("Hydrus", hy)

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
        self.pdf_write_sc = QCheckBox("Write comic:/page: base sidecars")
        pf.addRow(self.pdf_enabled)
        pf.addRow("Default DPI", self.pdf_dpi)
        pf.addRow(self.pdf_write_sc)
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

    def load_from(self, s: Settings) -> None:
        o, h, src, m, p, perf = (
            s.output, s.hydrus, s.sources, s.matching, s.pdf, s.performance)
        self.hydrus_enabled.setChecked(o.hydrus_enabled)
        self.hydrus_import.setChecked(o.hydrus_import)
        self.hydrus_import_unmatched.setChecked(o.hydrus_import_unmatched)
        self.hydrus_tag_service.setText(o.hydrus_tag_service)
        self.hydrus_tag_deleted.setChecked(o.hydrus_tag_deleted_duplicates)
        self.sidecars_enabled.setChecked(o.sidecars_enabled)
        self.sidecar_format.setCurrentText(o.sidecar_format)
        self.sidecar_tag_fn.setText(o.sidecar_tag_filename)
        self.sidecar_url_fn.setText(o.sidecar_url_filename)
        self.sidecar_json_fn.setText(o.sidecar_json_filename)
        self.results_pages.setChecked(h.results_pages_enabled)
        self.new_imports_name.setText(h.new_imports_page_name)
        self.newly_tagged_name.setText(h.newly_tagged_page_name)
        self.duplicate_tagged_name.setText(h.duplicate_tagged_page_name)
        self.already_tagged_name.setText(h.already_tagged_page_name)
        self.build_already.setChecked(h.build_already_tagged_page)
        self.page_limit.setValue(h.result_page_limit)
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
        s = Settings()
        s.output.hydrus_enabled = self.hydrus_enabled.isChecked()
        s.output.hydrus_import = self.hydrus_import.isChecked()
        s.output.hydrus_import_unmatched = self.hydrus_import_unmatched.isChecked()
        s.output.hydrus_tag_service = self.hydrus_tag_service.text().strip() or "downloader tags"
        s.output.hydrus_tag_deleted_duplicates = self.hydrus_tag_deleted.isChecked()
        s.output.sidecars_enabled = self.sidecars_enabled.isChecked()
        s.output.sidecar_format = self.sidecar_format.currentText()
        s.output.sidecar_tag_filename = self.sidecar_tag_fn.text().strip()
        s.output.sidecar_url_filename = self.sidecar_url_fn.text().strip()
        s.output.sidecar_json_filename = self.sidecar_json_fn.text().strip()
        s.hydrus.results_pages_enabled = self.results_pages.isChecked()
        s.hydrus.new_imports_page_name = self.new_imports_name.text().strip()
        s.hydrus.newly_tagged_page_name = self.newly_tagged_name.text().strip()
        s.hydrus.duplicate_tagged_page_name = self.duplicate_tagged_name.text().strip()
        s.hydrus.already_tagged_page_name = self.already_tagged_name.text().strip()
        s.hydrus.build_already_tagged_page = self.build_already.isChecked()
        s.hydrus.result_page_limit = self.page_limit.value()
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

    def _save_defaults(self) -> None:
        try:
            s = self.to_settings()
            validate_output_patterns(s.output)
        except SidecarPatternError as e:
            QMessageBox.warning(self, "Invalid pattern", str(e))
            return
        SettingsStore().save(s)
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
        self.scan_worker: Optional[ScanWorker] = None
        self.discover_worker: Optional[DiscoverWorker] = None
        self.cancel_event = threading.Event()
        self.bridge = QtEventBridge()
        self.bridge.event.connect(self._on_event)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.inventory_ready.connect(self._on_inventory)
        self._closing = False
        self._review_count = 0

        self._build_ui()
        _fit_window_to_screen(self, prefer_w=900, prefer_h=640)
        # Route engine warnings (notify()) into the issue pane for the whole
        # session, not just during a scan — credential / Hydrus problems are
        # reported while loading credentials, well before any run starts. The
        # UI must exist first, since the bridge delivers in-thread signals
        # synchronously when emitted from the GUI thread.
        set_active_observer(QtObserver(self.bridge))
        self.integrator.load_credentials_from_store(self.cred_store)
        self._refresh_source_status()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Menu
        file_menu = self.menuBar().addMenu("&File")
        act_creds = QAction("Credentials…", self)
        act_creds.triggered.connect(self._edit_credentials)
        act_settings = QAction("Settings…", self)
        act_settings.triggered.connect(lambda: self.main_tabs.setCurrentIndex(1))
        act_quit = QAction("Quit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_creds)
        file_menu.addAction(act_settings)
        file_menu.addSeparator()
        file_menu.addAction(act_quit)

        # Source status (always visible)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status_label)

        # Top-level Scan | Settings — keeps the window short enough for laptops
        self.main_tabs = QTabWidget()
        self.main_tabs.setDocumentMode(True)
        root.addWidget(self.main_tabs, stretch=1)

        # ── Scan tab ─────────────────────────────────────────────────────
        scan = QWidget()
        scan_lay = QVBoxLayout(scan)
        scan_lay.setContentsMargins(4, 8, 4, 4)
        scan_lay.setSpacing(6)

        folder_row = QHBoxLayout()
        self.drop = DropFolderLabel()
        self.drop.folder_dropped.connect(self._set_folder)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.drop, stretch=1)
        folder_row.addWidget(browse)
        scan_lay.addLayout(folder_row)

        self.inventory_label = QLabel("Choose a folder to scan.")
        self.inventory_label.setWordWrap(True)
        scan_lay.addWidget(self.inventory_label)

        # Session options — wrap on narrow windows via a flow-like row
        opts = QHBoxLayout()
        self.opt_import_unmatched = QCheckBox("Import unmatched")
        self.opt_sync_sidecars = QCheckBox("Sync sidecars first")
        self.opt_already = QCheckBox("Already Tagged page")
        self.opt_page_limit = QSpinBox()
        self.opt_page_limit.setRange(0, 1_000_000)
        self.opt_page_limit.setSpecialValueText("Unlimited")
        self.opt_page_limit.setMaximumWidth(100)
        self.opt_page_limit.setValue(self.settings.hydrus.result_page_limit)
        self.opt_import_unmatched.setChecked(self.settings.output.hydrus_import_unmatched)
        self.opt_already.setChecked(self.settings.hydrus.build_already_tagged_page)
        opts.addWidget(self.opt_import_unmatched)
        opts.addWidget(self.opt_sync_sidecars)
        opts.addWidget(self.opt_already)
        opts.addWidget(QLabel("Page limit:"))
        opts.addWidget(self.opt_page_limit)
        opts.addStretch()
        scan_lay.addLayout(opts)

        # Progress cards
        prog = QHBoxLayout()
        self.hash_card = self._make_track_card("Hash tier")
        self.perc_card = self._make_track_card("Perceptual tier")
        prog.addWidget(self.hash_card["box"])
        prog.addWidget(self.perc_card["box"])
        scan_lay.addLayout(prog)

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
        self.cancel_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn.clicked.connect(self._cancel)
        self.another_btn.clicked.connect(self._scan_another)
        self.reveal_btn.clicked.connect(self._reveal)
        self.reset_btn.clicked.connect(self._reset)
        for b in (self.start_btn, self.cancel_btn, self.another_btn,
                  self.reveal_btn, self.reset_btn):
            actions.addWidget(b)
        scan_lay.addLayout(actions)

        self.main_tabs.addTab(scan, "Scan")

        # ── Settings tab (scrollable pages) ──────────────────────────────
        self.settings_panel = SettingsPanel(self.settings)
        self.main_tabs.addTab(self.settings_panel, "Settings")

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
        parts = []
        for s, st in status.items():
            symbol = {"active": "●", "disabled": "○", "unavailable": "✗"}.get(st, "?")
            parts.append(f"{symbol} {s} ({st})")
        hydrus = "Hydrus ✓" if self.integrator.has_hydrus else "Hydrus ✗"
        self.status_label.setText(
            f"{hydrus}  ·  " + "  ".join(parts))

    def _set_folder(self, path: str) -> None:
        self.folder = Path(path).expanduser().resolve()
        self.drop.setText(str(self.folder))
        self._refresh_review_badge()
        self.inventory_label.setText("Indexing…")
        self.start_btn.setEnabled(False)
        # Honor current Settings-tab toggles (e.g. PDF off) during discovery.
        self.integrator.apply_settings(self.settings_panel.to_settings())
        self.discover_worker = DiscoverWorker(
            self.integrator, self.folder, self.bridge)
        self.discover_worker.start()

    def _browse_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder to scan")
        if d:
            self._set_folder(d)

    @Slot(object)
    def _on_inventory(self, inv: dict) -> None:
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

    def _start(self) -> None:
        if not self.folder:
            QMessageBox.information(self, "Start", "Choose a folder first.")
            return
        if self.scan_worker and self.scan_worker.isRunning():
            return

        s = self.settings_panel.to_settings()
        self.integrator.apply_settings(s)
        # Reload credentials in case user updated them
        self.integrator.load_credentials_from_store(self.cred_store)

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
        opts.build_already_tagged_page = self.opt_already.isChecked()
        opts.result_page_limit = self.opt_page_limit.value()
        # Always set, so the engine never falls through to an interactive prompt.
        # The PDF-quality dialog below may override it.
        opts.pdf_dpi = s.pdf.pdf_dpi
        opts.settings_override = s

        # PDF quality only when rendering is enabled AND jobs remain.
        # Re-discover under current settings so a disabled PDF toggle is honored
        # even if inventory was built while rendering was still on.
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

        self.cancel_event = threading.Event()
        self._set_running(True)
        self.summary_label.setText("")
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
        self.cancel_btn.setEnabled(running)
        self.reset_btn.setEnabled(not running)
        self.settings_panel.setEnabled(not running)
        self.another_btn.setEnabled(not running)

    @Slot(object)
    def _on_event(self, event: RunEvent) -> None:
        if event.kind == "log" or event.kind == "issue":
            self._add_issue(event.message)
            return
        if event.kind == "print" and event.message:
            self._log(event.message)
            return
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
        label = "CANCELLED" if summary.cancelled else "DONE"
        self.summary_label.setText(
            f"{label}: tagged {summary.tagged} · unmatched {summary.unmatched} · "
            f"duplicates {summary.duplicates} · pending review {summary.pending_review}")
        self._log(self.summary_label.text())
        # Authoritative count at end of run — reconcile the event-driven tally.
        self._review_count = summary.pending_review
        self._set_review_badge(self._review_count)
        if self._closing:
            self.close()

    @Slot(str)
    def _on_failed(self, msg: str) -> None:
        self._set_running(False)
        self._add_issue(msg)
        QMessageBox.critical(self, "Error", msg)
        if self._closing:
            self.close()

    def _add_issue(self, msg: str) -> None:
        self.issues.insertItem(0, msg)
        while self.issues.count() > 50:
            self.issues.takeItem(self.issues.count() - 1)
        self._log(msg)

    def _log(self, msg: str) -> None:
        self.log.append(msg)

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
        self.folder = None
        self.inventory = None
        self._refresh_review_badge()
        self.drop.setText("Drop a folder here, or use Browse…")
        self.inventory_label.setText("Choose a folder to scan.")
        self.summary_label.setText("")
        self.start_btn.setEnabled(False)

    def _reveal(self) -> None:
        if self.folder and self.folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def _reset(self) -> None:
        dlg = ResetDialog(self, settings=self.settings)
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

    def closeEvent(self, event) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            self._closing = True
            self._cancel()
            self._log("Finishing current request before quit…")
            event.ignore()
            return
        event.accept()


def main() -> None:
    # Avoid Qt plugin issues when packaged
    app = QApplication(sys.argv)
    app.setApplicationName("FurTag")
    app.setOrganizationName("FurTag")
    app.setOrganizationDomain("furtag.org")
    # High-DPI: use screen pixels sanely on Retina Macs
    try:
        app.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
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
