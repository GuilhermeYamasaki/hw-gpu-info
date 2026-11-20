"""Leitor nativo de sensores da GPU AMD via sysfs/hwmon no Linux."""

from __future__ import annotations

import bisect
import glob
import math
import os
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional


MAX_PERCENTILE_SAMPLES = 7200  # ~2h a 1 leitura/s


@dataclass
class MetricStats:
    """Armazena valor atual, mínimo, médio, P95, desvio padrão (±σ) e máximo."""

    current: Optional[float] = None
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    _mean: float = 0.0
    _m2: float = 0.0
    _count: int = 0
    _window: Deque[float] = field(default_factory=lambda: deque(maxlen=MAX_PERCENTILE_SAMPLES))
    _sorted: List[float] = field(default_factory=list)

    @property
    def avg_val(self) -> Optional[float]:
        if self._count == 0:
            return None
        return self._mean

    @property
    def std_dev(self) -> Optional[float]:
        if self._count == 0:
            return None
        return math.sqrt(max(0.0, self._m2 / self._count))

    @property
    def p95_val(self) -> Optional[float]:
        n = len(self._sorted)
        if n == 0:
            return None
        idx = min(n - 1, max(0, math.ceil(0.95 * n) - 1))
        return self._sorted[idx]

    def update(self, value: Optional[float]) -> None:
        self.current = value
        if value is None:
            return

        # Algoritmo de Welford para média e variância estáveis em O(1)
        self._count += 1
        delta = value - self._mean
        self._mean += delta / self._count
        delta2 = value - self._mean
        self._m2 += delta * delta2

        if self.min_val is None or value < self.min_val:
            self.min_val = value
        if self.max_val is None or value > self.max_val:
            self.max_val = value

        # Janela ordenada para P95 em O(log N)
        if len(self._window) == self._window.maxlen:
            oldest = self._window[0]
            pos = bisect.bisect_left(self._sorted, oldest)
            if pos < len(self._sorted) and self._sorted[pos] == oldest:
                del self._sorted[pos]
        self._window.append(value)
        bisect.insort(self._sorted, value)

    def reset(self) -> None:
        self.min_val = self.current
        self.max_val = self.current
        self._window.clear()
        self._sorted.clear()
        if self.current is not None:
            self._mean = self.current
            self._m2 = 0.0
            self._count = 1
            self._window.append(self.current)
            self._sorted.append(self.current)
        else:
            self._mean = 0.0
            self._m2 = 0.0
            self._count = 0


@dataclass
class FanStatus:
    """Estado legível da ventoinha da GPU."""

    state: str  # "ok", "zero_rpm", "alert", "unknown"
    label: str
    detail: str


@dataclass
class GpuSnapshot:
    """Leitura instantânea dos sensores da GPU."""

    temp_edge: Optional[float] = None
    temp_junction: Optional[float] = None
    temp_mem: Optional[float] = None
    clock_sclk_mhz: Optional[float] = None
    clock_mclk_mhz: Optional[float] = None
    fan_rpm: Optional[float] = None
    fan_max_rpm: Optional[float] = None
    fan_pwm_percent: Optional[float] = None
    power_watts: Optional[float] = None
    gpu_busy_percent: Optional[float] = None
    vram_used_mb: Optional[float] = None
    vram_total_mb: Optional[float] = None


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_int(path: Path) -> Optional[int]:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _detect_gpu_name(device_dir: Optional[Path]) -> str:
    """Tenta obter o nome amigável da GPU via lspci."""
    pci_slot = device_dir.resolve().name if device_dir is not None else None
    try:
        cmd = ["lspci", "-s", pci_slot] if pci_slot else ["lspci"]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if any(k in line.lower() for k in ("vga", "3d", "display")):
                # Formato típico: "0b:00.0 VGA compatible controller: Advanced Micro Devices..."
                parts = line.split(":", 2)
                if len(parts) == 3:
                    name = parts[2].strip()
                    name = name.replace("Advanced Micro Devices, Inc. [AMD/ATI] ", "AMD ")
                    return name
    except Exception:
        pass
    return "AMD Radeon GPU"


class AmdGpuMonitor:
    """Descobre automaticamente a GPU AMD e monitora seus sensores."""

    METRIC_KEYS = (
        "temp_edge",
        "temp_junction",
        "temp_mem",
        "clock_sclk_mhz",
        "clock_mclk_mhz",
        "fan_rpm",
        "fan_pwm_percent",
        "power_watts",
        "gpu_busy_percent",
        "vram_used_mb",
    )

    def __init__(self) -> None:
        self.hwmon_dir: Optional[Path] = None
        self.device_dir: Optional[Path] = None
        self.gpu_name: str = "GPU não detectada"
        self.vram_total_mb: Optional[float] = None
        self.fan_max_rpm: Optional[float] = None
        self.stats: Dict[str, MetricStats] = {k: MetricStats() for k in self.METRIC_KEYS}
        self._temp_map: Dict[str, Path] = {}
        self._freq_map: Dict[str, Path] = {}

        self._discover()

    def _discover(self) -> None:
        # 1. Procura em /sys/class/drm/card*/device/hwmon/hwmon*
        candidates = sorted(glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*"))
        for candidate in candidates:
            hwmon = Path(candidate)
            if _read_text(hwmon / "name") == "amdgpu":
                self.hwmon_dir = hwmon
                self.device_dir = hwmon.parent.parent
                break

        # 2. Fallback em /sys/class/hwmon/hwmon*
        if self.hwmon_dir is None:
            for candidate in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
                hwmon = Path(candidate)
                if _read_text(hwmon / "name") == "amdgpu":
                    self.hwmon_dir = hwmon
                    dev = hwmon / "device"
                    self.device_dir = dev if dev.exists() else None
                    break

        if self.hwmon_dir is None:
            return

        self.gpu_name = _detect_gpu_name(self.device_dir)
        self._map_labeled_sensors()

        fan_max = _read_int(self.hwmon_dir / "fan1_max")
        if fan_max is not None:
            self.fan_max_rpm = float(fan_max)

        if self.device_dir is not None:
            vram_total = _read_int(self.device_dir / "mem_info_vram_total")
            if vram_total is not None:
                self.vram_total_mb = vram_total / (1024 * 1024)

    def _map_labeled_sensors(self) -> None:
        assert self.hwmon_dir is not None
        for idx in range(1, 8):
            label = _read_text(self.hwmon_dir / f"temp{idx}_label")
            input_path = self.hwmon_dir / f"temp{idx}_input"
            if label and input_path.exists():
                self._temp_map[label.lower()] = input_path

        for idx in range(1, 8):
            label = _read_text(self.hwmon_dir / f"freq{idx}_label")
            input_path = self.hwmon_dir / f"freq{idx}_input"
            if label and input_path.exists():
                self._freq_map[label.lower()] = input_path

    @property
    def is_available(self) -> bool:
        return self.hwmon_dir is not None

    def _read_temp(self, label: str, fallback_file: str) -> Optional[float]:
        assert self.hwmon_dir is not None
        path = self._temp_map.get(label, self.hwmon_dir / fallback_file)
        raw = _read_int(path)
        return raw / 1000.0 if raw is not None else None

    def _read_freq_mhz(self, label: str, fallback_file: str) -> Optional[float]:
        assert self.hwmon_dir is not None
        path = self._freq_map.get(label, self.hwmon_dir / fallback_file)
        raw = _read_int(path)
        return raw / 1_000_000.0 if raw is not None else None

    def poll(self) -> GpuSnapshot:
        """Lê todos os sensores e atualiza estatísticas de atual/mín/máx."""
        snap = GpuSnapshot(vram_total_mb=self.vram_total_mb, fan_max_rpm=self.fan_max_rpm)
        if self.hwmon_dir is None:
            return snap

        snap.temp_edge = self._read_temp("edge", "temp1_input")
        snap.temp_junction = self._read_temp("junction", "temp2_input")
        snap.temp_mem = self._read_temp("mem", "temp3_input")

        snap.clock_sclk_mhz = self._read_freq_mhz("sclk", "freq1_input")
        snap.clock_mclk_mhz = self._read_freq_mhz("mclk", "freq2_input")

        fan_raw = _read_int(self.hwmon_dir / "fan1_input")
        if fan_raw is not None:
            snap.fan_rpm = float(fan_raw)

        pwm = _read_int(self.hwmon_dir / "pwm1")
        pwm_max = _read_int(self.hwmon_dir / "pwm1_max") or 255
        if pwm is not None and pwm_max > 0:
            snap.fan_pwm_percent = (pwm / pwm_max) * 100.0

        power_raw = _read_int(self.hwmon_dir / "power1_average")
        if power_raw is None:
            power_raw = _read_int(self.hwmon_dir / "power1_input")
        if power_raw is not None:
            snap.power_watts = power_raw / 1_000_000.0

        if self.device_dir is not None:
            busy = _read_int(self.device_dir / "gpu_busy_percent")
            if busy is not None:
                snap.gpu_busy_percent = float(busy)
            vram_used = _read_int(self.device_dir / "mem_info_vram_used")
            if vram_used is not None:
                snap.vram_used_mb = vram_used / (1024 * 1024)

        for key in self.METRIC_KEYS:
            self.stats[key].update(getattr(snap, key))

        return snap

    def evaluate_fan_status(self, snap: GpuSnapshot) -> FanStatus:
        """Avalia se a ventoinha está funcionando normalmente."""
        if snap.fan_rpm is None:
            return FanStatus(
                state="unknown",
                label="FAN INDISPONÍVEL",
                detail="Sensor fan1_input não encontrado",
            )

        rpm = int(round(snap.fan_rpm))
        pwm_txt = f" ({snap.fan_pwm_percent:.0f}%)" if snap.fan_pwm_percent is not None else ""

        if rpm > 0:
            return FanStatus(
                state="ok",
                label="FAN OK · GIRANDO",
                detail=f"{rpm} RPM{pwm_txt}",
            )

        edge = snap.temp_edge or 0.0
        junction = snap.temp_junction or 0.0
        if edge >= 60.0 or junction >= 65.0:
            return FanStatus(
                state="alert",
                label="ALERTA · FAN PARADA!",
                detail=f"0 RPM com GPU a {edge:.0f} °C",
            )

        return FanStatus(
            state="zero_rpm",
            label="ZERO RPM · MODO SILENCIOSO",
            detail=f"0 RPM · GPU fria ({edge:.0f} °C)",
        )

    def reset_min_max(self) -> None:
        for stat in self.stats.values():
            stat.reset()

    def to_dict(self, snap: Optional[GpuSnapshot] = None) -> Dict[str, Any]:
        """Exporta o estado atual e estatísticas (atual/mín/méd/máx) em dict JSON."""
        if snap is None:
            snap = self.poll()
        fan_status = self.evaluate_fan_status(snap)

        def _r(val: Optional[float], digits: int = 1) -> Optional[float]:
            return round(val, digits) if val is not None else None

        metric_specs = (
            ("temp_edge", "temp_edge_c", 1),
            ("temp_junction", "temp_junction_c", 1),
            ("temp_mem", "temp_mem_c", 1),
            ("clock_sclk_mhz", "clock_sclk_mhz", 0),
            ("clock_mclk_mhz", "clock_mclk_mhz", 0),
            ("fan_rpm", "fan_rpm", 0),
            ("fan_pwm_percent", "fan_pwm_percent", 1),
            ("gpu_busy_percent", "gpu_busy_percent", 1),
            ("vram_used_mb", "vram_used_mb", 1),
            ("power_watts", "power_watts", 1),
        )

        metrics: Dict[str, Any] = {}
        for src_key, out_key, digits in metric_specs:
            st = self.stats[src_key]
            metrics[out_key] = {
                "current": _r(st.current, digits),
                "min": _r(st.min_val, digits),
                "avg": _r(st.avg_val, digits),
                "p95": _r(st.p95_val, digits),
                "std_dev": _r(st.std_dev, 1 if digits == 0 else digits),
                "max": _r(st.max_val, digits),
                "samples": st._count,
            }

        return {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "gpu": {
                "name": self.gpu_name,
                "driver": "amdgpu",
                "hwmon": str(self.hwmon_dir) if self.hwmon_dir else None,
                "vram_total_mb": _r(self.vram_total_mb, 1),
                "fan_max_rpm": _r(self.fan_max_rpm, 0),
            },
            "fan_status": {
                "state": fan_status.state,
                "label": fan_status.label,
                "detail": fan_status.detail,
            },
            "metrics": metrics,
        }
