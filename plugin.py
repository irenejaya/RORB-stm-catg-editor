# -*- coding: utf-8 -*-
"""
RORB catg/stm Editor - Main Plugin Class

Registers two toolbar actions:
  1. CATG Editor — open/edit RORB Catchment (.catg) files in table format
  2. STM Editor  — open/edit/create RORB Storm (.stm) files in table format
"""

import os
from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication
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
        self.dlg_catg = None
        self.dlg_stm = None

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
        """Launch the CATG Editor dialog."""
        if self.dlg_catg is None:
            from .editors.rorb_catg_editor import CATGEditorDialog
            self.dlg_catg = CATGEditorDialog(
                parent=self.iface.mainWindow()
            )

        self.dlg_catg.show()
        self.dlg_catg.raise_()
        self.dlg_catg.activateWindow()

    def run_stm(self):
        """Launch the STM Editor dialog."""
        if self.dlg_stm is None:
            from .editors.rorb_stm_editor import STMEditorDialog
            self.dlg_stm = STMEditorDialog(
                parent=self.iface.mainWindow()
            )

        self.dlg_stm.show()
        self.dlg_stm.raise_()
        self.dlg_stm.activateWindow()

    def unload(self):
        """Unload the plugin - called when plugin is unloaded."""
        # Remove menu items
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)

        # Remove toolbar
        if self.toolbar:
            del self.toolbar

        # Close dialogs
        if self.dlg_catg:
            self.dlg_catg.close()
            self.dlg_catg = None
        if self.dlg_stm:
            self.dlg_stm.close()
            self.dlg_stm = None
