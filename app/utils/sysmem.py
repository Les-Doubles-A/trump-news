"""Mémoire que Windows peut encore réserver (« commit ») : RAM + fichier de pagination.

C'est elle qui limite le chargement d'un modèle LM Studio, même sur la carte graphique : sans fichier de pagination,
un modèle 8b (~6 Go) ne se charge plus dès que d'autres programmes (WSL, Docker, navigateur) occupent la mémoire.
"""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass

GB = 1024 ** 3


@dataclass
class MemoryStatus:
    total_ram_gb: float
    free_ram_gb: float
    commit_limit_gb: float      # RAM + fichier de pagination
    commit_free_gb: float       # ce qui peut encore être réservé

    @property
    def has_pagefile(self) -> bool:
        return self.commit_limit_gb > self.total_ram_gb + 0.5


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory_status() -> MemoryStatus | None:
    """None hors Windows ou si l'appel système échoue."""
    if sys.platform != "win32":
        return None
    st = _MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return MemoryStatus(round(st.ullTotalPhys / GB, 1), round(st.ullAvailPhys / GB, 1),
                        round(st.ullTotalPageFile / GB, 1), round(st.ullAvailPageFile / GB, 1))
