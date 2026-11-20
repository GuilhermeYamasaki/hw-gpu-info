"""Detecção automática de jogos da Steam e gravação de histórico de sessões."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gpu_sensors import GpuSnapshot, MetricStats

# AppIds de ferramentas/runtimes da Steam que não são jogos
IGNORED_APP_IDS = {
    "0",
    "228980",   # Steamworks Common Redistributables
    "1070560",  # Steam Linux Runtime 1.0 (scout)
    "1391110",  # Steam Linux Runtime 2.0 (soldier)
    "1628350",  # Steam Linux Runtime 3.0 (sniper)
    "1493710",  # Proton Experimental
    "2805730",  # Proton 9.0
    "2348590",  # Proton 8.0
    "1887720",  # Proton 7.0
}

STEAM_ROOT_CANDIDATES = (
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
    Path.home() / ".steam/debian-installation",
    Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
)

RE_APPID = re.compile(r"AppId=(\d+)")
RE_VDF_PATH = re.compile(r'"path"\s+"([^"]+)"')
RE_ACF_NAME = re.compile(r'"name"\s+"([^"]+)"')
RE_COMMON_DIR = re.compile(r"steamapps/common/([^/]+)")


@dataclass
class DetectedGame:
    app_id: str
    name: str


class SteamGameDetector:
    """Detecta jogos da Steam rodando via /proc e resolve o nome pelo appmanifest."""

    def __init__(self) -> None:
        self._name_cache: Dict[str, str] = {}

    def _steam_library_dirs(self) -> List[Path]:
        libs: List[Path] = []
        seen: set[str] = set()

        def _add(p: Path) -> None:
            try:
                resolved = str(p.resolve())
            except OSError:
                resolved = str(p)
            if resolved not in seen and p.exists():
                seen.add(resolved)
                libs.append(p)

        for root in STEAM_ROOT_CANDIDATES:
            steamapps = root / "steamapps"
            if steamapps.is_dir():
                _add(steamapps)
                vdf = steamapps / "libraryfolders.vdf"
                if vdf.is_file():
                    try:
                        text = vdf.read_text(encoding="utf-8", errors="ignore")
                        for match in RE_VDF_PATH.finditer(text):
                            lib_path = Path(match.group(1).replace("\\\\", "/")) / "steamapps"
                            _add(lib_path)
                    except OSError:
                        pass
        return libs

    def resolve_game_name(self, app_id: str, fallback_cmdline: str = "") -> str:
        if app_id in self._name_cache:
            return self._name_cache[app_id]

        for lib in self._steam_library_dirs():
            manifest = lib / f"appmanifest_{app_id}.acf"
            if manifest.is_file():
                try:
                    text = manifest.read_text(encoding="utf-8", errors="ignore")
                    match = RE_ACF_NAME.search(text)
                    if match:
                        name = match.group(1).strip()
                        self._name_cache[app_id] = name
                        return name
                except OSError:
                    pass

        common_match = RE_COMMON_DIR.search(fallback_cmdline)
        if common_match:
            name = common_match.group(1).strip()
            self._name_cache[app_id] = name
            return name

        return f"Steam App {app_id}"

    def detect(self) -> Optional[DetectedGame]:
        """Retorna o jogo da Steam ativo no momento, ou None."""
        proc_dir = Path("/proc")
        try:
            entries = os.listdir(proc_dir)
        except OSError:
            return None

        for pid in entries:
            if not pid.isdigit():
                continue
            cmdline_path = proc_dir / pid / "cmdline"
            try:
                raw = cmdline_path.read_bytes()
            except OSError:
                continue
            if not raw:
                continue

            if b"SteamLaunch" in raw and b"AppId=" in raw:
                text = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
                match = RE_APPID.search(text)
                if match:
                    app_id = match.group(1)
                    if app_id not in IGNORED_APP_IDS:
                        return DetectedGame(
                            app_id=app_id,
                            name=self.resolve_game_name(app_id, text),
                        )
        return None


@dataclass
class ActiveSession:
    app_id: str
    game_name: str
    started_at: datetime
    started_mono: float
    stats: Dict[str, MetricStats] = field(
        default_factory=lambda: {
            "temp_edge": MetricStats(),
            "temp_junction": MetricStats(),
            "temp_mem": MetricStats(),
            "clock_sclk_mhz": MetricStats(),
            "fan_rpm": MetricStats(),
            "power_watts": MetricStats(),
            "gpu_busy_percent": MetricStats(),
        }
    )

    def duration_seconds(self) -> int:
        return max(0, int(time.monotonic() - self.started_mono))

    def record_snapshot(self, snap: GpuSnapshot) -> None:
        for key, stat in self.stats.items():
            stat.update(getattr(snap, key))

    def to_record(self) -> Dict[str, Any]:
        def _r(val: Optional[float], digits: int = 1) -> Optional[float]:
            return round(val, digits) if val is not None else None

        ended_at = datetime.now().astimezone()
        return {
            "app_id": self.app_id,
            "game": self.game_name,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": ended_at.isoformat(timespec="seconds"),
            "duration_sec": self.duration_seconds(),
            "samples": self.stats["temp_edge"]._count,
            "temp_edge_c": {
                "min": _r(self.stats["temp_edge"].min_val, 1),
                "avg": _r(self.stats["temp_edge"].avg_val, 1),
                "p95": _r(self.stats["temp_edge"].p95_val, 1),
                "std_dev": _r(self.stats["temp_edge"].std_dev, 1),
                "max": _r(self.stats["temp_edge"].max_val, 1),
            },
            "temp_junction_c": {
                "min": _r(self.stats["temp_junction"].min_val, 1),
                "avg": _r(self.stats["temp_junction"].avg_val, 1),
                "p95": _r(self.stats["temp_junction"].p95_val, 1),
                "std_dev": _r(self.stats["temp_junction"].std_dev, 1),
                "max": _r(self.stats["temp_junction"].max_val, 1),
            },
            "temp_mem_c": {
                "avg": _r(self.stats["temp_mem"].avg_val, 1),
                "p95": _r(self.stats["temp_mem"].p95_val, 1),
                "std_dev": _r(self.stats["temp_mem"].std_dev, 1),
                "max": _r(self.stats["temp_mem"].max_val, 1),
            },
            "clock_sclk_mhz": {
                "avg": _r(self.stats["clock_sclk_mhz"].avg_val, 0),
                "p95": _r(self.stats["clock_sclk_mhz"].p95_val, 0),
                "std_dev": _r(self.stats["clock_sclk_mhz"].std_dev, 1),
                "max": _r(self.stats["clock_sclk_mhz"].max_val, 0),
            },
            "fan_rpm": {
                "avg": _r(self.stats["fan_rpm"].avg_val, 0),
                "p95": _r(self.stats["fan_rpm"].p95_val, 0),
                "std_dev": _r(self.stats["fan_rpm"].std_dev, 1),
                "max": _r(self.stats["fan_rpm"].max_val, 0),
            },
            "power_watts": {
                "avg": _r(self.stats["power_watts"].avg_val, 1),
                "p95": _r(self.stats["power_watts"].p95_val, 1),
                "std_dev": _r(self.stats["power_watts"].std_dev, 1),
                "max": _r(self.stats["power_watts"].max_val, 1),
            },
            "gpu_busy_percent": {
                "avg": _r(self.stats["gpu_busy_percent"].avg_val, 1),
            },
        }


class GameSessionTracker:
    """Gerencia o ciclo de vida das sessões de jogos da Steam e salva histórico."""

    def __init__(
        self,
        history_path: Optional[Path] = None,
        min_duration_sec: int = 30,
        scan_interval_sec: float = 2.0,
    ) -> None:
        self.history_path = history_path or (Path(__file__).resolve().parent / "historico_jogos.json")
        self.min_duration_sec = min_duration_sec
        self.scan_interval_sec = scan_interval_sec
        self.detector = SteamGameDetector()
        self.active: Optional[ActiveSession] = None
        self._last_scan_mono: float = 0.0

    def update(self, snap: GpuSnapshot) -> Optional[Tuple[str, Any]]:
        """Atualiza detecção e grava amostras. Retorna evento ('started'|'saved', payload) se houver."""
        now = time.monotonic()
        event: Optional[Tuple[str, Any]] = None

        if now - self._last_scan_mono >= self.scan_interval_sec:
            self._last_scan_mono = now
            detected = self.detector.detect()
            event = self._handle_detection(detected)

        if self.active is not None:
            self.active.record_snapshot(snap)

        return event

    def _handle_detection(self, detected: Optional[DetectedGame]) -> Optional[Tuple[str, Any]]:
        if detected is not None:
            if self.active is None:
                self.active = ActiveSession(
                    app_id=detected.app_id,
                    game_name=detected.name,
                    started_at=datetime.now().astimezone(),
                    started_mono=time.monotonic(),
                )
                return ("started", detected.name)

            if self.active.app_id != detected.app_id:
                saved = self._finish_active()
                self.active = ActiveSession(
                    app_id=detected.app_id,
                    game_name=detected.name,
                    started_at=datetime.now().astimezone(),
                    started_mono=time.monotonic(),
                )
                return ("saved", saved) if saved else ("started", detected.name)
            return None

        if self.active is not None:
            saved = self._finish_active()
            if saved is not None:
                return ("saved", saved)
        return None

    def finalize_if_active(self) -> Optional[Dict[str, Any]]:
        """Finaliza e salva a sessão ativa ao fechar o aplicativo."""
        return self._finish_active()

    def _finish_active(self) -> Optional[Dict[str, Any]]:
        if self.active is None:
            return None
        session = self.active
        self.active = None
        if session.duration_seconds() < self.min_duration_sec or session.stats["temp_edge"]._count == 0:
            return None
        record = session.to_record()
        self._append_record(record)
        return record

    def _append_record(self, record: Dict[str, Any]) -> None:
        history = self.load_history()
        history.insert(0, record)
        tmp = self.history_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.history_path)

    def load_history(self) -> List[Dict[str, Any]]:
        if not self.history_path.is_file():
            return []
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def clear_history(self) -> None:
        if self.history_path.exists():
            self.history_path.write_text("[]\n", encoding="utf-8")
