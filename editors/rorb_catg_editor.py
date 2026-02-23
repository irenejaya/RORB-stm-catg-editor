"""
RORB CATG (Catchment) File Editor for QGIS
===========================================
A PyQGIS tool to view and edit RORB Catchment (.catg) files with:
  - Section-aware parsing (Intro, Nodes, Reaches, Storages, I/O)
  - Lossless round-trip editing (exact character-position preservation)
  - Table-style display for nodes and reaches
  - Editable print flags for nodes (0/70/71/72) and reaches (0/1)
  - Everything after C END RORB_GE preserved verbatim

File Structure (parsed by section markers):
  - Intro/Header: Title, version, warnings, comments, background image
  - C #NODES:     Node data with coordinates, areas, fractions, print flags
  - C #REACHES:   Reach data with from/to nodes, type, length, slope
  - C #STORAGES:  Reservoir/retarding basin definitions
  - C #INFLOW/OUTFLOW: Channel inflow/outflow definitions
  - C END RORB_GE: End marker for graphical/geographic data
  - Data Block:   Routing instructions, areas, fractions (preserved verbatim)

Node Print Flags:
  0  = No print output
  70 = Print calculated discharge
  71 = Print calculated and actual discharge
  72 = Insert dummy gauging station

Reach Print Flags:
  0  = No print
  1  = Print

Usage:
    Run from QGIS Python console:
        exec(open(r'path/to/RORB_catg_editor.py').read())
"""

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QPushButton, QFileDialog,
    QMessageBox, QWidget,
    QLineEdit, QGroupBox, QAbstractItemView,
    QProgressBar, QFrame, QScrollArea, QApplication,
    QComboBox, QTextEdit,
)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QFont, QColor, QKeySequence


# ============================================================================
#  Constants
# ============================================================================

NODE_PRINT_FLAGS = {
    0:  "No print",
    70: "Print calc discharge",
    71: "Print calc & actual discharge",
    72: "Dummy gauging station",
}

REACH_PRINT_FLAGS = {
    0: "No print",
    1: "Print",
}


# ============================================================================
#  Data Model
# ============================================================================

@dataclass
class NodeData:
    """Parsed node from C #NODES section.

    Each node occupies 2 lines in the comment block:
      Line 1: C  <idx> <x> <y> <scale> <subarea_flag> <unk> <downstream> <name>
              <area> <dci> <ici> <print_flag> <flag2> <flag3>
      Line 2: C <print_location or blank>
    """
    index: int = 0
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    subarea_flag: int = 0
    unknown_flag: int = 0
    downstream: int = 0
    name: str = ""
    area: float = 0.0
    dci: float = 0.0
    ici: float = 0.0
    print_flag: int = 0
    flag2: int = 0
    flag3: int = 0
    raw_line: str = ""              # original first C line (preserved)
    raw_line2: str = ""             # original second C line (preserved)
    print_location: str = ""        # extracted location name
    _original_print_flag: int = 0   # for change tracking
    _original_location: str = ""    # for change tracking


@dataclass
class ReachData:
    """Parsed reach from C #REACHES section.

    Each reach occupies 3 lines:
      Line 1: C  <idx> <name> <from> <to> <unk1> <type> <unk2> <len> <slope>
              <n_coords> <print_flag>
      Line 2: C  <x-coordinates...>
      Line 3: C  <y-coordinates...>
    """
    index: int = 0
    name: str = ""
    from_node: int = 0
    to_node: int = 0
    unknown1: int = 0
    reach_type: int = 0
    unknown2: int = 0
    length: float = 0.0
    slope: float = 0.0
    n_coords: int = 0
    print_flag: int = 0
    raw_lines: List[str] = field(default_factory=list)
    _original_print_flag: int = 0


@dataclass
class StorageData:
    """Parsed storage summary from C #STORAGES section (display only)."""
    index: int = 0
    name: str = ""
    from_node: int = 0
    to_node: int = 0


@dataclass
class CATGFile:
    """Complete parsed CATG file with all sections preserved for lossless save."""
    filepath: str = ""

    # Section 1: Intro (everything before C #NODES)
    intro_lines: List[str] = field(default_factory=list)

    # Section 2: Nodes
    node_header: List[str] = field(default_factory=list)   # "C #NODES" + count line
    nodes: List[NodeData] = field(default_factory=list)
    node_gap: List[str] = field(default_factory=list)      # blank C lines before C #REACHES
    node_count: int = 0

    # Section 3: Reaches
    reach_header: List[str] = field(default_factory=list)  # "C #REACHES" + count line
    reaches: List[ReachData] = field(default_factory=list)
    reach_gap: List[str] = field(default_factory=list)     # blank C lines before next section
    reach_count: int = 0

    # Section 4: Storages (raw lines preserved; parsed for display)
    storage_lines: List[str] = field(default_factory=list)
    storages: List[StorageData] = field(default_factory=list)
    storage_count: int = 0

    # Section 5: Inflow/Outflow (raw lines preserved)
    io_lines: List[str] = field(default_factory=list)
    io_count: int = 0

    # Section 6: END RORB_GE + data block (everything from C END RORB_GE to EOF)
    end_lines: List[str] = field(default_factory=list)


# ============================================================================
#  Parser
# ============================================================================

class CATGParser:
    """Parses a RORB .catg file into a CATGFile structure.

    Uses section markers (C #NODES, C #REACHES, etc.) to split the file,
    then parses nodes and reaches with regex while preserving raw lines
    for lossless round-trip saving.
    """

    NODE_RE = re.compile(
        r'^C\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+'
        r'(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+'
        r'([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+'
        r'(\d+)\s+(\d+)\s+(\d+)\s*$'
    )

    REACH_RE = re.compile(
        r'^C\s+(\d+)\s+(\S+)\s+(\d+)\s+(\d+)\s+'
        r'(\d+)\s+(\d+)\s+(\d+)\s+'
        r'([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s*$'
    )

    STORAGE_HEADER_RE = re.compile(
        r'^C\s+(\d+)\s+(\S+)\s+(\d+)\s+(\d+)'
    )

    @staticmethod
    def _read_lines(filepath: str) -> List[str]:
        """Read file preserving trailing spaces (only strip line endings)."""
        with open(filepath, "r", encoding="utf-8-sig") as f:
            return [line.rstrip('\n\r') for line in f.readlines()]

    @staticmethod
    def _find_marker(lines: List[str], marker: str) -> int:
        """Find the line index where stripped content equals `marker`."""
        for i, line in enumerate(lines):
            if line.strip() == marker:
                return i
        return -1

    @staticmethod
    def _parse_count(line: str) -> int:
        """Extract integer count from a line like 'C    581'."""
        m = re.search(r'(\d+)', line)
        return int(m.group(1)) if m else 0

    def parse(self, filepath: str) -> CATGFile:
        """Parse a .catg file into a CATGFile structure."""
        lines = self._read_lines(filepath)
        catg = CATGFile(filepath=filepath)

        # --- Locate section markers ---
        nodes_idx = self._find_marker(lines, 'C #NODES')
        reaches_idx = self._find_marker(lines, 'C #REACHES')
        storages_idx = self._find_marker(lines, 'C #STORAGES')
        io_idx = self._find_marker(lines, 'C #INFLOW/OUTFLOW')
        end_idx = self._find_marker(lines, 'C END RORB_GE')

        if nodes_idx < 0:
            raise ValueError(
                "Could not find 'C #NODES' marker. "
                "This file may not be a valid RORB CATG file."
            )
        if reaches_idx < 0:
            raise ValueError(
                "Could not find 'C #REACHES' marker. "
                "This file may not be a valid RORB CATG file."
            )

        # --- 1. Intro (everything before C #NODES) ---
        catg.intro_lines = lines[:nodes_idx]

        # --- 2. Nodes ---
        catg.node_header = [lines[nodes_idx], lines[nodes_idx + 1]]
        catg.node_count = self._parse_count(lines[nodes_idx + 1])

        idx = nodes_idx + 2
        for _ in range(catg.node_count):
            if idx + 1 >= len(lines):
                break
            raw_line = lines[idx]
            raw_line2 = lines[idx + 1]
            idx += 2

            m = self.NODE_RE.match(raw_line)
            if m:
                pf = int(m.group(12))
                location = ""
                if pf in (70, 71, 72) and len(raw_line2) > 2:
                    location = raw_line2[2:].strip()

                node = NodeData(
                    index=int(m.group(1)),
                    x=float(m.group(2)),
                    y=float(m.group(3)),
                    scale=float(m.group(4)),
                    subarea_flag=int(m.group(5)),
                    unknown_flag=int(m.group(6)),
                    downstream=int(m.group(7)),
                    name=m.group(8),
                    area=float(m.group(9)),
                    dci=float(m.group(10)),
                    ici=float(m.group(11)),
                    print_flag=pf,
                    flag2=int(m.group(13)),
                    flag3=int(m.group(14)),
                    raw_line=raw_line,
                    raw_line2=raw_line2,
                    print_location=location,
                    _original_print_flag=pf,
                    _original_location=location,
                )
                catg.nodes.append(node)
            else:
                # Fallback: unparseable node — store raw lines
                catg.nodes.append(NodeData(
                    raw_line=raw_line, raw_line2=raw_line2,
                    name="?PARSE_ERR",
                ))

        # Gap between last node and C #REACHES
        catg.node_gap = lines[idx:reaches_idx]

        # --- 3. Reaches ---
        catg.reach_header = [lines[reaches_idx], lines[reaches_idx + 1]]
        catg.reach_count = self._parse_count(lines[reaches_idx + 1])

        idx = reaches_idx + 2
        for _ in range(catg.reach_count):
            if idx >= len(lines):
                break
            raw_header = lines[idx]
            m = self.REACH_RE.match(raw_header)
            if m:
                raw_lines = [raw_header]
                # Read X and Y coordinate lines
                for _ in range(2):
                    idx += 1
                    if idx < len(lines):
                        raw_lines.append(lines[idx])
                idx += 1

                pf = int(m.group(11))
                reach = ReachData(
                    index=int(m.group(1)),
                    name=m.group(2),
                    from_node=int(m.group(3)),
                    to_node=int(m.group(4)),
                    unknown1=int(m.group(5)),
                    reach_type=int(m.group(6)),
                    unknown2=int(m.group(7)),
                    length=float(m.group(8)),
                    slope=float(m.group(9)),
                    n_coords=int(m.group(10)),
                    print_flag=pf,
                    raw_lines=raw_lines,
                    _original_print_flag=pf,
                )
                catg.reaches.append(reach)
            else:
                # Non-matching line — skip (shouldn't happen in valid files)
                idx += 1

        # Gap between last reach and next section
        next_section = storages_idx if storages_idx >= 0 else (
            io_idx if io_idx >= 0 else (
                end_idx if end_idx >= 0 else len(lines)
            )
        )
        catg.reach_gap = lines[idx:next_section]

        # --- 4. Storages ---
        if storages_idx >= 0:
            storage_end = io_idx if io_idx >= 0 else (
                end_idx if end_idx >= 0 else len(lines)
            )
            catg.storage_lines = lines[storages_idx:storage_end]
            if storages_idx + 1 < len(lines):
                catg.storage_count = self._parse_count(lines[storages_idx + 1])

            # Parse storage names for display
            for line in catg.storage_lines[2:]:
                sm = self.STORAGE_HEADER_RE.match(line)
                if sm:
                    catg.storages.append(StorageData(
                        index=int(sm.group(1)),
                        name=sm.group(2),
                        from_node=int(sm.group(3)),
                        to_node=int(sm.group(4)),
                    ))

        # --- 5. Inflow/Outflow ---
        if io_idx >= 0:
            io_end = end_idx if end_idx >= 0 else len(lines)
            catg.io_lines = lines[io_idx:io_end]
            if io_idx + 1 < len(lines):
                catg.io_count = self._parse_count(lines[io_idx + 1])

        # --- 6. END RORB_GE + data block (everything to EOF) ---
        if end_idx >= 0:
            catg.end_lines = lines[end_idx:]

        return catg


# ============================================================================
#  Writer
# ============================================================================

class CATGWriter:
    """Writes a CATGFile back to disk with lossless formatting.

    Only the print flag values (and optionally print locations) are patched
    at exact character positions. All other content is written verbatim from
    the stored raw lines.
    """

    @staticmethod
    def _patch_node_print_flag(raw_line: str, new_flag: int) -> str:
        """Patch the print flag (first of 3 trailing integers) preserving column width.

        The last 3 integer fields on a node line are: print_flag  flag2  flag3
        e.g. '  0  0  0' or ' 70  0  0'.  The total width of (spacing + value)
        for the print flag is kept constant so downstream columns don't shift.
        """
        m = re.search(r'(\s+)(\d+)(\s+\d+\s+\d+)\s*$', raw_line)
        if not m:
            return raw_line
        prefix = raw_line[:m.start()]
        old_spacing = m.group(1)
        old_value = m.group(2)
        suffix = m.group(3)

        total_width = len(old_spacing) + len(old_value)
        new_value = str(new_flag)
        new_spacing = ' ' * max(1, total_width - len(new_value))

        return prefix + new_spacing + new_value + suffix

    @staticmethod
    def _patch_reach_print_flag(raw_line: str, new_flag: int) -> str:
        """Patch the last integer (print flag) on a reach header line."""
        m = re.search(r'(\s+)(\d+)\s*$', raw_line)
        if not m:
            return raw_line
        prefix = raw_line[:m.start()]
        old_spacing = m.group(1)
        old_value = m.group(2)

        total_width = len(old_spacing) + len(old_value)
        new_value = str(new_flag)
        new_spacing = ' ' * max(1, total_width - len(new_value))

        return prefix + new_spacing + new_value

    @staticmethod
    def _reconstruct_line2(node: NodeData) -> str:
        """Reconstruct the second node line (print location or blank).

        Preserves original line width by padding/trimming as needed.
        """
        original_width = len(node.raw_line2) if node.raw_line2 else 52

        if node.print_flag in (70, 71, 72):
            if node.print_location and node.print_location.strip():
                loc_line = "C " + node.print_location
                # Pad to original width
                if len(loc_line) < original_width:
                    loc_line = loc_line.ljust(original_width)
                return loc_line
            else:
                # Flag set but no location — keep original line2
                return node.raw_line2
        else:
            # No print — if was previously a print node, blank out the line
            if node._original_print_flag in (70, 71, 72):
                return "C" + " " * max(0, original_width - 1)
            else:
                return node.raw_line2

    def write(self, catg: CATGFile, filepath: str):
        """Write the CATGFile to disk, patching only changed print flags."""
        out = []

        # 1. Intro
        out.extend(catg.intro_lines)

        # 2. Node header
        out.extend(catg.node_header)

        # 3. Nodes
        for node in catg.nodes:
            # Patch print flag if changed
            if node.print_flag != node._original_print_flag:
                out.append(self._patch_node_print_flag(
                    node.raw_line, node.print_flag
                ))
                out.append(self._reconstruct_line2(node))
                # Update tracking
                node._original_print_flag = node.print_flag
                node._original_location = node.print_location
            elif (node.print_flag in (70, 71, 72) and
                  node.print_location != node._original_location):
                # Location text changed but flag unchanged
                out.append(node.raw_line)
                out.append(self._reconstruct_line2(node))
                node._original_location = node.print_location
            else:
                # No changes — verbatim
                out.append(node.raw_line)
                out.append(node.raw_line2)

        # 4. Node gap
        out.extend(catg.node_gap)

        # 5. Reach header
        out.extend(catg.reach_header)

        # 6. Reaches
        for reach in catg.reaches:
            if reach.print_flag != reach._original_print_flag:
                patched = self._patch_reach_print_flag(
                    reach.raw_lines[0], reach.print_flag
                )
                out.append(patched)
                out.extend(reach.raw_lines[1:])
                reach._original_print_flag = reach.print_flag
            else:
                out.extend(reach.raw_lines)

        # 7. Reach gap
        out.extend(catg.reach_gap)

        # 8. Storages (verbatim)
        out.extend(catg.storage_lines)

        # 9. Inflow/Outflow (verbatim)
        out.extend(catg.io_lines)

        # 10. END RORB_GE + data block (verbatim)
        out.extend(catg.end_lines)

        with open(filepath, "w", encoding="utf-8") as f:
            for line in out:
                f.write(line + "\n")


# ============================================================================
#  CopyPasteTable — QTableWidget with Ctrl+C / Ctrl+V for Excel interop
# ============================================================================

class CopyPasteTable(QTableWidget):
    """QTableWidget with clipboard support for Excel/spreadsheet interop.

    Copy  — selected cells → clipboard as tab-separated text.
    Paste — clipboard text → table starting at current cell (respects editability).
    """

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
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return

        for r, line in enumerate(lines):
            for c, val in enumerate(line.split("\t")):
                row, col = start_row + r, start_col + c
                if row < self.rowCount() and col < self.columnCount():
                    item = self.item(row, col)
                    if item and (item.flags() & Qt.ItemIsEditable):
                        item.setText(val.strip())


# ============================================================================
#  Main Dialog
# ============================================================================

class CATGEditorDialog(QDialog):
    """PyQGIS dialog for viewing and editing RORB Catchment (.catg) files.

    Layout: QSplitter with three panels:
      - LEFT:   Section tree (Intro, Nodes, Reaches, Storages, I/O, Data Block)
      - CENTER: Context-sensitive editor panel (tables, info displays)
      - RIGHT:  Help text, legend, section info, file summary

    Key features:
      - Editable print flags for nodes (0/70/71/72) and reaches (0/1)
      - Filter bar for nodes and reaches tables
      - Batch operations for setting print flags on selected rows
      - Lossless round-trip saving (exact spacing preservation)
    """

    # Colours
    COLOR_INTRO   = QColor(230, 240, 255)   # Light blue
    COLOR_NODE    = QColor(240, 255, 240)    # Light green
    COLOR_REACH   = QColor(255, 248, 220)    # Light yellow
    COLOR_STORAGE = QColor(245, 235, 255)    # Light purple
    COLOR_IO      = QColor(255, 240, 245)    # Light pink
    COLOR_DATA    = QColor(240, 240, 240)    # Light gray
    COLOR_PRINT   = QColor(255, 243, 224)    # Orange tint — print-enabled rows
    COLOR_READONLY = QColor(240, 240, 240)   # Gray — read-only cells
    COLOR_EDITABLE = QColor(255, 255, 255)   # White — editable cells

    MONO = QFont("Consolas", 10)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.catg: Optional[CATGFile] = None
        self.filepath = ""
        self._updating = False    # guard for cellChanged feedback loops

        self.setWindowTitle("RORB CATG Editor")
        self.setMinimumSize(1100, 650)
        self.resize(1400, 800)
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

        # Toolbar
        root.addWidget(self._create_toolbar())

        # Main splitter: tree | editor | help
        self.main_splitter = QSplitter(Qt.Horizontal)

        # LEFT: Section tree
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

        # CENTER: Editor panel
        self.editor_box = QWidget()
        self.editor_lay = QVBoxLayout(self.editor_box)
        self.editor_lay.setContentsMargins(6, 6, 6, 6)
        self.editor_lay.setSpacing(6)
        placeholder = QLabel("Open a CATG file to begin editing.")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setStyleSheet("color: #999; font-size: 14px;")
        self.editor_lay.addWidget(placeholder)

        # RIGHT: Help / info panel
        right_panel = self._create_right_panel()

        self.main_splitter.addWidget(tree_container)
        self.main_splitter.addWidget(self.editor_box)
        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setStretchFactor(2, 0)
        self.main_splitter.setSizes([240, 900, 340])

        root.addWidget(self.main_splitter, 1)

        # Bottom status bar
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

        self.btn_open = QPushButton("  Open CATG")
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

        layout.addWidget(self.btn_open)
        layout.addWidget(self.btn_save)
        layout.addWidget(self.btn_save_as)
        layout.addStretch()
        layout.addWidget(self.lbl_file)
        return group

    # ------------------------------------------------------------------
    # Right help panel
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

        # Title
        title = QLabel("<b>RORB CATG Editor</b>")
        title.setStyleSheet("font-size: 11pt; color: #1976D2;")
        layout.addWidget(title)

        # Help text
        help_text = QLabel(
            "<b>How to use:</b><br>"
            "1. Click <b style='color:#1565C0;'>Open CATG</b> to load a file<br>"
            "2. Navigate sections in the <b>tree</b> on the left<br>"
            "3. View and edit <b>print flags</b> in the tables<br>"
            "4. Use <b>batch buttons</b> to set flags on selected rows<br>"
            "5. Click <b style='color:#2e7d32;'>Save</b> to write back<br><br>"
            "<b>Node Print Flags:</b><br>"
            "<code>0</code> — No print output<br>"
            "<code>70</code> — Print calculated discharge<br>"
            "<code>71</code> — Print calculated &amp; actual discharge<br>"
            "<code>72</code> — Insert dummy gauging station<br><br>"
            "<b>Reach Print Flags:</b><br>"
            "<code>0</code> — No print<br>"
            "<code>1</code> — Print<br><br>"
            "<b>Tips:</b><br>"
            "• <b>Ctrl+C</b> copies selected cells (tab-separated)<br>"
            "• <b>Ctrl+V</b> pastes into editable cells<br>"
            "• Use the <b>filter bar</b> to search nodes by name or index<br>"
            "• Only the <b>C comment block</b> is edited; the data block "
            "(routing instructions) is preserved verbatim<br><br>"
            "<b>Note:</b> After changing print flags, you may need to "
            "regenerate the instruction data in RORB GE for the changes "
            "to take effect in simulations."
        )
        help_text.setWordWrap(True)
        help_text.setTextFormat(Qt.RichText)
        help_text.setStyleSheet("font-size: 9pt;")
        layout.addWidget(help_text)

        # Legend
        legend_group = QGroupBox("Section Colours")
        legend_group.setStyleSheet(
            "QGroupBox { font-weight: bold; background-color: #f5f5f5; }"
        )
        legend_layout = QVBoxLayout()
        legend_layout.setSpacing(3)
        legends = [
            ("Intro / Header", self.COLOR_INTRO, "Title, version, comments"),
            ("Nodes",          self.COLOR_NODE,   "Node data with coordinates and flags"),
            ("Reaches",        self.COLOR_REACH,  "Reach routing data"),
            ("Storages",       self.COLOR_STORAGE, "Reservoir / retarding basin data"),
            ("Inflow/Outflow", self.COLOR_IO,     "Channel inflow/outflow definitions"),
            ("Data Block",     self.COLOR_DATA,   "Routing instructions (preserved verbatim)"),
            ("Print-enabled",  self.COLOR_PRINT,  "Rows with non-zero print flag"),
        ]
        for text, color, tip in legends:
            lbl = QLabel(f"  {text}")
            lbl.setStyleSheet(
                f"background-color: rgb({color.red()},{color.green()},{color.blue()}); "
                "padding: 3px 8px; border: 1px solid #ccc; border-radius: 2px; "
                "font-size: 9pt;"
            )
            lbl.setToolTip(tip)
            legend_layout.addWidget(lbl)
        legend_group.setLayout(legend_layout)
        layout.addWidget(legend_group)

        # Section info (updates on selection)
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

        # File info (updates on open)
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
        self.lbl_status = QLabel("Ready — open a CATG file to begin")
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
        self.btn_open.clicked.connect(self._on_open)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_save_as.clicked.connect(self._on_save_as)
        self.tree.currentItemChanged.connect(self._on_tree_changed)

    # ====================================================================
    # FILE OPERATIONS
    # ====================================================================

    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open RORB Catchment File", "",
            "Catchment Files (*.catg);;All Files (*)",
        )
        if not path:
            return

        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(10)
        QApplication.processEvents()

        try:
            parser = CATGParser()
            self.catg = parser.parse(path)
            self.filepath = path
            self.lbl_file.setText(os.path.basename(path))
            self.lbl_file.setStyleSheet(
                "color: #333; font-weight: bold; font-size: 10pt; padding-left: 12px;"
            )
            self.btn_save.setEnabled(True)
            self.btn_save_as.setEnabled(True)

            self.progress_bar.setValue(60)
            QApplication.processEvents()

            self._populate_tree()

            self.progress_bar.setValue(90)
            QApplication.processEvents()

            # Count print points
            print_nodes = sum(
                1 for n in self.catg.nodes if n.print_flag in (70, 71, 72)
            )
            print_reaches = sum(
                1 for r in self.catg.reaches if r.print_flag != 0
            )

            self._status(
                f"Loaded  |  Nodes: {self.catg.node_count}  |  "
                f"Reaches: {self.catg.reach_count}  |  "
                f"Storages: {self.catg.storage_count}  |  "
                f"Print nodes: {print_nodes}  |  Print reaches: {print_reaches}"
            )

            self._update_file_info()

            self.progress_bar.setValue(100)
            QTimer.singleShot(1200, lambda: self.progress_bar.setVisible(False))

        except Exception as exc:
            self.progress_bar.setVisible(False)
            QMessageBox.critical(
                self, "Parse Error",
                f"Failed to parse CATG file:\n\n{exc}"
            )

    def _on_save(self):
        if not self.filepath:
            return self._on_save_as()
        self._write(self.filepath)

    def _on_save_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save RORB Catchment File", self.filepath,
            "Catchment Files (*.catg);;All Files (*)",
        )
        if path:
            self.filepath = path
            self.lbl_file.setText(os.path.basename(path))
            self._write(path)

    def _write(self, path: str):
        if not self.catg:
            return
        try:
            CATGWriter().write(self.catg, path)
            self.catg.filepath = path
            self._status(f"Saved successfully → {path}")
        except Exception as exc:
            QMessageBox.critical(
                self, "Save Error",
                f"Failed to save:\n\n{exc}"
            )

    # ====================================================================
    # TREE MANAGEMENT
    # ====================================================================

    def _populate_tree(self):
        self.tree.clear()
        if not self.catg:
            return

        catg = self.catg
        print_nodes = sum(
            1 for n in catg.nodes if n.print_flag in (70, 71, 72)
        )
        print_reaches = sum(1 for r in catg.reaches if r.print_flag != 0)

        items = [
            ("intro",    f"Header / Intro ({len(catg.intro_lines)} lines)"),
            ("nodes",    f"Nodes ({catg.node_count})  [{print_nodes} print]"),
            ("reaches",  f"Reaches ({catg.reach_count})  [{print_reaches} print]"),
            ("storages", f"Storages ({catg.storage_count})"),
            ("io",       f"Inflow/Outflow ({catg.io_count})"),
            ("data",     "Data Block"),
        ]

        for key, label in items:
            item = QTreeWidgetItem(self.tree)
            item.setText(0, label)
            item.setData(0, Qt.UserRole, key)
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)

        self.tree.expandAll()

    def _on_tree_changed(self, current, _previous):
        if current is None:
            return
        key = current.data(0, Qt.UserRole)
        if not key or not self.catg:
            return
        self._show_editor(key)

    # ====================================================================
    # EDITOR UTILITIES
    # ====================================================================

    def _clear_editor(self):
        """Remove all widgets from the editor panel."""
        while self.editor_lay.count():
            child = self.editor_lay.takeAt(0)
            w = child.widget()
            if w:
                w.deleteLater()

    def _status(self, text: str):
        self.lbl_status.setText(text)

    def _make_table(self, rows, cols, editable=True):
        """Create a CopyPasteTable with consistent styling."""
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

    def _update_file_info(self):
        """Update the right-panel file summary."""
        if not self.catg:
            return
        c = self.catg
        print_nodes = sum(
            1 for n in c.nodes if n.print_flag in (70, 71, 72)
        )
        print_reaches = sum(1 for r in c.reaches if r.print_flag != 0)
        fname = os.path.basename(c.filepath) if c.filepath else "Unknown"

        # Count instruction lines in data block
        data_instructions = 0
        data_print_points = 0
        for line in c.end_lines:
            s = line.strip()
            if s and not s.startswith('C') and len(s) > 0:
                # Check for code 7 (PRINT instruction)
                m = re.match(r'^7\s*[,\s]', s)
                if m:
                    data_print_points += 1
                # Count non-comment, non-empty lines as instructions
                if s[0].isdigit() or s.startswith('-'):
                    data_instructions += 1

        self.file_info_label.setText(
            f"<b>File:</b> {fname}<br>"
            f"<b>Nodes:</b> {c.node_count}<br>"
            f"<b>Reaches:</b> {c.reach_count}<br>"
            f"<b>Storages:</b> {c.storage_count}<br>"
            f"<b>Inflow/Outflow:</b> {c.io_count}<br>"
            f"<b>Print nodes:</b> {print_nodes}<br>"
            f"<b>Print reaches:</b> {print_reaches}<br>"
            f"<b>Data block print points:</b> {data_print_points}<br>"
            f"<b>Path:</b> <span style='font-size:8pt;'>{c.filepath}</span>"
        )

    # ====================================================================
    # EDITOR DISPATCH
    # ====================================================================

    def _show_editor(self, key: str):
        self._clear_editor()

        dispatchers = {
            "intro":    self._ed_intro,
            "nodes":    self._ed_nodes,
            "reaches":  self._ed_reaches,
            "storages": self._ed_storages,
            "io":       self._ed_io,
            "data":     self._ed_data_block,
        }
        fn = dispatchers.get(key)
        if fn:
            fn()

    # ====================================================================
    # EDITOR: Intro / Header
    # ====================================================================

    def _ed_intro(self):
        group = QGroupBox("Header / Intro")
        c = self.COLOR_INTRO
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({c.red()},{c.green()},{c.blue()}); "
            "border: 1px solid #b0c4de; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        desc = QLabel(
            "File header including title, RORB GE version, warnings, "
            "file comments, sub-area area comments, impervious fraction "
            "comments, and background image settings.<br>"
            "<b>Read-only</b> — edit in RORB GE."
        )
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.RichText)
        desc.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
        lay.addWidget(desc)

        info = QLabel(f"Lines: {len(self.catg.intro_lines)}")
        info.setStyleSheet("color: #777; font-weight: normal; font-size: 9pt;")
        lay.addWidget(info)

        text_edit = QTextEdit()
        text_edit.setFont(self.MONO)
        text_edit.setReadOnly(True)
        text_edit.setPlainText("\n".join(self.catg.intro_lines))
        text_edit.setMinimumHeight(300)
        text_edit.setStyleSheet(
            "QTextEdit { background-color: #fafafa; border: 1px solid #ccc; "
            "border-radius: 3px; }"
        )
        lay.addWidget(text_edit, 1)

        self.editor_lay.addWidget(group, 1)

        self.info_label.setText(
            f"<b>Section:</b> Header / Intro<br>"
            f"<b>Lines:</b> {len(self.catg.intro_lines)}<br>"
            f"<b>Status:</b> Read-only"
        )

    # ====================================================================
    # EDITOR: Nodes — big table with editable print flags
    # ====================================================================

    def _ed_nodes(self):
        catg = self.catg

        # --- Group box ---
        group = QGroupBox("Nodes")
        c = self.COLOR_NODE
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({c.red()},{c.green()},{c.blue()}); "
            "border: 1px solid #a5d6a7; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        # --- Filter bar ---
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)

        search = QLineEdit()
        search.setPlaceholderText("Search by name or index...")
        search.setStyleSheet(
            "padding: 4px 8px; border: 1px solid #aaa; border-radius: 3px;"
        )
        search.setMaximumWidth(250)

        flag_filter = QComboBox()
        flag_filter.addItems([
            "All Nodes",
            "Print Nodes Only (flag ≠ 0)",
            "Non-Print Nodes Only (flag = 0)",
            "Sub-area Nodes Only",
        ])
        flag_filter.setStyleSheet("padding: 4px; font-size: 9pt;")

        count_label = QLabel(f"Showing {catg.node_count} of {catg.node_count} nodes")
        count_label.setStyleSheet(
            "color: #777; font-weight: normal; font-size: 9pt;"
        )

        filter_row.addWidget(QLabel("Filter:"))
        filter_row.addWidget(search)
        filter_row.addWidget(flag_filter)
        filter_row.addStretch()
        filter_row.addWidget(count_label)
        lay.addLayout(filter_row)

        # --- Table ---
        COLS = [
            "Index", "Name", "X", "Y", "SubArea", "Downstream",
            "Area", "DCI", "ICI", "Print Flag", "Print Location",
        ]
        PRINT_COL = 9
        LOC_COL = 10

        tbl = self._make_table(len(catg.nodes), len(COLS), editable=True)
        tbl.setHorizontalHeaderLabels(COLS)
        tbl.verticalHeader().setVisible(False)

        # Column widths
        col_widths = [60, 100, 80, 80, 60, 85, 85, 80, 80, 90, 200]
        for i, w in enumerate(col_widths):
            tbl.setColumnWidth(i, w)

        # --- Populate ---
        self._updating = True
        for row, node in enumerate(catg.nodes):
            values = [
                str(node.index), node.name,
                f"{node.x:.3f}", f"{node.y:.3f}",
                str(node.subarea_flag), str(node.downstream),
                f"{node.area:.6f}", f"{node.dci:.6f}", f"{node.ici:.6f}",
                str(node.print_flag), node.print_location,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col not in (PRINT_COL, LOC_COL):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setBackground(self.COLOR_READONLY)
                elif col == LOC_COL and node.print_flag == 0:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setBackground(self.COLOR_READONLY)
                else:
                    item.setBackground(self.COLOR_EDITABLE)
                tbl.setItem(row, col, item)

            # Highlight print-enabled rows
            if node.print_flag in (70, 71, 72):
                for col in range(len(COLS)):
                    it = tbl.item(row, col)
                    if it and col not in (PRINT_COL, LOC_COL):
                        it.setBackground(self.COLOR_PRINT)
                    elif it and col == PRINT_COL:
                        it.setBackground(QColor(255, 224, 178))  # deeper orange
        self._updating = False

        # --- Helper: colour a single row ---
        def _color_row(r):
            nd = catg.nodes[r]
            is_print = nd.print_flag in (70, 71, 72)
            for col in range(len(COLS)):
                it = tbl.item(r, col)
                if not it:
                    continue
                if col == PRINT_COL:
                    it.setBackground(
                        QColor(255, 224, 178) if is_print else self.COLOR_EDITABLE
                    )
                elif col == LOC_COL:
                    if is_print:
                        it.setFlags(it.flags() | Qt.ItemIsEditable)
                        it.setBackground(self.COLOR_EDITABLE)
                    else:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                        it.setBackground(self.COLOR_READONLY)
                        it.setText("")
                else:
                    it.setBackground(
                        self.COLOR_PRINT if is_print else self.COLOR_READONLY
                    )

        # --- Cell change handler ---
        def _on_cell_changed(r, col):
            if self._updating:
                return
            if r < 0 or r >= len(catg.nodes):
                return
            node = catg.nodes[r]

            if col == PRINT_COL:
                text = tbl.item(r, PRINT_COL).text().strip()
                try:
                    val = int(text)
                except ValueError:
                    # Revert
                    self._updating = True
                    tbl.item(r, PRINT_COL).setText(str(node.print_flag))
                    self._updating = False
                    self._status("Invalid print flag value — must be 0, 70, 71, or 72")
                    return

                if val not in NODE_PRINT_FLAGS:
                    self._updating = True
                    tbl.item(r, PRINT_COL).setText(str(node.print_flag))
                    self._updating = False
                    self._status(
                        f"Invalid print flag: {val}. "
                        "Valid values: 0, 70, 71, 72"
                    )
                    return

                node.print_flag = val
                self._updating = True
                _color_row(r)
                self._updating = False
                self._update_file_info()
                self._status(
                    f"Node {node.index} ({node.name}): "
                    f"print flag → {val} ({NODE_PRINT_FLAGS[val]})"
                )

            elif col == LOC_COL:
                node.print_location = tbl.item(r, LOC_COL).text().strip()

        tbl.cellChanged.connect(_on_cell_changed)
        lay.addWidget(tbl, 1)

        # --- Batch operation buttons ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        batch_style = (
            "QPushButton {{ padding: 5px 12px; border: 1px solid {0}; "
            "color: {0}; border-radius: 3px; font-weight: bold; font-size: 8pt; }}"
            "QPushButton:hover {{ background-color: {1}; }}"
        )

        def _set_selected_flag(flag_val):
            """Set print flag for all selected rows."""
            selected_rows = sorted(set(idx.row() for idx in tbl.selectedIndexes()))
            if not selected_rows:
                self._status("Select rows first, then apply batch operation")
                return
            self._updating = True
            for r in selected_rows:
                if r < len(catg.nodes):
                    catg.nodes[r].print_flag = flag_val
                    tbl.item(r, PRINT_COL).setText(str(flag_val))
                    _color_row(r)
            self._updating = False
            self._update_file_info()
            self._status(
                f"Set {len(selected_rows)} node(s) → "
                f"{flag_val} ({NODE_PRINT_FLAGS[flag_val]})"
            )

        btn_clear = QPushButton("Clear → 0")
        btn_clear.setToolTip("Set selected nodes to 0 (no print)")
        btn_clear.setStyleSheet(batch_style.format("#757575", "#EEEEEE"))
        btn_clear.clicked.connect(lambda: _set_selected_flag(0))
        btn_row.addWidget(btn_clear)

        btn_70 = QPushButton("Set → 70")
        btn_70.setToolTip("Set selected nodes to 70 (print calc discharge)")
        btn_70.setStyleSheet(batch_style.format("#FF9800", "#FFF3E0"))
        btn_70.clicked.connect(lambda: _set_selected_flag(70))
        btn_row.addWidget(btn_70)

        btn_71 = QPushButton("Set → 71")
        btn_71.setToolTip("Set selected nodes to 71 (print calc & actual)")
        btn_71.setStyleSheet(batch_style.format("#F57C00", "#FFE0B2"))
        btn_71.clicked.connect(lambda: _set_selected_flag(71))
        btn_row.addWidget(btn_71)

        btn_72 = QPushButton("Set → 72")
        btn_72.setToolTip("Set selected nodes to 72 (dummy gauging station)")
        btn_72.setStyleSheet(batch_style.format("#E65100", "#FFE0B2"))
        btn_72.clicked.connect(lambda: _set_selected_flag(72))
        btn_row.addWidget(btn_72)

        btn_row.addWidget(QLabel("  │  "))

        btn_select_print = QPushButton("Select All Print Nodes")
        btn_select_print.setToolTip("Select all rows with non-zero print flags")
        btn_select_print.setStyleSheet(batch_style.format("#1976D2", "#E3F2FD"))

        def _select_print_nodes():
            tbl.clearSelection()
            for r in range(tbl.rowCount()):
                if not tbl.isRowHidden(r) and catg.nodes[r].print_flag in (70, 71, 72):
                    for col in range(len(COLS)):
                        tbl.item(r, col).setSelected(True)
            n = sum(1 for nd in catg.nodes if nd.print_flag in (70, 71, 72))
            self._status(f"Selected {n} print node(s)")

        btn_select_print.clicked.connect(_select_print_nodes)
        btn_row.addWidget(btn_select_print)

        btn_row.addStretch()
        lay.addLayout(btn_row)

        # --- Filter logic ---
        def _apply_filter():
            search_text = search.text().lower()
            filter_mode = flag_filter.currentIndex()
            visible_count = 0

            for r in range(tbl.rowCount()):
                show = True
                if r < len(catg.nodes):
                    nd = catg.nodes[r]
                    # Text search
                    if search_text:
                        if (search_text not in nd.name.lower() and
                                search_text not in str(nd.index) and
                                search_text not in nd.print_location.lower()):
                            show = False
                    # Flag filter
                    if filter_mode == 1 and nd.print_flag == 0:
                        show = False
                    elif filter_mode == 2 and nd.print_flag != 0:
                        show = False
                    elif filter_mode == 3 and nd.subarea_flag != 1:
                        show = False

                tbl.setRowHidden(r, not show)
                if show:
                    visible_count += 1

            count_label.setText(
                f"Showing {visible_count} of {catg.node_count} nodes"
            )

        search.textChanged.connect(lambda: _apply_filter())
        flag_filter.currentIndexChanged.connect(lambda: _apply_filter())

        self.editor_lay.addWidget(group, 1)

        # Update section info
        print_nodes = sum(
            1 for n in catg.nodes if n.print_flag in (70, 71, 72)
        )
        self.info_label.setText(
            f"<b>Section:</b> Nodes<br>"
            f"<b>Total nodes:</b> {catg.node_count}<br>"
            f"<b>Print nodes:</b> {print_nodes}<br>"
            f"<b>Sub-area nodes:</b> "
            f"{sum(1 for n in catg.nodes if n.subarea_flag == 1)}<br>"
            f"<b>Junction nodes:</b> "
            f"{sum(1 for n in catg.nodes if n.subarea_flag == 0)}<br>"
            f"<b>Editable:</b> Print Flag, Print Location"
        )

    # ====================================================================
    # EDITOR: Reaches — table with editable print flag
    # ====================================================================

    def _ed_reaches(self):
        catg = self.catg

        # --- Group box ---
        group = QGroupBox("Reaches")
        c = self.COLOR_REACH
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({c.red()},{c.green()},{c.blue()}); "
            "border: 1px solid #ffe082; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        # --- Filter bar ---
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)

        search = QLineEdit()
        search.setPlaceholderText("Search by name or index...")
        search.setStyleSheet(
            "padding: 4px 8px; border: 1px solid #aaa; border-radius: 3px;"
        )
        search.setMaximumWidth(250)

        flag_filter = QComboBox()
        flag_filter.addItems([
            "All Reaches",
            "Print Reaches Only (flag = 1)",
            "Non-Print Reaches Only (flag = 0)",
        ])
        flag_filter.setStyleSheet("padding: 4px; font-size: 9pt;")

        count_label = QLabel(
            f"Showing {catg.reach_count} of {catg.reach_count} reaches"
        )
        count_label.setStyleSheet(
            "color: #777; font-weight: normal; font-size: 9pt;"
        )

        filter_row.addWidget(QLabel("Filter:"))
        filter_row.addWidget(search)
        filter_row.addWidget(flag_filter)
        filter_row.addStretch()
        filter_row.addWidget(count_label)
        lay.addLayout(filter_row)

        # --- Table ---
        COLS = [
            "Index", "Name", "From Node", "To Node",
            "Type", "Length", "Slope", "Print Flag",
        ]
        PRINT_COL = 7

        tbl = self._make_table(len(catg.reaches), len(COLS), editable=True)
        tbl.setHorizontalHeaderLabels(COLS)
        tbl.verticalHeader().setVisible(False)

        col_widths = [60, 160, 80, 80, 50, 90, 90, 80]
        for i, w in enumerate(col_widths):
            tbl.setColumnWidth(i, w)

        # --- Populate ---
        self._updating = True
        for row, reach in enumerate(catg.reaches):
            values = [
                str(reach.index), reach.name,
                str(reach.from_node), str(reach.to_node),
                str(reach.reach_type),
                f"{reach.length:.3f}", f"{reach.slope:.3f}",
                str(reach.print_flag),
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col != PRINT_COL:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setBackground(self.COLOR_READONLY)
                else:
                    item.setBackground(self.COLOR_EDITABLE)
                tbl.setItem(row, col, item)

            # Highlight print-enabled rows
            if reach.print_flag != 0:
                for col in range(len(COLS)):
                    it = tbl.item(row, col)
                    if it and col != PRINT_COL:
                        it.setBackground(self.COLOR_PRINT)
                    elif it and col == PRINT_COL:
                        it.setBackground(QColor(255, 224, 178))
        self._updating = False

        # --- Helper: colour a single row ---
        def _color_row(r):
            rd = catg.reaches[r]
            is_print = rd.print_flag != 0
            for col in range(len(COLS)):
                it = tbl.item(r, col)
                if not it:
                    continue
                if col == PRINT_COL:
                    it.setBackground(
                        QColor(255, 224, 178) if is_print else self.COLOR_EDITABLE
                    )
                else:
                    it.setBackground(
                        self.COLOR_PRINT if is_print else self.COLOR_READONLY
                    )

        # --- Cell change handler ---
        def _on_cell_changed(r, col):
            if self._updating:
                return
            if col != PRINT_COL or r < 0 or r >= len(catg.reaches):
                return
            reach = catg.reaches[r]
            text = tbl.item(r, PRINT_COL).text().strip()
            try:
                val = int(text)
            except ValueError:
                self._updating = True
                tbl.item(r, PRINT_COL).setText(str(reach.print_flag))
                self._updating = False
                self._status("Invalid print flag — must be 0 or 1")
                return

            if val not in REACH_PRINT_FLAGS:
                self._updating = True
                tbl.item(r, PRINT_COL).setText(str(reach.print_flag))
                self._updating = False
                self._status(f"Invalid print flag: {val}. Valid values: 0, 1")
                return

            reach.print_flag = val
            self._updating = True
            _color_row(r)
            self._updating = False
            self._update_file_info()
            self._status(
                f"Reach {reach.index} ({reach.name}): "
                f"print flag → {val} ({REACH_PRINT_FLAGS[val]})"
            )

        tbl.cellChanged.connect(_on_cell_changed)
        lay.addWidget(tbl, 1)

        # --- Batch buttons ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        batch_style = (
            "QPushButton {{ padding: 5px 12px; border: 1px solid {0}; "
            "color: {0}; border-radius: 3px; font-weight: bold; font-size: 8pt; }}"
            "QPushButton:hover {{ background-color: {1}; }}"
        )

        def _set_selected_flag(flag_val):
            selected_rows = sorted(set(idx.row() for idx in tbl.selectedIndexes()))
            if not selected_rows:
                self._status("Select rows first, then apply batch operation")
                return
            self._updating = True
            for r in selected_rows:
                if r < len(catg.reaches):
                    catg.reaches[r].print_flag = flag_val
                    tbl.item(r, PRINT_COL).setText(str(flag_val))
                    _color_row(r)
            self._updating = False
            self._update_file_info()
            self._status(
                f"Set {len(selected_rows)} reach(es) → "
                f"{flag_val} ({REACH_PRINT_FLAGS[flag_val]})"
            )

        btn_clear = QPushButton("Clear → 0")
        btn_clear.setToolTip("Set selected reaches to 0 (no print)")
        btn_clear.setStyleSheet(batch_style.format("#757575", "#EEEEEE"))
        btn_clear.clicked.connect(lambda: _set_selected_flag(0))
        btn_row.addWidget(btn_clear)

        btn_1 = QPushButton("Set → 1")
        btn_1.setToolTip("Set selected reaches to 1 (print)")
        btn_1.setStyleSheet(batch_style.format("#FF9800", "#FFF3E0"))
        btn_1.clicked.connect(lambda: _set_selected_flag(1))
        btn_row.addWidget(btn_1)

        btn_row.addWidget(QLabel("  │  "))

        btn_select_print = QPushButton("Select All Print Reaches")
        btn_select_print.setToolTip("Select all rows with print flag = 1")
        btn_select_print.setStyleSheet(batch_style.format("#1976D2", "#E3F2FD"))

        def _select_print_reaches():
            tbl.clearSelection()
            for r in range(tbl.rowCount()):
                if not tbl.isRowHidden(r) and catg.reaches[r].print_flag != 0:
                    for col in range(len(COLS)):
                        tbl.item(r, col).setSelected(True)
            n = sum(1 for rd in catg.reaches if rd.print_flag != 0)
            self._status(f"Selected {n} print reach(es)")

        btn_select_print.clicked.connect(_select_print_reaches)
        btn_row.addWidget(btn_select_print)

        btn_row.addStretch()
        lay.addLayout(btn_row)

        # --- Filter logic ---
        def _apply_filter():
            search_text = search.text().lower()
            filter_mode = flag_filter.currentIndex()
            visible_count = 0

            for r in range(tbl.rowCount()):
                show = True
                if r < len(catg.reaches):
                    rd = catg.reaches[r]
                    if search_text:
                        if (search_text not in rd.name.lower() and
                                search_text not in str(rd.index)):
                            show = False
                    if filter_mode == 1 and rd.print_flag == 0:
                        show = False
                    elif filter_mode == 2 and rd.print_flag != 0:
                        show = False

                tbl.setRowHidden(r, not show)
                if show:
                    visible_count += 1

            count_label.setText(
                f"Showing {visible_count} of {catg.reach_count} reaches"
            )

        search.textChanged.connect(lambda: _apply_filter())
        flag_filter.currentIndexChanged.connect(lambda: _apply_filter())

        self.editor_lay.addWidget(group, 1)

        # Update section info
        print_reaches = sum(1 for r in catg.reaches if r.print_flag != 0)
        reach_types = {}
        for r in catg.reaches:
            reach_types[r.reach_type] = reach_types.get(r.reach_type, 0) + 1
        type_str = ", ".join(
            f"Type {t}: {c}" for t, c in sorted(reach_types.items())
        )
        self.info_label.setText(
            f"<b>Section:</b> Reaches<br>"
            f"<b>Total reaches:</b> {catg.reach_count}<br>"
            f"<b>Print reaches:</b> {print_reaches}<br>"
            f"<b>Reach types:</b> {type_str}<br>"
            f"<b>Editable:</b> Print Flag"
        )

    # ====================================================================
    # EDITOR: Storages — read-only info display
    # ====================================================================

    def _ed_storages(self):
        catg = self.catg

        group = QGroupBox("Storages")
        c = self.COLOR_STORAGE
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({c.red()},{c.green()},{c.blue()}); "
            "border: 1px solid #ce93d8; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        desc = QLabel(
            f"<b>{catg.storage_count}</b> storage(s) defined "
            f"({len(catg.storage_lines)} raw lines).<br>"
            "<b>Read-only</b> — edit storage parameters in RORB GE."
        )
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.RichText)
        desc.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
        lay.addWidget(desc)

        if catg.storages:
            # Summary table
            tbl = self._make_table(
                len(catg.storages), 4, editable=False
            )
            tbl.setHorizontalHeaderLabels(
                ["Index", "Name", "From Node", "To Node"]
            )
            tbl.verticalHeader().setVisible(False)
            tbl.setMaximumHeight(min(200, 30 + len(catg.storages) * 28))

            for row, st in enumerate(catg.storages):
                for col, val in enumerate([
                    str(st.index), st.name,
                    str(st.from_node), str(st.to_node),
                ]):
                    item = QTableWidgetItem(val)
                    item.setBackground(self.COLOR_READONLY)
                    tbl.setItem(row, col, item)

            tbl.resizeColumnsToContents()
            lay.addWidget(tbl)

        # Raw text
        if catg.storage_lines:
            raw_group = QGroupBox("Raw Storage Data")
            raw_group.setStyleSheet(
                "QGroupBox { font-weight: normal; background-color: #f5f5f5; "
                "border: 1px solid #ccc; border-radius: 4px; padding-top: 18px; }"
            )
            raw_lay = QVBoxLayout(raw_group)

            text_edit = QTextEdit()
            text_edit.setFont(self.MONO)
            text_edit.setReadOnly(True)
            text_edit.setPlainText("\n".join(catg.storage_lines))
            text_edit.setStyleSheet(
                "QTextEdit { background-color: #fafafa; border: 1px solid #ccc; "
                "border-radius: 3px; font-size: 9pt; }"
            )
            raw_lay.addWidget(text_edit, 1)
            lay.addWidget(raw_group, 1)
        else:
            lay.addStretch()

        self.editor_lay.addWidget(group, 1)

        self.info_label.setText(
            f"<b>Section:</b> Storages<br>"
            f"<b>Storage count:</b> {catg.storage_count}<br>"
            f"<b>Raw lines:</b> {len(catg.storage_lines)}<br>"
            f"<b>Status:</b> Read-only"
        )

    # ====================================================================
    # EDITOR: Inflow/Outflow — read-only info display
    # ====================================================================

    def _ed_io(self):
        catg = self.catg

        group = QGroupBox("Inflow / Outflow")
        c = self.COLOR_IO
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({c.red()},{c.green()},{c.blue()}); "
            "border: 1px solid #f8bbd0; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        desc = QLabel(
            f"<b>{catg.io_count}</b> inflow/outflow definition(s) "
            f"({len(catg.io_lines)} raw lines).<br>"
            "<b>Read-only</b> — edit in RORB GE."
        )
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.RichText)
        desc.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
        lay.addWidget(desc)

        if catg.io_lines:
            text_edit = QTextEdit()
            text_edit.setFont(self.MONO)
            text_edit.setReadOnly(True)
            text_edit.setPlainText("\n".join(catg.io_lines))
            text_edit.setMaximumHeight(200)
            text_edit.setStyleSheet(
                "QTextEdit { background-color: #fafafa; border: 1px solid #ccc; "
                "border-radius: 3px; font-size: 9pt; }"
            )
            lay.addWidget(text_edit)

        lay.addStretch()
        self.editor_lay.addWidget(group, 1)

        self.info_label.setText(
            f"<b>Section:</b> Inflow / Outflow<br>"
            f"<b>Count:</b> {catg.io_count}<br>"
            f"<b>Raw lines:</b> {len(catg.io_lines)}<br>"
            f"<b>Status:</b> Read-only"
        )

    # ====================================================================
    # EDITOR: Data Block — read-only summary + raw text
    # ====================================================================

    def _ed_data_block(self):
        catg = self.catg

        group = QGroupBox("Data Block (Post C END RORB_GE)")
        c = self.COLOR_DATA
        group.setStyleSheet(
            "QGroupBox { font-weight: bold; font-size: 10pt; "
            f"background-color: rgb({c.red()},{c.green()},{c.blue()}); "
            "border: 1px solid #ccc; border-radius: 4px; padding-top: 18px; }"
        )
        lay = QVBoxLayout(group)

        # Analyse data block
        total_lines = len(catg.end_lines)
        comment_lines = sum(1 for l in catg.end_lines if l.strip().startswith('C'))
        instruction_lines = total_lines - comment_lines

        # Count code 7 (PRINT) instructions
        print_count = 0
        for line in catg.end_lines:
            s = line.strip()
            if re.match(r'^7\s*[,\s]', s):
                print_count += 1

        desc = QLabel(
            f"Everything from <code>C END RORB_GE</code> to end of file.<br>"
            f"This includes the routing instruction sequence, sub-area areas, "
            f"DCI/ICI fractions, and trailing data.<br><br>"
            f"<b>Total lines:</b> {total_lines}<br>"
            f"<b>Comment lines (C):</b> {comment_lines}<br>"
            f"<b>Instruction/data lines:</b> {instruction_lines}<br>"
            f"<b>PRINT instructions (code 7):</b> {print_count}<br><br>"
            f"<b>Read-only</b> — this block is preserved verbatim on save.<br>"
            f"To change routing instructions, use RORB GE."
        )
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.RichText)
        desc.setStyleSheet("font-weight: normal; color: #555; font-size: 9pt;")
        lay.addWidget(desc)

        # Show first N lines as preview
        preview_lines = catg.end_lines[:100]
        if len(catg.end_lines) > 100:
            preview_lines.append(
                f"\n... ({len(catg.end_lines) - 100} more lines) ..."
            )

        text_edit = QTextEdit()
        text_edit.setFont(self.MONO)
        text_edit.setReadOnly(True)
        text_edit.setPlainText("\n".join(preview_lines))
        text_edit.setStyleSheet(
            "QTextEdit { background-color: #fafafa; border: 1px solid #ccc; "
            "border-radius: 3px; font-size: 9pt; }"
        )
        lay.addWidget(text_edit, 1)

        self.editor_lay.addWidget(group, 1)

        self.info_label.setText(
            f"<b>Section:</b> Data Block<br>"
            f"<b>Total lines:</b> {total_lines}<br>"
            f"<b>PRINT points:</b> {print_count}<br>"
            f"<b>Status:</b> Read-only (preserved verbatim)"
        )
