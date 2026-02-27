# -*- coding: utf-8 -*-
"""
RORB catg/stm Editor - Main Plugin Class

Registers two toolbar actions:
  1. CATG Editor — open/edit RORB Catchment (.catg) files in table format
  2. STM Editor  — open/edit/create RORB Storm (.stm) files in table format
"""

import os
from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon


class RORBFileEditorPlugin:
    """Main plugin class for RORB File Editor."""

    def __init__(self, iface):
        """
        Initialize the plugin.

        Args:
            iface: QGIS interface instance
        """
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu_name = "RORB catg/stm Editor"
        self.toolbar = None
        self.dialogs = []  # Track open dialogs for cleanup

        # Initialize locale (with safe fallback)
        locale_setting = QSettings().value('locale/userLocale')
        locale = locale_setting[0:2] if locale_setting else 'en'
        locale_path = os.path.join(
            self.plugin_dir, 'i18n',
            'RORBFileEditor_{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

    def tr(self, message):
        """Translate string."""
        return QCoreApplication.translate('RORBFileEditorPlugin', message)

    def initGui(self):
        """Initialize the GUI - called when plugin is loaded."""
        # Create toolbar
        self.toolbar = self.iface.addToolBar(self.menu_name)
        self.toolbar.setObjectName("RORBCatgStmEditorToolbar")

        # ---- CATG Editor action ----
        icon_catg_path = os.path.join(self.plugin_dir, 'icon_catg.svg')
        icon_catg = QIcon(icon_catg_path)

        self.action_catg = QAction(
            icon_catg,
            self.tr("RORB CATG Editor"),
            self.iface.mainWindow()
        )
        self.action_catg.setToolTip(
            "Open RORB Catchment (.catg) File Editor\n"
            "View/edit nodes, reaches, storages,\n"
            "print flags, and routing data"
        )
        self.action_catg.triggered.connect(self.run_catg)
        self.toolbar.addAction(self.action_catg)
        self.iface.addPluginToMenu(self.menu_name, self.action_catg)
        self.actions.append(self.action_catg)

        # ---- STM Editor action ----
        icon_stm_path = os.path.join(self.plugin_dir, 'icon_stm.svg')
        icon_stm = QIcon(icon_stm_path)

        self.action_stm = QAction(
            icon_stm,
            self.tr("RORB STM Editor"),
            self.iface.mainWindow()
        )
        self.action_stm.setToolTip(
            "Open RORB Storm (.stm) File Editor\n"
            "View/edit storm parameters, bursts,\n"
            "pluviographs, and hydrograph data"
        )
        self.action_stm.triggered.connect(self.run_stm)
        self.toolbar.addAction(self.action_stm)
        self.iface.addPluginToMenu(self.menu_name, self.action_stm)
        self.actions.append(self.action_stm)

    def run_catg(self):
        """Launch a new CATG Editor dialog window."""
        from .editors.rorb_catg_editor import CATGEditorDialog
        
        # Create a fresh, independent dialog instance each time
        dlg = CATGEditorDialog(parent=None)  # No parent = independent window
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)  # Auto-cleanup when closed
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        
        # Track the dialog for any cleanup if needed
        self.dialogs.append(dlg)
        # Remove from list when closed
        dlg.finished.connect(lambda: self.dialogs.remove(dlg) if dlg in self.dialogs else None)

    def run_stm(self):
        """Launch a new STM Editor dialog window."""
        from .editors.rorb_stm_editor import STMEditorDialog
        
        # Create a fresh, independent dialog instance each time
        dlg = STMEditorDialog(parent=None)  # No parent = independent window
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)  # Auto-cleanup when closed
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        
        # Track the dialog for any cleanup if needed
        self.dialogs.append(dlg)
        # Remove from list when closed
        dlg.finished.connect(lambda: self.dialogs.remove(dlg) if dlg in self.dialogs else None)

    def unload(self):
        """Unload the plugin - called when plugin is unloaded."""
        # Remove menu items
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)

        # Remove toolbar
        if self.toolbar:
            del self.toolbar

        # Close all open dialogs
        for dlg in self.dialogs[:]:  # Copy list to avoid modification during iteration
            dlg.close()
        self.dialogs = []
