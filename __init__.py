# -*- coding: utf-8 -*-
"""
RORB catg/stm Editor - QGIS Plugin

Interactive table-format editor for RORB Catchment (.catg) and
Storm (.stm) text files — view, edit, and create new non-uniform
STM files without breaking file structure.
"""

__author__ = 'Irene Jaya'
__date__ = '2026-02-22'
__copyright__ = '(C) 2026, Irene Jaya'


def classFactory(iface):
    """
    Load the plugin class.

    Args:
        iface: A QGIS interface instance

    Returns:
        RORBFileEditorPlugin instance
    """
    from .plugin import RORBFileEditorPlugin
    return RORBFileEditorPlugin(iface)
