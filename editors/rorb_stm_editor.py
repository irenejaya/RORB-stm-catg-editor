"""
RORB STM (Storm) File Editor for QGIS
======================================
A PyQGIS tool to view and edit RORB Storm (.stm) files with:
  - Section-aware parsing that reads storm parameters first, then uses
    burst count / pluviograph count to dynamically parse the remainder.
  - Delimiter-preserving round-trip editing (tab, comma).
  - Table-style editing for every data section with add/delete support.
  - Lossless reconstruction of the original file format.

File Structure (parsed sequentially):
  Block 1  - Event Header (free text) + Model Mode (free text)
  Block 2  - Storm Parameters (comma-delimited, drives burst/pluvio counts)
  Block 3  - Burst Time Ranges (comma-delimited, inline comment preserved)
  Block 4  - Pluviograph Rainfall Data (pluvio_count stations, comma or tab)
  Block 5  - Sub-area Rainfalls (burst_count blocks, comma or tab)
  Block 6  - Pluviograph References (burst_count blocks, comma or tab)
  Block 7  - Hydrograph Data header + time ranges + N station blocks

  New files default to comma-delimited for all data tables.

Usage:
    Run from QGIS Python console:
        exec(open(r'path/to/RORB_stm_editor.py').read())
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QPushButton, QFileDialog,
    QMessageBox, QWidget, QFormLayout,
    QLineEdit, QGroupBox, QAbstractItemView,
    QProgressBar, QFrame, QScrollArea, QApplication, QInputDialog,
)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QFont, QColor, QKeySequence


# ============================================================================
#  Data Model
# ============================================================================

STORM_PARAM_LABELS = [
    "Time Increment, h (Item 7.3)",
    "No. of Time Incs. for Calcs (Item 7.4)",
    "No. of Rainfall Bursts (Item 7.5)",
    "No. of Pluviographs (Item 7.6)",
    "Areal Rainfall Flag (Item 7.7: 0=uniform, 1=variable)",
]


@dataclass
class Section:
    """One logical block of the STM file, with enough metadata for lossless save."""
    section_type: str               # identifies which block this belongs to
    delimiter: Optional[str] = None # "\t" or "," - how data values are separated
    terminator_style: str = "none"  # "inline"  -> -99 at end of data line
                                    # "own_line" -> -99 on a separate line
                                    # "none"     -> no terminator
    comment_lines: List[str]  = field(default_factory=list)  # preceding C lines
    prefix_line: str          = ""       # station ID or station name line
    suffix_lines: List[str]   = field(default_factory=list)  # e.g. "10,4,2,-99"
    data: List[str]           = field(default_factory=list)   # editable values
    inline_comment: str       = ""       # trailing comment (burst ranges line)
    raw_text: str             = ""       # for free-text sections
    label: str                = ""       # display name in tree


# ============================================================================
#  Parser - reads the STM file sequentially, section by section
# ============================================================================

class STMParser:
    """
    Parses a RORB .stm file into an ordered list of Section objects.

    The parser reads Block 2 (Storm Parameters) first to learn:
      - burst_count   - number of rainfall bursts  (drives Blocks 5, 6)
      - pluvio_count  - number of pluviographs     (drives Block 4)
    Then uses those counts to parse the rest of the file.
    """

    def __init__(self):
        self.sections: List[Section] = []
        self.burst_count: int = 0
        self.pluvio_count: int = 0
        self.duration: int = 0
        self.time_inc: int = 0

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _clean_lines(filepath: str) -> List[str]:
        """Read file and strip trailing whitespace (spreadsheet tab artefacts)."""
        with open(filepath, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        lines = [line.rstrip() for line in lines]
        # drop blank trailing lines
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    @staticmethod
    def _is_comment(line: str) -> bool:
        """Return True if line is a C-comment (starts with 'C', case-sensitive).

        RORB convention: column-1 'C' marks a comment.  Handles both
        'C <text>' (with space) and 'C<text>' (no space) variants.
        """
        return bool(line) and line[0] == "C"

    @staticmethod
    def _normalise_comment(line: str) -> str:
        """Ensure comment line has the canonical 'C ' prefix (with space).

        Converts 'Ctext' → 'C text' while leaving 'C text' untouched.
        """
        if line and line[0] == "C" and (len(line) == 1 or line[1] != " "):
            return "C " + line[1:]
        return line

    @staticmethod
    def _strip_after_99(line: str):
        """Find -99 token in a line, return (data_part, inline_comment, found).

        Everything after the -99 token on the same line is treated as a
        comment / trailing text and preserved but NOT parsed as data.
        """
        # Locate '-99' surrounded by delimiters, whitespace, or line boundaries
        import re
        m = re.search(r'(?:^|[,\t])\s*-99(?:\s|[,\t]|$)', line)
        if m is None:
            return line, "", False
        # Find where '-99' itself starts inside the match
        pos99 = line.index("-99", m.start())
        data_part = line[:m.start()].rstrip(", \t")
        trailing = line[pos99 + 3:].strip().lstrip(", \t")
        return data_part, trailing, True

    @staticmethod
    def _split_comma(line: str) -> List[str]:
        """Split a comma-delimited line, stripping each part, removing -99
        and any trailing text after -99."""
        data_part, _, _ = STMParser._strip_after_99(line)
        return [p.strip() for p in data_part.split(",") if p.strip() and p.strip() != "-99"]

    @staticmethod
    def _split_tab_data(line: str) -> List[str]:
        """Split a tab-delimited data line, stripping and removing -99."""
        data_part, _, _ = STMParser._strip_after_99(line)
        return [v.strip() for v in data_part.split("\t") if v.strip() and v.strip() != "-99"]

    @staticmethod
    def _split_data_line(line: str):
        """Auto-detect delimiter and split a data line.

        Returns (values, delimiter, has_inline_99) where:
          - values:  list of value strings (without -99)
          - delimiter: '\t' or ','
          - has_inline_99: True if -99 was at end of this line
        """
        delim = "\t" if "\t" in line else ","
        data_part, _trailing, has_inline_99 = STMParser._strip_after_99(line)
        parts = [v.strip() for v in data_part.split(delim) if v.strip()]
        values = [v for v in parts if v != "-99"]
        return values, delim, has_inline_99

    # -- main entry point -----------------------------------------------------

    def parse(self, filepath: str) -> List[Section]:
        lines = self._clean_lines(filepath)
        self.sections = []
        idx = 0
        total = len(lines)

        # -- Block 1: Event Header --
        if idx < total:
            self.sections.append(Section(
                section_type="event_header",
                raw_text=lines[idx],
                label="Event Description",
            ))
            idx += 1

        # -- Block 1b: Model Mode --
        if idx < total:
            self.sections.append(Section(
                section_type="model_mode",
                raw_text=lines[idx],
                label="Model Mode",
            ))
            idx += 1

        # -- Block 2: Storm Parameters --
        #   C-comment lines (with or without space after C) act as column
        #   headers, followed by a comma-delimited data line:
        #     time_inc, duration, burst_count, pluvio_count, flag, -99 [comment]
        comments: List[str] = []
        while idx < total and lines[idx].startswith("C"):
            comments.append(self._normalise_comment(lines[idx]))
            idx += 1

        if idx < total:
            line = lines[idx]
            data_part, trailing, _found = self._strip_after_99(line)
            data = [p.strip() for p in data_part.split(",") if p.strip()]
            inline_comment = trailing
            self.sections.append(Section(
                section_type="storm_params",
                delimiter=",",
                terminator_style="inline",
                comment_lines=list(comments),
                data=data,
                inline_comment=inline_comment,
                label="Storm Parameters",
            ))
            # Extract the structural values that drive further parsing
            self.time_inc    = float(data[0]) if len(data) > 0 else 1
            self.duration    = int(float(data[1])) if len(data) > 1 else 0
            self.burst_count = int(float(data[2])) if len(data) > 2 else 0
            self.pluvio_count = int(float(data[3])) if len(data) > 3 else 0
            idx += 1

        # -- Block 3: Burst Time Ranges --
        #   Optional C-comment line(s) followed by comma-delimited pairs
        #   (start,end per burst) with optional inline -99 and trailing comment.
        burst_comments: List[str] = []
        while idx < total and lines[idx].startswith("C"):
            burst_comments.append(self._normalise_comment(lines[idx]))
            idx += 1
        if idx < total:
            line = lines[idx]
            inline_comment = ""
            if "-99" in line:
                pos = line.index("-99")
                inline_comment = line[pos + 3:].strip()
                data_part = line[:pos].strip().rstrip(",")
                parts = [p.strip() for p in data_part.split(",") if p.strip()]
                term_style = "inline"
            else:
                parts = self._split_comma(line)
                term_style = "none"

            self.sections.append(Section(
                section_type="burst_ranges",
                delimiter=",",
                terminator_style=term_style,
                comment_lines=list(burst_comments),
                data=parts,
                inline_comment=inline_comment,
                label="Burst Time Ranges",
            ))
            idx += 1

        # -- Block 4: Pluviograph Rainfall Data --
        #   pluvio_count blocks, each = station ID line + tab-delimited data
        #   with inline -99 terminator.
        for p_idx in range(self.pluvio_count):
            if idx + 1 >= total:
                break
            station_id = lines[idx].strip()
            idx += 1
            data, delim, _ = self._split_data_line(lines[idx])
            idx += 1
            self.sections.append(Section(
                section_type="pluvio_data",
                delimiter=delim,
                terminator_style="inline",
                prefix_line=station_id,
                data=data,
                label=f"Pluviograph {p_idx + 1}",
            ))

        # -- Block 5: Sub-area Rainfalls --
        #   burst_count blocks, each = C comment + comma/tab data + inline/own-line -99.
        for b in range(self.burst_count):
            comments = []
            while idx < total and lines[idx].startswith("C"):
                comments.append(self._normalise_comment(lines[idx]))
                idx += 1
            if idx >= total:
                break
            data, delim, has_inline_99 = self._split_data_line(lines[idx])
            idx += 1
            if has_inline_99:
                term_style = "inline"
            elif idx < total and lines[idx].strip() == "-99":
                idx += 1
                term_style = "own_line"
            else:
                term_style = "none"
            self.sections.append(Section(
                section_type="subarea_rain",
                delimiter=delim,
                terminator_style=term_style,
                comment_lines=list(comments),
                data=data,
                label=f"Sub-area Rainfall - Burst {b + 1}",
            ))

        # -- Block 6: Pluviograph References --
        #   burst_count blocks, each = C comment + comma/tab ints + inline/own-line -99.
        for b in range(self.burst_count):
            comments = []
            while idx < total and lines[idx].startswith("C"):
                comments.append(self._normalise_comment(lines[idx]))
                idx += 1
            if idx >= total:
                break
            data, delim, has_inline_99 = self._split_data_line(lines[idx])
            idx += 1
            if has_inline_99:
                term_style = "inline"
            elif idx < total and lines[idx].strip() == "-99":
                idx += 1
                term_style = "own_line"
            else:
                term_style = "none"
            self.sections.append(Section(
                section_type="pluvio_ref",
                delimiter=delim,
                terminator_style=term_style,
                comment_lines=list(comments),
                data=data,
                label=f"Pluviograph Refs - Burst {b + 1}",
            ))

        # -- Block 7: Hydrograph Data --
        #   C comment header + comma-delimited time-range pairs + -99
        comments = []
        while idx < total and lines[idx].startswith("C"):
            comments.append(self._normalise_comment(lines[idx]))
            idx += 1

        hydro_count = 0
        if idx < total:
            parts = self._split_comma(lines[idx])
            hydro_count = len(parts) // 2
            self.sections.append(Section(
                section_type="hydro_time_ranges",
                delimiter=",",
                terminator_style="inline",
                comment_lines=list(comments),
                data=parts,
                label="Hydrograph Time Ranges",
            ))
            idx += 1

        # -- Block 7b: Hydrograph Stations --
        #   hydro_count blocks, each = station name | ID
        #   + tab-delimited flow data + own-line -99
        #   + optional comma-delimited suffix params (e.g. "10,4,2,-99")
        for _ in range(hydro_count):
            if idx >= total:
                break
            station_name = lines[idx].strip()
            idx += 1
            if idx >= total:
                break
            data, delim, has_inline_99 = self._split_data_line(lines[idx])
            idx += 1
            # detect terminator style
            if has_inline_99:
                term_style = "inline"
            elif idx < total and lines[idx].strip() == "-99":
                idx += 1
                term_style = "own_line"
            else:
                term_style = "none"
            # suffix param line (item 9.7: volumes of runoff)
            suffix: List[str] = []
            if (idx < total
                    and "," in lines[idx]
                    and not lines[idx].startswith("C")
                    and lines[idx].strip() != "-99"):
                suffix.append(lines[idx])
                idx += 1
            # short label
            short = station_name
            if "|" in station_name:
                short = station_name.split("|")[0].strip()
            if len(short) > 45:
                short = short[:42] + "..."
            self.sections.append(Section(
                section_type="hydro_station",
                delimiter=delim,
                terminator_style=term_style,
                prefix_line=station_name,
                suffix_lines=suffix,
                data=data,
                label=f"Hydro: {short}",
            ))

        # -- Remaining lines (file trailer / extra terminators) --
        if idx < total:
            remaining = []
            while idx < total:
                remaining.append(lines[idx])
                idx += 1
            if remaining:
                self.sections.append(Section(
                    section_type="trailer",
                    raw_text="\n".join(remaining),
                    label="File Trailer",
                ))

        return self.sections


# ============================================================================
#  Writer - reconstructs the STM text file from Section objects
# ============================================================================

class STMWriter:
    """Re-serialises Section objects back into an STM file, preserving
    each section's delimiter and terminator style."""

    def _join_sep(self, sec):
        """Return the join separator string for a section's delimiter."""
        return ", " if sec.delimiter == "," else (sec.delimiter or "\t")

    def _write_data_line(self, sec, out):
        """Append the data + terminator line(s) according to section style."""
        sep = self._join_sep(sec)
        data_str = sep.join(sec.data)
        if sec.terminator_style == "inline":
            out.append(data_str + sep + "-99")
        elif sec.terminator_style == "own_line":
            out.append(data_str)
            out.append("-99")
        else:
            # "none" – no terminator (e.g. Yarra burst ranges)
            out.append(data_str + ",")

    def write(self, sections: List[Section], filepath: str):
        out: List[str] = []

        for sec in sections:
            st = sec.section_type

            # -- free text --
            if st in ("event_header", "model_mode"):
                out.append(sec.raw_text)

            # -- comma-delimited params with inline -99 + optional comment --
            elif st == "storm_params":
                out.extend(sec.comment_lines)
                sep = self._join_sep(sec)
                line = sep.join(sec.data) + sep + "-99"
                if sec.inline_comment:
                    line += " " + sec.inline_comment
                out.append(line)

            # -- burst ranges (comma, optional inline -99, trailing comment) --
            elif st == "burst_ranges":
                out.extend(sec.comment_lines)
                sep = self._join_sep(sec)
                if sec.terminator_style == "inline":
                    line = sep.join(sec.data) + sep + "-99"
                    if sec.inline_comment:
                        line += " " + sec.inline_comment
                else:
                    line = sep.join(sec.data) + ","
                out.append(line)

            # -- pluvio data (auto-detected delimiter, inline -99) --
            elif st == "pluvio_data":
                out.append(sec.prefix_line)
                self._write_data_line(sec, out)

            # -- sub-area rain / pluvio refs (auto-detected delim + terminator) --
            elif st in ("subarea_rain", "pluvio_ref"):
                out.extend(sec.comment_lines)
                self._write_data_line(sec, out)

            # -- hydrograph time ranges (comma, inline -99) --
            elif st == "hydro_time_ranges":
                out.extend(sec.comment_lines)
                out.append(",".join(sec.data) + ",-99")

            # -- hydrograph station (auto-detected delim + terminator, suffix) --
            elif st == "hydro_station":
                out.append(sec.prefix_line)
                self._write_data_line(sec, out)
                # Only write non-empty suffix lines (Item 9.7)
                for sline in sec.suffix_lines:
                    if sline.strip():
                        out.append(sline)

            # -- file trailer (remaining lines like extra -99 terminators) --
            elif st == "trailer":
                for line in sec.raw_text.split("\n"):
                    out.append(line)

        with open(filepath, "w", encoding="utf-8") as f:
            for line in out:
                f.write(line + "\n")


# ============================================================================
#  Main Dialog - tree navigation + context-sensitive editor panel
# ============================================================================

class CopyPasteTable(QTableWidget):
    """QTableWidget with Ctrl+C / Ctrl+V support for Excel interop.

    Copy  – selected cells  → clipboard as tab-separated text.
    Paste – clipboard text  → table starting at current cell.
           Auto-expands columns/rows when pasted data exceeds table bounds,
           provided a Section reference is attached via _section.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Optional references – set by table-creation code to enable auto-expand
        self._section = None        # Section dataclass for data sync
        self._info_label = None     # QLabel showing "Values: N | Delimiter: …"
        self._delim_label = ""      # Delimiter display text (e.g. "COMMA")
        self._dialog = None         # STMEditorDialog ref for _updating guard

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            self._copy()
        elif event.matches(QKeySequence.Paste):
            self._paste()
        else:
            super().keyPressEvent(event)

    def _copy(self):
        sel = sorted(self.selectedIndexes(), key=lambda i: (i.row(), i.column()))
        if not sel:
            return
        rows = {}
        for idx in sel:
            rows.setdefault(idx.row(), {})[idx.column()] = idx.data() or ""
        min_col = min(c for cols in rows.values() for c in cols)
        max_col = max(c for cols in rows.values() for c in cols)
        lines = []
        for r in sorted(rows):
            cells = [rows[r].get(c, "") for c in range(min_col, max_col + 1)]
            lines.append("\t".join(str(v) for v in cells))
        QApplication.clipboard().setText("\n".join(lines))

    def _paste(self):
        text = QApplication.clipboard().text()
        if not text:
            return
        cur = self.currentIndex()
        start_row, start_col = cur.row(), cur.column()

        lines = text.split("\n")
        # Drop empty trailing lines (common with Excel copy)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return

        # Single-row data tables: only paste the first row to prevent
        # accidental multi-row expansion (no row-delete option exists).
        if self.rowCount() == 1 and self._section is not None:
            lines = lines[:1]

        # Determine required table dimensions
        max_row_needed = start_row + len(lines)
        max_col_needed = start_col + max(
            len(line.split("\t")) for line in lines
        )

        # --- Auto-expand table when section is attached -----------------
        expanded = False
        if self._dialog is not None:
            self._dialog._updating = True

        if max_row_needed > self.rowCount():
            self.setRowCount(max_row_needed)
            expanded = True
        if max_col_needed > self.columnCount():
            old_cols = self.columnCount()
            self.setColumnCount(max_col_needed)
            for c in range(old_cols, max_col_needed):
                self.setHorizontalHeaderItem(c, QTableWidgetItem(str(c)))
            expanded = True

        # Set cell values
        for r, line in enumerate(lines):
            for c, val in enumerate(line.split("\t")):
                row, col = start_row + r, start_col + c
                if row < self.rowCount() and col < self.columnCount():
                    item = self.item(row, col)
                    if item is None:
                        item = QTableWidgetItem()
                        self.setItem(row, col, item)
                    item.setText(val.strip())

        # --- Sync section data after paste ------------------------------
        if self._section is not None:
            sec = self._section
            if self.rowCount() == 1:
                # Single-row data table: rebuild sec.data from all columns
                sec.data = [
                    (self.item(0, c).text() if self.item(0, c) else "0")
                    for c in range(self.columnCount())
                ]
            if self._info_label is not None:
                self._info_label.setText(
                    f"Values: {len(sec.data)}  |  Delimiter: {self._delim_label}"
                )
            if self._dialog is not None:
                self._dialog._sync_paired_burst_columns(sec)
                self._dialog._update_section_info(sec)
                self._dialog._status(
                    f"{sec.label}  |  Values: {len(sec.data)}"
                )

        if self._dialog is not None:
            self._dialog._updating = False


class STMEditorDialog(QDialog):
    """
    PyQGIS dialog with a section tree on the left and an editor panel on
    the right that changes to match the selected section type.

    UI interaction:
    - _build_ui / _wire_signals separation
    - QSplitter with right help panel
    - Styled QPushButtons, QGroupBox sections
    - Status bar with context info
    - Add/delete row/column support for data tables
    """

    # Colours
    COLOR_HEADER     = QColor(230, 240, 255)   # Light blue - header sections
    COLOR_PARAM      = QColor(255, 248, 220)   # Light yellow - parameters
    COLOR_DATA       = QColor(240, 255, 240)   # Light green - data tables
    COLOR_HYDRO      = QColor(245, 235, 255)   # Light purple - hydrograph
    COLOR_READONLY   = QColor(240, 240, 240)   # Gray - read-only cells

    MONO = QFont("Consolas", 10)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sections: List[Section] = []
        self.filepath = ""
        self._current_idx = -1
        self._updating = False          # guards against cellChanged feedback

        self.setWindowTitle("RORB STM Editor")
        self.setMinimumSize(1000, 600)
        self.resize(1300, 780)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)

        self._build_ui()
        self._wire_signals()

    # ====================================================================
    # UI CONSTRUCTION
    # ====================================================================

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # --- Toolbar row ---
        root.addWidget(self._create_toolbar())

        # --- Main splitter: tree | editor | help panel ---
        self.main_splitter = QSplitter(Qt.Horizontal)

        # LEFT: section tree + management buttons
        tree_container = QWidget()
        tree_container.setMinimumWidth(220)
        tree_container.setMaximumWidth(380)
        tree_vlayout = QVBoxLayout(tree_container)
        tree_vlayout.setContentsMargins(0, 0, 0, 0)
        tree_vlayout.setSpacing(4)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Sections")
        self.tree.setAlternatingRowColors(True)
        self.tree.setStyleSheet(
            "QTreeWidget { font-size: 10pt; }"
            "QTreeWidget::item { padding: 3px 0px; }"
            "QTreeWidget::item:selected { background-color: #bbdefb; color: #000; }"
        )
        tree_vlayout.addWidget(self.tree)

        # Section management buttons
        _sbtn = (
            "QPushButton {{ padding: 4px 8px; border: 1px solid {0}; "
            "color: {0}; border-radius: 3px; font-weight: bold; font-size: 8pt; }}"
            "QPushButton:hover {{ background-color: {1}; }}"
            "QPushButton:disabled {{ color: #999; border-color: #ccc; }}"
        )
        sec_row1 = QHBoxLayout()
        sec_row1.setSpacing(3)

        self.btn_add_pluvio = QPushButton("+ Pluvio")
        self.btn_add_pluvio.setToolTip("Add a new Pluviograph data section")
        self.btn_add_pluvio.setStyleSheet(_sbtn.format("#4CAF50", "#E8F5E9"))
        self.btn_add_pluvio.setEnabled(False)
        sec_row1.addWidget(self.btn_add_pluvio)

        self.btn_add_burst = QPushButton("+ Burst")
        self.btn_add_burst.setToolTip("Add a new Sub-area Rainfall + Pluvio Ref pair")
        self.btn_add_burst.setStyleSheet(_sbtn.format("#2196F3", "#E3F2FD"))
        self.btn_add_burst.setEnabled(False)
        sec_row1.addWidget(self.btn_add_burst)

        self.btn_add_hydro = QPushButton("+ Hydro")
        self.btn_add_hydro.setToolTip("Add a new Hydrograph station")
        self.btn_add_hydro.setStyleSheet(_sbtn.format("#9C27B0", "#F3E5F5"))
        self.btn_add_hydro.setEnabled(False)
        sec_row1.addWidget(self.btn_add_hydro)

        self.btn_del_section = QPushButton("- Delete")
        self.btn_del_section.setToolTip("Delete the currently selected section")
        self.btn_del_section.setStyleSheet(_sbtn.format("#f44336", "#FFEBEE"))
        self.btn_del_section.setEnabled(False)
        sec_row1.addWidget(self.btn_del_section)

        tree_vlayout.addLayout(sec_row1)

        # CENTER: editor panel
        self.editor_box = QWidget()
        self.editor_lay = QVBoxLayout(self.editor_box)
        self.editor_lay.setContentsMargins(6, 6, 6, 6)
        self.editor_lay.setSpacing(6)
        placeholder = QLabel("Open or create a new STM file to begin editing.")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setStyleSheet("color: #999; font-size: 14px;")
        self.editor_lay.addWidget(placeholder)

        # RIGHT: help / info panel
        right_panel = self._create_right_panel()

        self.main_splitter.addWidget(tree_container)
        self.main_splitter.addWidget(self.editor_box)
        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self.main_splitter.setSizes([240, 900, 340])

        root.addWidget(self.main_splitter, 1)

        # --- Bottom status bar ---
        root.addWidget(self._create_bottom_bar())

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------
    def _create_toolbar(self):
        group = QGroupBox()
        group.setStyleSheet(
            "QGroupBox { border: 1px solid #ccc; border-radius: 4px; "
            "background-color: #fafafa; }"
        )
        layout = QHBoxLayout(group)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        self.btn_new = QPushButton("  New STM")
        self.btn_new.setStyleSheet("""
            QPushButton {
                background-color: #FF9800; color: white; border: none;
                padding: 7px 18px; border-radius: 4px; font-weight: bold;
                font-size: 10pt;
            }
            QPushButton:hover { background-color: #F57C00; }
            QPushButton:pressed { background-color: #E65100; }
        """)

        self.btn_open = QPushButton("  Open STM")
        self.btn_open.setStyleSheet("""
            QPushButton {
                background-color: #2196F3; color: white; border: none;
                padding: 7px 18px; border-radius: 4px; font-weight: bold;
                font-size: 10pt;
            }
            QPushButton:hover { background-color: #1976D2; }
            QPushButton:pressed { background-color: #0D47A1; }
        """)

        self.btn_save = QPushButton("  Save")
        self.btn_save.setEnabled(False)
        self.btn_save.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50; color: white; border: none;
                padding: 7px 18px; border-radius: 4px; font-weight: bold;
                font-size: 10pt;
            }
            QPushButton:hover { background-color: #388E3C; }
            QPushButton:pressed { background-color: #1B5E20; }
            QPushButton:disabled { background-color: #BDBDBD; color: #888; }
        """)

        self.btn_save_as = QPushButton("  Save As")
        self.btn_save_as.setEnabled(False)
        self.btn_save_as.setStyleSheet("""
            QPushButton {
                padding: 7px 18px; border-radius: 4px; font-size: 10pt;
                border: 1px solid #aaa;
            }
            QPushButton:hover { background-color: #e0e0e0; }
            QPushButton:disabled { background-color: #BDBDBD; color: #888; }
        """)

        self.lbl_file = QLabel("No file loaded")
        self.lbl_file.setStyleSheet(
            "color: #666; font-style: italic; font-size: 10pt; padding-left: 12px;"
        )

        layout.addWidget(self.btn_new)
        layout.addWidget(self.btn_open)
        layout.addWidget(self.btn_save)
        layout.addWidget(self.btn_save_as)
        layout.addStretch()
        layout.addWidget(self.lbl_file)
        return group

    # ------------------------------------------------------------------
    # Right help panel (GeoTable Compare style scroll area)
    # ------------------------------------------------------------------
    def _create_right_panel(self):
        panel = QWidget()
        panel.setMinimumWidth(340)
        panel.setMaximumWidth(340)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background-color: #f5f5f5; border: none; }"
        )

        content = QWidget()
        content.setStyleSheet("QWidget { background-color: #f5f5f5; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # --- Title ---
        title = QLabel("<b>RORB STM Editor</b>")
        title.setStyleSheet("font-size: 11pt; color: #1976D2;")
        layout.addWidget(title)

        # --- Help text ---
        help_text = QLabel(
            "<b>How to use:</b><br>"
            "1. Click <b style='color:#e65100;'>New STM</b> to create from scratch, "
            "or <b style='color:#1565C0;'>Open STM</b> to load a file<br>"
            "2. Navigate sections in the <b>tree</b> on the left<br>"
            "3. Edit values in the <b>table</b> in the centre<br>"
            "4. Use <b>+ Add</b> / <b>- Delete</b> buttons to modify table size<br>"
            "5. Click <b style='color:#2e7d32;'>Save</b> to write back<br><br>"
            "<b>Section Management:</b><br>"
            "<b style='color:#4CAF50;'>+ Pluvio</b> - Add pluviograph station<br>"
            "<b style='color:#2196F3;'>+ Burst</b> - Add sub-area rainfall + "
            "pluvio ref pair<br>"
            "<b style='color:#9C27B0;'>+ Hydro</b> - Add hydrograph station<br>"
            "<b style='color:#f44336;'>- Delete</b> - Remove selected section<br><br>"
            "<b>Tip:</b> Delimiter format (tab, comma) is preserved "
            "automatically. The <code>-99</code> terminators are managed "
            "by the editor and hidden from view.<br><br>"
            "<b>Note:</b> Storm Parameters (burst count, pluvio count) are "
            "auto-synced when you add or delete sections."
        )
        help_text.setWordWrap(True)
        help_text.setTextFormat(Qt.RichText)
        help_text.setStyleSheet("font-size: 9pt;")
        layout.addWidget(help_text)

        # --- Legend ---
        legend_group = QGroupBox("Section Types")
        legend_group.setStyleSheet(
            "QGroupBox { font-weight: bold; background-color: #f5f5f5; }"
        )
        legend_layout = QVBoxLayout()
        legend_layout.setSpacing(3)

        legends = [
            ("Header / Text", "230,240,255", "Event description and model mode"),
            ("Parameters", "255,248,220", "Storm configuration values (comma-delimited)"),
            ("Data Tables", "240,255,240", "Rainfall, sub-area & reference data (comma or tab)"),
            ("Hydrographs", "245,235,255", "Observed flow/level data with station metadata"),
        ]
        for text, rgb, tip in legends:
            lbl = QLabel(f"  {text}")
            lbl.setStyleSheet(
                f"background-color: rgb({rgb}); padding: 3px 8px; "
                "border: 1px solid #ccc; border-radius: 2px; font-size: 9pt;"
            )
            lbl.setToolTip(tip)
            legend_layout.addWidget(lbl)

        legend_group.setLayout(legend_layout)
        layout.addWidget(legend_group)

        # --- Section info (updates when section is selected) ---
        self.info_group = QGroupBox("Current Section")
        self.info_group.setStyleSheet(
            "QGroupBox { font-weight: bold; background-color: #f5f5f5; }"
        )
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        self.info_label = QLabel(
            "<i style='color:#888;'>Select a section to see details</i>"
        )
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(Qt.RichText)
        self.info_label.setStyleSheet("font-size: 9pt;")
        info_layout.addWidget(self.info_label)
        self.info_group.setLayout(info_layout)
        layout.addWidget(self.info_group)

        # --- File info (updates when file is loaded) ---
        self.file_info_group = QGroupBox("File Summary")
        self.file_info_group.setStyleSheet(
            "QGroupBox { font-weight: bold; background-color: #f5f5f5; }"
        )
        fi_layout = QVBoxLayout()
        self.file_info_label = QLabel(
            "<i style='color:#888;'>No file loaded</i>"
        )
        self.file_info_label.setWordWrap(True)
        self.file_info_label.setTextFormat(Qt.RichText)
        self.file_info_label.setStyleSheet("font-size: 9pt;")
        fi_layout.addWidget(self.file_info_label)
        self.file_info_group.setLayout(fi_layout)
        layout.addWidget(self.file_info_group)

        layout.addStretch()
        scroll.setWidget(content)

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.addWidget(scroll)
        return panel

    # ------------------------------------------------------------------
    # Bottom bar (status + progress)
    # ------------------------------------------------------------------
    def _create_bottom_bar(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.lbl_status = QLabel("Ready - open an STM file to begin")
        self.lbl_status.setStyleSheet(
            "background: #f0f0f0; padding: 5px 8px; border-top: 1px solid #ccc; "
            "color: #555; font-size: 9pt;"
        )
        row.addWidget(self.lbl_status)
        layout.addLayout(row)
        return widget

    # ====================================================================
    # SIGNAL WIRING
    # ====================================================================

    def _wire_signals(self):
        self.btn_new.clicked.connect(self._on_new)
        self.btn_open.clicked.connect(self._on_open)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_save_as.clicked.connect(self._on_save_as)
        self.tree.currentItemChanged.connect(self._on_tree_changed)
        self.btn_add_pluvio.clicked.connect(self._add_pluvio_section)
        self.btn_add_burst.clicked.connect(self._add_subarea_burst_sections)
        self.btn_add_hydro.clicked.connect(self._add_hydro_station_section)
        self.btn_del_section.clicked.connect(self._delete_current_section)

    # ====================================================================
    # FILE OPERATIONS
    # ====================================================================

    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open RORB Storm File", "",
            "Storm Files (*.stm);;All Files (*)",
        )
        if not path:
            return

        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(10)
        QApplication.processEvents()

        try:
            parser = STMParser()
            self.sections = parser.parse(path)
            self.filepath = path
            self.lbl_file.setText(os.path.basename(path))
            self.lbl_file.setStyleSheet(
                "color: #333; font-weight: bold; font-size: 10pt; padding-left: 12px;"
            )
            self.btn_save.setEnabled(True)
            self.btn_save_as.setEnabled(True)
            self._enable_section_buttons(True)

            self.progress_bar.setValue(60)
            QApplication.processEvents()

            self._populate_tree()

            self.progress_bar.setValue(90)
            QApplication.processEvents()

            self._status(
                f"Loaded {len(self.sections)} sections  |  "
                f"Bursts: {parser.burst_count}  |  "
                f"Pluviographs: {parser.pluvio_count}  |  "
                f"Duration: {parser.duration} x {parser.time_inc} hr"
            )

            # Update file info panel
            self.file_info_label.setText(
                f"<b>File:</b> {os.path.basename(path)}<br>"
                f"<b>Sections:</b> {len(self.sections)}<br>"
                f"<b>Burst count:</b> {parser.burst_count}<br>"
                f"<b>Pluviographs:</b> {parser.pluvio_count}<br>"
                f"<b>Duration:</b> {parser.duration} x {parser.time_inc} hr<br>"
                f"<b>Path:</b> <span style='font-size:8pt;'>{path}</span>"
            )

            self.progress_bar.setValue(100)
            QTimer.singleShot(1200, lambda: self.progress_bar.setVisible(False))

        except Exception as exc:
            self.progress_bar.setVisible(False)
            QMessageBox.critical(self, "Parse Error",
                                 f"Failed to parse STM file:\n\n{exc}")

    def _on_save(self):
        if not self.filepath:
            return self._on_save_as()
        self._write(self.filepath)

    def _on_save_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save RORB Storm File", self.filepath,
            "Storm Files (*.stm);;All Files (*)",
        )
        if path:
            self.filepath = path
            self.lbl_file.setText(os.path.basename(path))
            self._write(path)

    def _write(self, path: str):
        try:
            STMWriter().write(self.sections, path)
            self._status(f"Saved successfully -> {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save Error",
                                 f"Failed to save:\n\n{exc}")

    # ====================================================================
    # TREE MANAGEMENT
    # ====================================================================

    def _populate_tree(self):
        self.tree.clear()
        self._current_idx = -1

        groups = {}          # parent nodes keyed by group name
        group_labels = {
            "pluvio_data":  "Pluviographs",
            "subarea_rain": "Sub-area Rainfalls",
            "pluvio_ref":   "Pluviograph References",
            "hydro":        "Hydrographs",
        }

        def _get_group(key: str) -> QTreeWidgetItem:
            if key not in groups:
                parent = QTreeWidgetItem(self.tree)
                parent.setText(0, group_labels[key])
                parent.setData(0, Qt.UserRole, -1)
                parent.setExpanded(True)
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                groups[key] = parent
            return groups[key]

        for i, sec in enumerate(self.sections):
            st = sec.section_type

            if st in ("event_header", "model_mode", "storm_params",
                       "burst_ranges"):
                item = QTreeWidgetItem(self.tree)
                item.setText(0, sec.label)
                item.setData(0, Qt.UserRole, i)

            elif st == "pluvio_data":
                child = QTreeWidgetItem(_get_group("pluvio_data"))
                child.setText(0, sec.label)
                child.setData(0, Qt.UserRole, i)

            elif st == "subarea_rain":
                child = QTreeWidgetItem(_get_group("subarea_rain"))
                child.setText(0, sec.label)
                child.setData(0, Qt.UserRole, i)

            elif st == "pluvio_ref":
                child = QTreeWidgetItem(_get_group("pluvio_ref"))
                child.setText(0, sec.label)
                child.setData(0, Qt.UserRole, i)

            elif st == "hydro_time_ranges":
                child = QTreeWidgetItem(_get_group("hydro"))
                child.setText(0, sec.label)
                child.setData(0, Qt.UserRole, i)

            elif st == "hydro_station":
                child = QTreeWidgetItem(_get_group("hydro"))
                child.setText(0, sec.label)
                child.setData(0, Qt.UserRole, i)

        self.tree.expandAll()

    # ====================================================================
    # TREE SELECTION -> EDITOR
    # ====================================================================

    def _on_tree_changed(self, current, _previous):
        if current is None:
            return
        idx = current.data(0, Qt.UserRole)
        if idx is None or idx < 0 or idx >= len(self.sections):
            return
        self._current_idx = idx
        self._show_editor(self.sections[idx])

    # ====================================================================
    # NEW FILE CREATION
    # ====================================================================

    def _on_new(self):
        """Create a brand-new STM file with minimal default structure."""
        if self.sections:
            reply = QMessageBox.question(
                self, "New STM",
                "This will discard the current file. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self.sections = [
            Section(
                section_type="event_header",
                raw_text="New Storm Event",
                label="Event Description",
            ),
            Section(
                section_type="model_mode",
                raw_text="DESIGN",
                label="Model Mode",
            ),
            Section(
                section_type="storm_params",
                delimiter=",",
                terminator_style="inline",
                comment_lines=[
                    "C time_inc, duration, burst_count, pluvio_count, flag",
                ],
                data=["1", "1", "1", "1", "1"],
                label="Storm Parameters",
            ),
            Section(
                section_type="burst_ranges",
                delimiter=",",
                terminator_style="inline",
                data=["0", "1"],
                label="Burst Time Ranges",
            ),
            Section(
                section_type="pluvio_data",
                delimiter=",",
                terminator_style="inline",
                prefix_line="Pluvio_1",
                data=["0"],
                label="Pluviograph 1",
            ),
            Section(
                section_type="subarea_rain",
                delimiter=",",
                terminator_style="inline",
                comment_lines=["C Sub-area rainfall for Burst 1"],
                data=["1.0"],
                label="Sub-area Rainfall - Burst 1",
            ),
            Section(
                section_type="pluvio_ref",
                delimiter=",",
                terminator_style="inline",
                comment_lines=["C Pluviograph references for Burst 1"],
                data=["1"],
                label="Pluviograph Refs - Burst 1",
            ),
            Section(
                section_type="hydro_time_ranges",
                delimiter=",",
                terminator_style="inline",
                comment_lines=["C Hydrograph data"],
                data=["0", "1"],
                label="Hydrograph Time Ranges",
            ),
            Section(
                section_type="hydro_station",
                delimiter=",",
                terminator_style="inline",
                prefix_line="Station_1",
                data=["0"],
                label="Hydro: Station_1",
            ),
        ]

        self.filepath = ""
        self.lbl_file.setText("New STM (unsaved)")
        self.lbl_file.setStyleSheet(
            "color: #e65100; font-weight: bold; font-size: 10pt; padding-left: 12px;"
        )
        self.btn_save.setEnabled(True)
        self.btn_save_as.setEnabled(True)
        self._enable_section_buttons(True)
        self._populate_tree()
        self._update_file_info()
        self._status("New STM created - add sections and save when ready")

    # ====================================================================
    # SECTION MANAGEMENT
    # ====================================================================

    def _enable_section_buttons(self, enabled=True):
        """Enable/disable section management buttons."""
        self.btn_add_pluvio.setEnabled(enabled)
        self.btn_add_burst.setEnabled(enabled)
        self.btn_add_hydro.setEnabled(enabled)
        self.btn_del_section.setEnabled(enabled)

    def _find_last_index(self, section_type):
        """Find the last index of a given section type, or -1."""
        idx = -1
        for i, sec in enumerate(self.sections):
            if sec.section_type == section_type:
                idx = i
        return idx

    def _find_insert_pos(self, section_type):
        """Find the correct insertion position for a new section of the given type.

        Maintains ordering: event_header, model_mode, storm_params, burst_ranges,
        pluvio_data..., subarea_rain..., pluvio_ref..., hydro_time_ranges,
        hydro_station..., trailer
        """
        order = ["event_header", "model_mode", "storm_params", "burst_ranges",
                 "pluvio_data", "subarea_rain", "pluvio_ref",
                 "hydro_time_ranges", "hydro_station", "trailer"]

        # Find the last section of the same type
        last_same = self._find_last_index(section_type)
        if last_same >= 0:
            return last_same + 1

        # Find the type's position in the ordering
        type_pos = order.index(section_type) if section_type in order else len(order) - 1

        # Find the last section of any earlier type
        for t in reversed(order[:type_pos]):
            last = self._find_last_index(t)
            if last >= 0:
                return last + 1

        return len(self.sections)

    def _sync_storm_params(self):
        """Recount pluvio/burst sections and update storm parameters."""
        sp_sec = None
        for sec in self.sections:
            if sec.section_type == "storm_params":
                sp_sec = sec
                break
        if sp_sec is None:
            return

        pluvio_count = sum(1 for s in self.sections if s.section_type == "pluvio_data")
        burst_count = sum(1 for s in self.sections if s.section_type == "subarea_rain")

        # Ensure data list is large enough
        while len(sp_sec.data) < 5:
            sp_sec.data.append("1")
        sp_sec.data[2] = str(burst_count)
        sp_sec.data[3] = str(pluvio_count)

        # Re-label subarea_rain and pluvio_ref sections
        b = 0
        for sec in self.sections:
            if sec.section_type == "subarea_rain":
                b += 1
                sec.label = f"Sub-area Rainfall - Burst {b}"
        b = 0
        for sec in self.sections:
            if sec.section_type == "pluvio_ref":
                b += 1
                sec.label = f"Pluviograph Refs - Burst {b}"

    def _sync_sections_to_params(self):
        """Auto-create or remove pluvio / burst sections to match storm-param counts.

        Reads burst_count (index 2) and pluvio_count (index 3) from the
        storm_params section, then adds or removes pluvio_data, subarea_rain,
        pluvio_ref, and burst_ranges entries so the section list matches.
        """
        sp_sec = None
        for sec in self.sections:
            if sec.section_type == "storm_params":
                sp_sec = sec
                break
        if sp_sec is None or len(sp_sec.data) < 4:
            return

        # Parse target counts (ignore non-integer edits gracefully)
        try:
            target_bursts = int(sp_sec.data[2])
        except (ValueError, IndexError):
            return
        try:
            target_pluvios = int(sp_sec.data[3])
        except (ValueError, IndexError):
            return

        # Clamp to sane range to avoid accidental huge allocations
        target_bursts = max(0, min(target_bursts, 200))
        target_pluvios = max(0, min(target_pluvios, 500))

        # --- Match data length from existing sections (for new ones) ---
        default_pluvio_data = ["0"]
        default_sa_data = ["1.0"]
        default_pr_data = ["1"]
        for sec in self.sections:
            if sec.section_type == "pluvio_data" and sec.data:
                default_pluvio_data = ["0"] * len(sec.data)
                break
        for sec in self.sections:
            if sec.section_type == "subarea_rain" and sec.data:
                default_sa_data = ["0"] * len(sec.data)
                break
        for sec in self.sections:
            if sec.section_type == "pluvio_ref" and sec.data:
                default_pr_data = ["1"] * len(sec.data)
                break

        # ----- Pluviograph Data sections -----
        cur_pluvios = sum(1 for s in self.sections if s.section_type == "pluvio_data")
        while cur_pluvios < target_pluvios:
            cur_pluvios += 1
            new_sec = Section(
                section_type="pluvio_data",
                delimiter=",",
                terminator_style="inline",
                prefix_line=f"Pluvio_{cur_pluvios}",
                data=list(default_pluvio_data),
                label=f"Pluviograph {cur_pluvios}",
            )
            pos = self._find_insert_pos("pluvio_data")
            self.sections.insert(pos, new_sec)
        while cur_pluvios > target_pluvios and cur_pluvios > 0:
            idx = self._find_last_index("pluvio_data")
            if idx >= 0:
                self.sections.pop(idx)
            cur_pluvios -= 1

        # ----- Sub-area Rainfall + Pluviograph Refs (paired per burst) -----
        cur_bursts = sum(1 for s in self.sections if s.section_type == "subarea_rain")
        while cur_bursts < target_bursts:
            cur_bursts += 1
            sa_sec = Section(
                section_type="subarea_rain",
                delimiter=",",
                terminator_style="inline",
                comment_lines=[f"C Sub-area rainfall for Burst {cur_bursts}"],
                data=list(default_sa_data),
                label=f"Sub-area Rainfall - Burst {cur_bursts}",
            )
            pos_sa = self._find_insert_pos("subarea_rain")
            self.sections.insert(pos_sa, sa_sec)

            pr_sec = Section(
                section_type="pluvio_ref",
                delimiter=",",
                terminator_style="inline",
                comment_lines=[f"C Pluviograph references for Burst {cur_bursts}"],
                data=list(default_pr_data),
                label=f"Pluviograph Refs - Burst {cur_bursts}",
            )
            pos_pr = self._find_insert_pos("pluvio_ref")
            self.sections.insert(pos_pr, pr_sec)
        while cur_bursts > target_bursts and cur_bursts > 0:
            # Remove last subarea_rain
            idx = self._find_last_index("subarea_rain")
            if idx >= 0:
                self.sections.pop(idx)
            # Remove last pluvio_ref
            idx = self._find_last_index("pluvio_ref")
            if idx >= 0:
                self.sections.pop(idx)
            cur_bursts -= 1

        # ----- Burst Time Ranges: ensure correct number of pairs -----
        for sec in self.sections:
            if sec.section_type == "burst_ranges":
                needed = target_bursts * 2
                while len(sec.data) < needed:
                    sec.data.extend(["0", "0"])
                while len(sec.data) > needed and len(sec.data) >= 2:
                    sec.data.pop()
                    sec.data.pop()
                break

        # Re-label everything consistently
        self._sync_storm_params()
        self._populate_tree()
        self._update_file_info()

    def _update_file_info(self):
        """Update the file summary panel with current section counts."""
        pluvio_count = sum(1 for s in self.sections if s.section_type == "pluvio_data")
        burst_count = sum(1 for s in self.sections if s.section_type == "subarea_rain")
        hydro_count = sum(1 for s in self.sections if s.section_type == "hydro_station")

        duration = "?"
        time_inc = "?"
        for sec in self.sections:
            if sec.section_type == "storm_params":
                if len(sec.data) > 0:
                    time_inc = sec.data[0]
                if len(sec.data) > 1:
                    duration = sec.data[1]
                break

        fname = os.path.basename(self.filepath) if self.filepath else "New (unsaved)"
        self.file_info_label.setText(
            f"<b>File:</b> {fname}<br>"
            f"<b>Sections:</b> {len(self.sections)}<br>"
            f"<b>Burst count:</b> {burst_count}<br>"
            f"<b>Pluviographs:</b> {pluvio_count}<br>"
            f"<b>Hydro stations:</b> {hydro_count}<br>"
            f"<b>Duration:</b> {duration} x {time_inc} hr"
        )

    def _add_pluvio_section(self):
        """Add a new empty pluviograph data section."""
        count = sum(1 for s in self.sections if s.section_type == "pluvio_data")
        default_name = f"Pluvio_{count + 1}"

        text, ok = QInputDialog.getText(
            self, "New Pluviograph", "Station ID:", QLineEdit.Normal, default_name
        )
        if not ok or not text.strip():
            return
        name = text.strip()

        # Match data length from existing pluviographs
        default_data = ["0"]
        for sec in self.sections:
            if sec.section_type == "pluvio_data" and sec.data:
                default_data = ["0"] * len(sec.data)
                break

        pluvio_num = count + 1
        new_sec = Section(
            section_type="pluvio_data",
            delimiter=",",
            terminator_style="inline",
            prefix_line=name,
            data=list(default_data),
            label=f"Pluviograph {pluvio_num}",
        )

        pos = self._find_insert_pos("pluvio_data")
        self.sections.insert(pos, new_sec)
        self._sync_storm_params()
        self._populate_tree()
        self._update_file_info()
        self._status(f"Added pluviograph: {name}")

    def _add_subarea_burst_sections(self):
        """Add a new sub-area rainfall AND corresponding pluviograph reference."""
        burst_count = sum(1 for s in self.sections if s.section_type == "subarea_rain")
        new_burst_num = burst_count + 1

        # Match data length from existing sections
        default_sa_data = ["1.0"]
        default_pr_data = ["1"]
        for sec in self.sections:
            if sec.section_type == "subarea_rain" and sec.data:
                default_sa_data = ["0"] * len(sec.data)
                break
        for sec in self.sections:
            if sec.section_type == "pluvio_ref" and sec.data:
                default_pr_data = ["1"] * len(sec.data)
                break

        sa_sec = Section(
            section_type="subarea_rain",
            delimiter=",",
            terminator_style="inline",
            comment_lines=[f"C Sub-area rainfall for Burst {new_burst_num}"],
            data=list(default_sa_data),
            label=f"Sub-area Rainfall - Burst {new_burst_num}",
        )

        pr_sec = Section(
            section_type="pluvio_ref",
            delimiter=",",
            terminator_style="inline",
            comment_lines=[f"C Pluviograph references for Burst {new_burst_num}"],
            data=list(default_pr_data),
            label=f"Pluviograph Refs - Burst {new_burst_num}",
        )

        # Insert subarea_rain first
        pos_sa = self._find_insert_pos("subarea_rain")
        self.sections.insert(pos_sa, sa_sec)

        # Insert pluvio_ref (indices shifted by 1 after subarea insert)
        pos_pr = self._find_insert_pos("pluvio_ref")
        self.sections.insert(pos_pr, pr_sec)

        # Add a time range pair to burst_ranges
        for sec in self.sections:
            if sec.section_type == "burst_ranges":
                sec.data.extend(["0", "0"])
                break

        self._sync_storm_params()
        self._populate_tree()
        self._update_file_info()
        self._status(f"Added Burst {new_burst_num} (Sub-area Rainfall + Pluvio Refs)")

    def _add_hydro_station_section(self):
        """Add a new hydrograph station section."""
        count = sum(1 for s in self.sections if s.section_type == "hydro_station")
        default_name = f"Station_{count + 1}"

        text, ok = QInputDialog.getText(
            self, "New Hydrograph Station", "Station Name / ID:",
            QLineEdit.Normal, default_name,
        )
        if not ok or not text.strip():
            return
        name = text.strip()

        # Match data length from existing hydro stations
        default_data = ["0"]
        for sec in self.sections:
            if sec.section_type == "hydro_station" and sec.data:
                default_data = ["0"] * len(sec.data)
                break

        # Ensure hydro_time_ranges exists
        htr = None
        for sec in self.sections:
            if sec.section_type == "hydro_time_ranges":
                htr = sec
                break

        if htr is None:
            htr = Section(
                section_type="hydro_time_ranges",
                delimiter=",",
                terminator_style="inline",
                comment_lines=["C Hydrograph data"],
                data=["0", "0"],
                label="Hydrograph Time Ranges",
            )
            pos = self._find_insert_pos("hydro_time_ranges")
            self.sections.insert(pos, htr)
        else:
            htr.data.extend(["0", "0"])

        # Short label
        short = name
        if "|" in name:
            short = name.split("|")[0].strip()
        if len(short) > 45:
            short = short[:42] + "..."

        new_sec = Section(
            section_type="hydro_station",
            delimiter=",",
            terminator_style="inline",
            prefix_line=name,
            data=list(default_data),
            label=f"Hydro: {short}",
        )

        pos = self._find_insert_pos("hydro_station")
        self.sections.insert(pos, new_sec)
        self._populate_tree()
        self._update_file_info()
        self._status(f"Added hydrograph station: {name}")

    def _delete_current_section(self):
        """Delete the currently selected section."""
        idx = self._current_idx
        if idx < 0 or idx >= len(self.sections):
            QMessageBox.information(self, "Info", "Select a section to delete.")
            return

        sec = self.sections[idx]

        # Prevent deleting structural sections
        if sec.section_type in ("event_header", "model_mode", "storm_params",
                                 "burst_ranges"):
            QMessageBox.warning(
                self, "Cannot Delete",
                f"The '{sec.label}' section is required and cannot be deleted.\n"
                "You can edit its values instead.",
            )
            return

        # Prevent deleting pluvio_ref directly (paired with subarea)
        if sec.section_type == "pluvio_ref":
            QMessageBox.warning(
                self, "Cannot Delete Individually",
                "Pluviograph References are paired with Sub-area Rainfalls.\n"
                "Delete the corresponding Sub-area Rainfall section instead.",
            )
            return

        reply = QMessageBox.question(
            self, "Delete Section",
            f"Delete '{sec.label}'?\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        label = sec.label

        if sec.section_type == "subarea_rain":
            # Find which burst number this is (0-based)
            burst_idx = 0
            for s in self.sections:
                if s is sec:
                    break
                if s.section_type == "subarea_rain":
                    burst_idx += 1

            # Remove the matching pluvio_ref
            ref_count = 0
            for i, s in enumerate(self.sections):
                if s.section_type == "pluvio_ref":
                    if ref_count == burst_idx:
                        self.sections.pop(i)
                        break
                    ref_count += 1

            # Remove the subarea_rain itself
            if sec in self.sections:
                self.sections.remove(sec)

            # Remove the burst range pair
            for s in self.sections:
                if s.section_type == "burst_ranges":
                    start = burst_idx * 2
                    if start + 1 < len(s.data):
                        s.data.pop(start + 1)
                        s.data.pop(start)
                    elif start < len(s.data):
                        s.data.pop(start)
                    break

            self._sync_storm_params()

        elif sec.section_type == "hydro_station":
            # Find which hydro station number this is (0-based)
            hydro_idx = 0
            for s in self.sections:
                if s is sec:
                    break
                if s.section_type == "hydro_station":
                    hydro_idx += 1

            self.sections.remove(sec)

            # Remove time range pair from hydro_time_ranges
            for s in self.sections:
                if s.section_type == "hydro_time_ranges":
                    start = hydro_idx * 2
                    if start + 1 < len(s.data):
                        s.data.pop(start + 1)
                        s.data.pop(start)
                    elif start < len(s.data):
                        s.data.pop(start)
                    break

        else:
            # pluvio_data, hydro_time_ranges, trailer, etc.
            self.sections.remove(sec)
            if sec.section_type == "pluvio_data":
                self._sync_storm_params()

        self._current_idx = -1
        self._clear_editor()
        self._populate_tree()
        self._update_file_info()
        self._status(f"Deleted: {label}")

    # ====================================================================
    # EDITOR PANEL UTILITIES
    # ====================================================================

    def _clear_editor(self):
        while self.editor_lay.count():
            child = self.editor_lay.takeAt(0)
            w = child.widget()
            if w:
                w.deleteLater()

    def _status(self, text: str):
        self.lbl_status.setText(text)

    def _update_section_info(self, sec: Section):
        """Update the right-panel section info box."""
        delim_names = {"\t": "TAB", ",": "COMMA", None: "None"}
        d = delim_names.get(sec.delimiter, sec.delimiter or "None")
        term_names = {
            "inline": "Inline (-99 on data line)",
            "own_line": "Own line (-99 below data)",
            "none": "None",
        }
        t = term_names.get(sec.terminator_style, sec.terminator_style)

        info = (
            f"<b>Type:</b> {sec.section_type}<br>"
            f"<b>Label:</b> {sec.label}<br>"
            f"<b>Delimiter:</b> {d}<br>"
            f"<b>Terminator:</b> {t}<br>"
            f"<b>Data values:</b> {len(sec.data)}"
        )
        if sec.prefix_line:
            info += f"<br><b>Station:</b> {sec.prefix_line}"
        if sec.comment_lines:
            info += f"<br><b>Comment lines:</b> {len(sec.comment_lines)}"
        if sec.suffix_lines:
            info += f"<br><b>Suffix lines:</b> {len(sec.suffix_lines)}"

        self.info_label.setText(info)

    # --- styled table factory (consistent with GeoTable Compare) ---

    def _make_table(self, rows, cols, editable=True):
        """Create a CopyPasteTable (QTableWidget) with consistent styling."""
        tbl = CopyPasteTable(rows, cols)
        tbl.setFont(self.MONO)
        tbl.setAlternatingRowColors(True)
        tbl.setSelectionBehavior(QAbstractItemView.SelectItems)
        tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        if not editable:
            tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setStyleSheet(
            "QTableWidget { gridline-color: #ddd; }"
            "QTableWidget::item:selected { background-color: #bbdefb; color: #000; }"
        )
        return tbl

    # --- burst-pair column sync ---

    def _find_burst_partner(self, sec: Section):
        """Return the paired section for a subarea_rain ↔ pluvio_ref burst pair.

        Sub-area Rainfall Burst N pairs with Pluviograph Refs Burst N.
        Returns None if no partner found.
        """
        if sec.section_type == "subarea_rain":
            partner_type = "pluvio_ref"
        elif sec.section_type == "pluvio_ref":
            partner_type = "subarea_rain"
        else:
            return None

        # Find which burst index this section is (0-based)
        burst_idx = 0
        for s in self.sections:
            if s is sec:
                break
            if s.section_type == sec.section_type:
                burst_idx += 1

        # Find the partner at the same burst index
        count = 0
        for s in self.sections:
            if s.section_type == partner_type:
                if count == burst_idx:
                    return s
                count += 1
        return None

    def _sync_paired_burst_columns(self, sec: Section):
        """After column changes to a subarea_rain or pluvio_ref, resize partner."""
        partner = self._find_burst_partner(sec)
        if partner is None:
            return
        target_len = len(sec.data)
        cur_len = len(partner.data)
        if cur_len == target_len:
            return
        default_val = "1" if partner.section_type == "pluvio_ref" else "0"
        if cur_len < target_len:
            partner.data.extend([default_val] * (target_len - cur_len))
        else:
            partner.data = partner.data[:target_len]

    # --- add/delete button row factory ---

    def _make_col_buttons(self, tbl, sec, info_label_ref=None, delim_label=""):
        """Create + Add / + Insert / - Delete / - Delete Selected column buttons."""
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        def _refresh_info():
            if info_label_ref is not None:
                info_label_ref.setText(
                    f"Values: {len(sec.data)}  |  Delimiter: {delim_label}"
                )
            self._update_section_info(sec)
            self._status(f"{sec.label}  |  Values: {len(sec.data)}")

        # + Add Column
        btn_add = QPushButton("+ Add Column")
        btn_add.setToolTip("Add a new value column at the end")
        btn_add.setStyleSheet(
            "QPushButton { padding: 5px 14px; border: 1px solid #4CAF50; "
            "color: #4CAF50; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #E8F5E9; }"
        )

        def _add():
            self._updating = True
            col = tbl.columnCount()
            tbl.setColumnCount(col + 1)
            tbl.setHorizontalHeaderItem(col, QTableWidgetItem(str(col)))
            tbl.setItem(0, col, QTableWidgetItem("0"))
            sec.data.append("0")
            self._updating = False
            self._sync_paired_burst_columns(sec)
            _refresh_info()

        btn_add.clicked.connect(_add)
        btn_row.addWidget(btn_add)

        # + Insert at Selection
        btn_insert = QPushButton("+ Insert at Selection")
        btn_insert.setToolTip("Insert a new column before the currently selected column")
        btn_insert.setStyleSheet(
            "QPushButton { padding: 5px 14px; border: 1px solid #2196F3; "
            "color: #2196F3; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #E3F2FD; }"
        )

        def _insert_at():
            sel_cols = sorted(set(idx.column() for idx in tbl.selectedIndexes()))
            if not sel_cols:
                QMessageBox.information(
                    self, "Info", "Select a column first to insert before it."
                )
                return
            insert_col = sel_cols[0]
            self._updating = True
            tbl.insertColumn(insert_col)
            tbl.setItem(0, insert_col, QTableWidgetItem("0"))
            sec.data.insert(insert_col, "0")
            for c in range(tbl.columnCount()):
                tbl.setHorizontalHeaderItem(c, QTableWidgetItem(str(c)))
            self._updating = False
            self._sync_paired_burst_columns(sec)
            _refresh_info()

        btn_insert.clicked.connect(_insert_at)
        btn_row.addWidget(btn_insert)

        # - Delete Last Column
        btn_del = QPushButton("- Delete Last")
        btn_del.setToolTip("Delete the last value column")
        btn_del.setStyleSheet(
            "QPushButton { padding: 5px 14px; border: 1px solid #f44336; "
            "color: #f44336; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #FFEBEE; }"
        )

        def _del():
            if tbl.columnCount() <= 1:
                return
            self._updating = True
            tbl.setColumnCount(tbl.columnCount() - 1)
            if sec.data:
                sec.data.pop()
            self._updating = False
            self._sync_paired_burst_columns(sec)
            _refresh_info()

        btn_del.clicked.connect(_del)
        btn_row.addWidget(btn_del)

        # - Delete Selected
        btn_del_sel = QPushButton("- Delete Selected")
        btn_del_sel.setToolTip("Delete the selected column(s)")
        btn_del_sel.setStyleSheet(
            "QPushButton { padding: 5px 14px; border: 1px solid #e65100; "
            "color: #e65100; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #FFF3E0; }"
        )

        def _del_selected():
            sel_cols = sorted(
                set(idx.column() for idx in tbl.selectedIndexes()), reverse=True
            )
            if not sel_cols:
                QMessageBox.information(self, "Info", "Select column(s) to delete.")
                return
            if tbl.columnCount() - len(sel_cols) < 1:
                QMessageBox.warning(self, "Warning", "Cannot delete all columns.")
                return
            self._updating = True
            for col in sel_cols:
                tbl.removeColumn(col)
                if col < len(sec.data):
                    sec.data.pop(col)
            for c in range(tbl.columnCount()):
                tbl.setHorizontalHeaderItem(c, QTableWidgetItem(str(c)))
            self._updating = False
            self._sync_paired_burst_columns(sec)
            _refresh_info()

        btn_del_sel.clicked.connect(_del_selected)
        btn_row.addWidget(btn_del_sel)

        btn_row.addStretch()
        return btn_row

    def _make_paired_row_buttons(self, tbl, sec):
        """Create + Add Row / - Delete Row for paired Start/End tables."""
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        btn_add = QPushButton("+ Add Row")
        btn_add.setToolTip("Add a new paired (Start, End) row at the bottom")
        btn_add.setStyleSheet(
            "QPushButton { padding: 5px 14px; border: 1px solid #4CAF50; "
            "color: #4CAF50; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #E8F5E9; }"
        )

        def _add_row():
            self._updating = True
            row = tbl.rowCount()
            tbl.setRowCount(row + 1)
            # Column 0 is read-only row number
            it = QTableWidgetItem(str(row + 1))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setBackground(self.COLOR_READONLY)
            tbl.setItem(row, 0, it)
            tbl.setItem(row, 1, QTableWidgetItem("0"))
            tbl.setItem(row, 2, QTableWidgetItem("0"))
            sec.data.extend(["0", "0"])
            self._updating = False
            self._update_section_info(sec)

        btn_add.clicked.connect(_add_row)
        btn_row.addWidget(btn_add)

        btn_del = QPushButton("- Delete Last Row")
        btn_del.setToolTip("Delete the last row")
        btn_del.setStyleSheet(
            "QPushButton { padding: 5px 14px; border: 1px solid #f44336; "
            "color: #f44336; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #FFEBEE; }"
        )

        def _del_row():
            if tbl.rowCount() <= 1:
                return
            self._updating = True
            tbl.setRowCount(tbl.rowCount() - 1)
            if len(sec.data) >= 2:
                sec.data.pop()
                sec.data.pop()
            self._updating = False
            self._update_section_info(sec)

        btn_del.clicked.connect(_del_row)
        btn_row.addWidget(btn_del)

        btn_row.addStretch()
        return btn_row

    # ====================================================================
    # EDITOR DISPATCH
    # ====================================================================

    def _show_editor(self, sec: Section):
        self._clear_editor()
        self._update_section_info(sec)
        st = sec.section_type

        if st in ("event_header",):
            self._ed_text(sec)
        elif st == "model_mode":
            self._ed_model_mode(sec)
        elif st == "storm_params":
            self._ed_storm_params(sec)
        elif st == "burst_ranges":
            self._ed_burst_ranges(sec)
        elif st in ("pluvio_data", "subarea_rain", "pluvio_ref"):
            self._ed_data_table(sec)
        elif st == "hydro_time_ranges":
            self._ed_hydro_ranges(sec)
        elif st == "hydro_station":
            self._ed_hydro_station(sec)
        elif st == "trailer":
            self._ed_text(sec)

        delim_names = {"\t": "TAB", ",": "COMMA", None: "-"}
        d = delim_names.get(sec.delimiter, sec.delimiter or "-")
        status_text = sec.label
        if sec.prefix_line:
            status_text += f" ({sec.prefix_line})"
        self._status(f"{status_text}  |  Delimiter: {d}  |  Values: {len(sec.data)}")

    # ====================================================================
    # EDITOR: Free text (Event Header / Model Mode)
    # ====================================================================

    def _ed_text(self, sec: Section):
        group = QGroupBox(sec.label)
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({self.COLOR_HEADER.red()},{self.COLOR_HEADER.green()},{self.COLOR_HEADER.blue()}); "
            "border: 1px solid #b0c4de; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        desc = QLabel("Edit the text value below. This is a free-text field.")
        desc.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
        lay.addWidget(desc)

        edit = QLineEdit(sec.raw_text)
        edit.setFont(self.MONO)
        edit.setStyleSheet("padding: 5px; border: 1px solid #aaa; border-radius: 3px;")
        edit.textChanged.connect(lambda t: setattr(sec, "raw_text", t))
        lay.addWidget(edit)

        self.editor_lay.addWidget(group)
        self.editor_lay.addStretch()

    # ====================================================================
    # EDITOR: Model Mode (Item 7.2 - FIT or DESIGN)
    # ====================================================================

    def _ed_model_mode(self, sec: Section):
        group = QGroupBox("Model Mode (Item 7.2)")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({self.COLOR_HEADER.red()},{self.COLOR_HEADER.green()},{self.COLOR_HEADER.blue()}); "
            "border: 1px solid #b0c4de; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        desc = QLabel(
            "Type of run: must contain <b>FIT</b> or <b>DESIGN</b> in the "
            "first 6 columns (per RORB manual Item 7.2)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
        lay.addWidget(desc)

        # Buttons for quick selection
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_fit = QPushButton("FIT")
        btn_fit.setToolTip("Set mode to FIT (calibration run)")
        btn_fit.setStyleSheet(
            "QPushButton { padding: 6px 20px; border: 2px solid #1976D2; "
            "color: #1976D2; border-radius: 4px; font-weight: bold; font-size: 10pt; }"
            "QPushButton:hover { background-color: #E3F2FD; }"
        )
        btn_design = QPushButton("DESIGN")
        btn_design.setToolTip("Set mode to DESIGN (design flood run)")
        btn_design.setStyleSheet(
            "QPushButton { padding: 6px 20px; border: 2px solid #388E3C; "
            "color: #388E3C; border-radius: 4px; font-weight: bold; font-size: 10pt; }"
            "QPushButton:hover { background-color: #E8F5E9; }"
        )
        btn_row.addWidget(btn_fit)
        btn_row.addWidget(btn_design)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        # Editable text field (for edge cases or existing non-standard values)
        edit_lbl = QLabel("Current value:")
        edit_lbl.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
        lay.addWidget(edit_lbl)

        edit = QLineEdit(sec.raw_text)
        edit.setFont(self.MONO)
        edit.setStyleSheet(
            "padding: 5px; border: 1px solid #aaa; border-radius: 3px;"
        )

        # Validation label
        warn = QLabel("")
        warn.setStyleSheet("font-weight: normal; font-size: 9pt;")

        def _validate(text):
            sec.raw_text = text
            first6 = text[:6].upper()
            if "FIT" in first6 or "DESIGN" in first6:
                warn.setText("✅ Valid: contains FIT or DESIGN in first 6 columns.")
                warn.setStyleSheet("font-weight: normal; color: #388E3C; font-size: 9pt;")
            else:
                warn.setText("⚠️ Warning: RORB requires FIT or DESIGN in first 6 columns.")
                warn.setStyleSheet("font-weight: normal; color: #e65100; font-size: 9pt;")

        edit.textChanged.connect(_validate)
        _validate(sec.raw_text)  # initial validation

        btn_fit.clicked.connect(lambda: edit.setText("FIT"))
        btn_design.clicked.connect(lambda: edit.setText("DESIGN"))

        lay.addWidget(edit)
        lay.addWidget(warn)

        self.editor_lay.addWidget(group)
        self.editor_lay.addStretch()

    # ====================================================================
    # EDITOR: Storm Parameters (key-value form)
    # ====================================================================

    def _ed_storm_params(self, sec: Section):
        # Comments display
        if sec.comment_lines:
            cmt_group = QGroupBox("File Comments (preserved on save)")
            cmt_group.setStyleSheet(
                "QGroupBox { font-weight: bold; background-color: #fff8e1; "
                "border: 1px solid #ffe082; border-radius: 4px; padding-top: 18px; }"
            )
            cmt_lay = QVBoxLayout(cmt_group)
            for c in sec.comment_lines:
                lbl = QLabel(c)
                lbl.setStyleSheet("color: #555; font-family: Consolas; font-size: 9pt; "
                                  "font-weight: normal;")
                cmt_lay.addWidget(lbl)
            self.editor_lay.addWidget(cmt_group)

        # Parameters form
        form_group = QGroupBox("Storm Parameters")
        form_group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({self.COLOR_PARAM.red()},{self.COLOR_PARAM.green()},{self.COLOR_PARAM.blue()}); "
            "border: 1px solid #ffe082; border-radius: 4px; padding-top: 18px; }"
        )
        form = QFormLayout(form_group)
        form.setSpacing(8)

        for i, val in enumerate(sec.data):
            label = (STORM_PARAM_LABELS[i]
                     if i < len(STORM_PARAM_LABELS)
                     else f"Parameter {i + 1}")
            edit = QLineEdit(val)
            edit.setFont(self.MONO)
            edit.setStyleSheet(
                "padding: 4px; border: 1px solid #aaa; border-radius: 3px;"
            )

            # highlight structural fields
            if i in (2, 3):
                edit.setStyleSheet(
                    "padding: 4px; border: 2px solid #ff9800; border-radius: 3px; "
                    "background-color: #fff3cd;"
                )
                edit.setToolTip(
                    "Changing this value will automatically create or remove "
                    "the matching sections (pluviographs / bursts)."
                )

            def _cb(index=i):
                def _inner(text):
                    if index < len(sec.data):
                        sec.data[index] = text
                    # Auto-sync sections when burst_count or pluvio_count changes
                    if index in (2, 3):
                        self._sync_sections_to_params()
                return _inner
            edit.textChanged.connect(_cb(i))
            form.addRow(label + ":", edit)

        self.editor_lay.addWidget(form_group)
        self.editor_lay.addStretch()

    # ====================================================================
    # EDITOR: Burst Time Ranges
    # ====================================================================

    def _ed_burst_ranges(self, sec: Section):
        group = QGroupBox("Burst Time Ranges")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({self.COLOR_PARAM.red()},{self.COLOR_PARAM.green()},{self.COLOR_PARAM.blue()}); "
            "border: 1px solid #ffe082; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        if sec.inline_comment:
            cmt = QLabel(f"<i>{sec.inline_comment}</i>")
            cmt.setWordWrap(True)
            cmt.setStyleSheet("color: #666; font-size: 9pt; font-weight: normal;")
            lay.addWidget(cmt)

        n = len(sec.data) // 2
        tbl = self._make_table(n, 3)
        tbl.setHorizontalHeaderLabels(["Burst", "Start", "End"])
        tbl.verticalHeader().setVisible(False)

        self._updating = True
        for r in range(n):
            it = QTableWidgetItem(str(r + 1))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setBackground(self.COLOR_READONLY)
            tbl.setItem(r, 0, it)
            s_val = sec.data[r * 2] if r * 2 < len(sec.data) else ""
            tbl.setItem(r, 1, QTableWidgetItem(s_val))
            e_val = sec.data[r * 2 + 1] if r * 2 + 1 < len(sec.data) else ""
            tbl.setItem(r, 2, QTableWidgetItem(e_val))
        self._updating = False

        def _cell(row, col):
            if self._updating or col == 0:
                return
            di = row * 2 + (col - 1)
            if di < len(sec.data):
                sec.data[di] = tbl.item(row, col).text()
        tbl.cellChanged.connect(_cell)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.resizeColumnsToContents()

        lay.addWidget(tbl)

        # Add/Delete Row buttons
        btn_row = self._make_paired_row_buttons(tbl, sec)
        lay.addLayout(btn_row)

        self.editor_lay.addWidget(group)
        self.editor_lay.addStretch()

    # ====================================================================
    # EDITOR: Data tables (pluvio / subarea / pluvioref) with add/delete
    # ====================================================================

    def _ed_data_table(self, sec: Section):
        # Header
        title_text = sec.label

        group = QGroupBox(title_text)
        color = self.COLOR_DATA
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({color.red()},{color.green()},{color.blue()}); "
            "border: 1px solid #a5d6a7; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        # Editable station name for pluviograph sections
        if sec.section_type == "pluvio_data" and sec.prefix_line is not None:
            name_lbl = QLabel("Station ID:")
            name_lbl.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
            lay.addWidget(name_lbl)
            name_edit = QLineEdit(sec.prefix_line)
            name_edit.setFont(self.MONO)
            name_edit.setStyleSheet(
                "padding: 4px; border: 1px solid #aaa; border-radius: 3px;"
            )
            def _update_pluvio_name(text):
                sec.prefix_line = text
            name_edit.textChanged.connect(_update_pluvio_name)
            lay.addWidget(name_edit)

        # Comment lines
        if sec.comment_lines:
            cmt = QLabel("\n".join(sec.comment_lines))
            cmt.setStyleSheet(
                "color: #555; font-family: Consolas; font-size: 9pt; font-weight: normal;"
            )
            lay.addWidget(cmt)

        # Info row
        n = len(sec.data)
        delim_label = "TAB" if sec.delimiter == "\t" else sec.delimiter
        info = QLabel(f"Values: {n}  |  Delimiter: {delim_label}")
        info.setStyleSheet("color: #777; font-weight: normal; font-size: 9pt;")
        lay.addWidget(info)

        # Table - single row of values (auto-expandable on paste)
        tbl = self._make_table(1, n)
        tbl._section = sec
        tbl._info_label = info
        tbl._delim_label = delim_label
        tbl._dialog = self
        tbl.setHorizontalHeaderLabels([str(i) for i in range(n)])
        tbl.verticalHeader().setVisible(False)
        tbl.horizontalHeader().setDefaultSectionSize(75)
        tbl.setMinimumHeight(80)

        self._updating = True
        for c, v in enumerate(sec.data):
            tbl.setItem(0, c, QTableWidgetItem(v))
        self._updating = False

        def _cell(_r, col):
            if not self._updating and col < len(sec.data):
                sec.data[col] = tbl.item(0, col).text()
        tbl.cellChanged.connect(_cell)

        lay.addWidget(tbl, 1)

        # Add/delete column buttons
        btn_layout = self._make_col_buttons(tbl, sec, info, delim_label)
        lay.addLayout(btn_layout)

        self.editor_lay.addWidget(group, 1)

    # ====================================================================
    # EDITOR: Hydrograph Time Ranges
    # ====================================================================

    def _ed_hydro_ranges(self, sec: Section):
        group = QGroupBox("Hydrograph Time Ranges")
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({self.COLOR_HYDRO.red()},{self.COLOR_HYDRO.green()},{self.COLOR_HYDRO.blue()}); "
            "border: 1px solid #ce93d8; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        if sec.comment_lines:
            cmt = QLabel("\n".join(sec.comment_lines))
            cmt.setStyleSheet("color: #555; font-weight: normal; font-size: 9pt;")
            lay.addWidget(cmt)

        n = len(sec.data) // 2
        tbl = self._make_table(n, 3)
        tbl.setHorizontalHeaderLabels(["Station #", "Start", "End"])
        tbl.verticalHeader().setVisible(False)

        self._updating = True
        for r in range(n):
            it = QTableWidgetItem(str(r + 1))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setBackground(self.COLOR_READONLY)
            tbl.setItem(r, 0, it)
            s = sec.data[r * 2] if r * 2 < len(sec.data) else ""
            tbl.setItem(r, 1, QTableWidgetItem(s))
            e = sec.data[r * 2 + 1] if r * 2 + 1 < len(sec.data) else ""
            tbl.setItem(r, 2, QTableWidgetItem(e))
        self._updating = False

        def _cell(row, col):
            if self._updating or col == 0:
                return
            di = row * 2 + (col - 1)
            if di < len(sec.data):
                sec.data[di] = tbl.item(row, col).text()
        tbl.cellChanged.connect(_cell)
        tbl.horizontalHeader().setStretchLastSection(True)
        tbl.resizeColumnsToContents()

        lay.addWidget(tbl)

        # Specialized Add/Delete Row buttons that sync hydro_station sections
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        btn_add = QPushButton("+ Add Station Row")
        btn_add.setToolTip(
            "Add a new time-range row AND create a corresponding Hydro Station section"
        )
        btn_add.setStyleSheet(
            "QPushButton { padding: 5px 14px; border: 1px solid #4CAF50; "
            "color: #4CAF50; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #E8F5E9; }"
        )

        def _add_hydro_row():
            self._updating = True
            row = tbl.rowCount()
            tbl.setRowCount(row + 1)
            it = QTableWidgetItem(str(row + 1))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            it.setBackground(self.COLOR_READONLY)
            tbl.setItem(row, 0, it)
            tbl.setItem(row, 1, QTableWidgetItem("0"))
            tbl.setItem(row, 2, QTableWidgetItem("0"))
            sec.data.extend(["0", "0"])
            self._updating = False
            self._update_section_info(sec)

            # Auto-create a corresponding hydro_station section
            station_num = row + 1
            default_data = ["0"]
            for s in self.sections:
                if s.section_type == "hydro_station" and s.data:
                    default_data = ["0"] * len(s.data)
                    break
            new_station = Section(
                section_type="hydro_station",
                delimiter=",",
                terminator_style="inline",
                prefix_line=f"Station_{station_num}",
                data=list(default_data),
                label=f"Hydro: Station_{station_num}",
            )
            pos = self._find_insert_pos("hydro_station")
            self.sections.insert(pos, new_station)
            self._populate_tree()
            self._update_file_info()
            self._status(
                f"Added time-range row {station_num} + Hydro Station: Station_{station_num}"
            )

        btn_add.clicked.connect(_add_hydro_row)
        btn_row.addWidget(btn_add)

        btn_del = QPushButton("- Delete Last Station Row")
        btn_del.setToolTip(
            "Delete the last time-range row AND the last Hydro Station section"
        )
        btn_del.setStyleSheet(
            "QPushButton { padding: 5px 14px; border: 1px solid #f44336; "
            "color: #f44336; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background-color: #FFEBEE; }"
        )

        def _del_hydro_row():
            if tbl.rowCount() <= 1:
                return
            self._updating = True
            tbl.setRowCount(tbl.rowCount() - 1)
            if len(sec.data) >= 2:
                sec.data.pop()
                sec.data.pop()
            self._updating = False
            self._update_section_info(sec)

            # Auto-delete the last hydro_station section
            last_idx = self._find_last_index("hydro_station")
            if last_idx >= 0:
                removed_label = self.sections[last_idx].label
                self.sections.pop(last_idx)
                self._populate_tree()
                self._update_file_info()
                self._status(f"Removed last time-range row + {removed_label}")

        btn_del.clicked.connect(_del_hydro_row)
        btn_row.addWidget(btn_del)

        btn_row.addStretch()
        lay.addLayout(btn_row)

        self.editor_lay.addWidget(group)
        self.editor_lay.addStretch()

    # ====================================================================
    # EDITOR: Hydrograph Station
    # ====================================================================

    def _ed_hydro_station(self, sec: Section):
        # Station name group
        name_group = QGroupBox("Station Name / ID")
        name_group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({self.COLOR_HYDRO.red()},{self.COLOR_HYDRO.green()},{self.COLOR_HYDRO.blue()}); "
            "border: 1px solid #ce93d8; border-radius: 4px; padding-top: 18px; }"
        )
        name_lay = QVBoxLayout(name_group)
        name_edit = QLineEdit(sec.prefix_line)
        name_edit.setFont(self.MONO)
        name_edit.setStyleSheet(
            "padding: 5px; border: 1px solid #aaa; border-radius: 3px;"
        )
        name_edit.textChanged.connect(
            lambda t: setattr(sec, "prefix_line", t)
        )
        name_lay.addWidget(name_edit)
        self.editor_lay.addWidget(name_group)

        # Flow data group
        n = len(sec.data)
        delim_label = "TAB" if sec.delimiter == "\t" else "COMMA"
        data_group = QGroupBox(f"Flow / Level Values ({n} values)")
        data_group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            "background-color: rgb(240,255,240); "
            "border: 1px solid #a5d6a7; border-radius: 4px; padding-top: 18px; }"
        )
        data_lay = QVBoxLayout(data_group)

        info = QLabel(f"Values: {n}  |  Delimiter: {delim_label}")
        info.setStyleSheet("color: #777; font-weight: normal; font-size: 9pt;")
        data_lay.addWidget(info)

        tbl = self._make_table(1, n)
        tbl._section = sec
        tbl._info_label = info
        tbl._delim_label = delim_label
        tbl._dialog = self
        tbl.setHorizontalHeaderLabels([str(i) for i in range(n)])
        tbl.verticalHeader().setVisible(False)
        tbl.horizontalHeader().setDefaultSectionSize(75)
        tbl.setMinimumHeight(80)

        self._updating = True
        for c, v in enumerate(sec.data):
            tbl.setItem(0, c, QTableWidgetItem(v))
        self._updating = False

        def _cell(_r, col):
            if not self._updating and col < len(sec.data):
                sec.data[col] = tbl.item(0, col).text()
        tbl.cellChanged.connect(_cell)

        data_lay.addWidget(tbl, 1)

        # Add/delete buttons
        delim_label = "TAB" if sec.delimiter == "\t" else "COMMA"
        btn_layout = self._make_col_buttons(tbl, sec, info, delim_label)
        data_lay.addLayout(btn_layout)

        self.editor_lay.addWidget(data_group, 1)

        # Suffix parameters (Item 9.7 – volumes of runoff)
        has_suffix = bool(sec.suffix_lines and any(s.strip() for s in sec.suffix_lines))

        sbox = QGroupBox("Volumes of Runoff (Item 9.7)")
        sbox.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            "background-color: #f5f5f5; "
            "border: 1px solid #ccc; border-radius: 4px; padding-top: 18px; }"
        )
        slay = QVBoxLayout(sbox)

        chk = QCheckBox("Include volumes of runoff for this station")
        chk.setStyleSheet("font-weight: normal; font-size: 9pt;")
        chk.setChecked(has_suffix)
        slay.addWidget(chk)

        desc = QLabel(
            "Comma-delimited proportions of total runoff in consecutive rises.\n"
            "Must end with -99 when values are provided (e.g. 0.6, 0.3, 0.1, -99)."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
        desc.setVisible(has_suffix)
        slay.addWidget(desc)

        if not sec.suffix_lines:
            sec.suffix_lines = [""]

        se = QLineEdit(sec.suffix_lines[0] if sec.suffix_lines else "")
        se.setFont(self.MONO)
        se.setStyleSheet(
            "padding: 4px; border: 1px solid #aaa; border-radius: 3px;"
        )
        se.setVisible(has_suffix)
        se.textChanged.connect(lambda text: sec.suffix_lines.__setitem__(0, text))
        slay.addWidget(se)

        def _toggle_suffix(checked):
            desc.setVisible(checked)
            se.setVisible(checked)
            if not checked:
                se.setText("")
                sec.suffix_lines = [""]
        chk.toggled.connect(_toggle_suffix)

        self.editor_lay.addWidget(sbox)
