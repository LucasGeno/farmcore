"""FarmCore oracle generator for plasmopy (Agroscope's VitiMeteo Plasmopara model).

FarmCore's own script (Apache-2.0). It runs the AGPL plasmopy program, installed only inside
the oracle container at /opt/plasmopy, as a separate process through its own entry point
(`src/main.py`, Hydra), and turns its infection table into a JSON fixture. It imports nothing
from plasmopy.

Usage (inside the container): python3 plasmopy/generate.py <season csv> <output dir>
"""

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from omegaconf import OmegaConf

PLASMOPY = Path("/opt/plasmopy")
STEP_MIN = 10  # plasmopy's default measurement interval; each input hour becomes 6 rows

# Upstream defaults stay as shipped, except what a run without a site, secrets or spore trap
# needs. The site is a nominal one for sunrise/sunset (sporulation darkness), not a real farm.
OVERRIDES = {
    "input_data.spore_counts": None,
    "spore_driven_model.enabled": False,
    "site.latitude": 46.0,
    "site.longitude": 7.0,
    "site.elevation": 400,
    "site.timezone": "UTC",
    # Upstream presets a 2025 maturation date for its own example; compute it from the inputs.
    "oospore_maturation.date": None,
}


def hydra_value(value):
    return "null" if value is None else str(value)


def write_meteo(season: Path, meteo: Path) -> None:
    """Plasmopy's input layout: `;`-separated, local time `%d.%m.%Y %H:%M`, 10-minute rows."""
    with season.open(encoding="utf-8") as f, meteo.open("w", encoding="utf-8") as out:
        out.write("datetime;temperature;humidity;rainfall_mm_h;leaf_wetness_min\n")
        for row in csv.DictReader(f):
            hour = datetime.strptime(row["time_utc"], "%Y-%m-%dT%H:%M:%SZ")
            wet_fraction = int(row["leaf_wet_min"]) / 60
            for k in range(60 // STEP_MIN):
                t = hour.replace(minute=k * STEP_MIN).strftime("%d.%m.%Y %H:%M")
                wet = round(wet_fraction * STEP_MIN)
                out.write(f"{t};{row['temp_c']};{row['rh_pct']};{row['rain_mm']};{wet}\n")


def main(season: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        meteo = work / "meteo.csv"
        write_meteo(season, meteo)
        overrides = {
            "input_data.meteo": str(meteo),
            "output.directory": str(work / "out"),
            "output.run_name": "farmcore",
            **OVERRIDES,
        }
        args = [f"{k}={hydra_value(v)}" for k, v in overrides.items()]
        env = {**os.environ, "MPLCONFIGDIR": str(work / "mpl"), "MPLBACKEND": "Agg"}
        # plasmopy writes logs and scratch files into its own tree: run a private copy.
        program = shutil.copytree(PLASMOPY, work / "plasmopy")
        subprocess.run([sys.executable, "src/main.py", *args], cwd=program, env=env, check=True)
        table = next((work / "out").rglob("*.infection_datetimes.csv"))
        with table.open(encoding="utf-8") as f:
            infections = [dict(r) for r in csv.DictReader(f)]

    config = OmegaConf.load(PLASMOPY / "config" / "main.yaml")
    for key, value in OVERRIDES.items():
        OmegaConf.update(config, key, value)
    model = {
        k: v
        for k, v in OmegaConf.to_container(config).items()
        if k not in {"hydra", "input_data", "output", "risk_heatmap"}
    }

    pyproject = (PLASMOPY / "pyproject.toml").read_text(encoding="utf-8")
    version = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in pyproject.splitlines()
        if line.startswith("version")
    )
    fixture = {
        "format": "farmcore-oracle-fixture/1",
        "generator": {
            "name": "plasmopy",
            "version": f"{version} (commit {os.environ['PLASMOPY_COMMIT']})",
            "licence": "AGPL-3.0 (LICENSE in the upstream repository)",
            "source": "https://github.com/agroscope-ch/plasmopy",
            "source_sha256": os.environ["PLASMOPY_SHA256"],
            "authors": "Livio Ruzzante; Agroscope",
            "runtime": f"Python {sys.version.split()[0]}",
            "run": "out of process, in the tools/oracles container (python3 src/main.py)",
        },
        "method_reference": (
            "Agroscope VitiMeteo Plasmopara model (plasmopy README). Not Rossi et al. 2008: "
            "use for event-timing cross-checks only."
        ),
        "call": "plasmopy src/main.py (Hydra) with the overrides in parameters.overrides",
        "parameters": {
            "overrides": OVERRIDES,
            "model": model,
            "input_step_minutes": STEP_MIN,
        },
        "inputs": {
            "file": f"inputs/{season.name}",
            "sha256": hashlib.sha256(season.read_bytes()).hexdigest(),
            "units": {"temp_c": "degC", "rh_pct": "%", "rain_mm": "mm/h", "leaf_wet_min": "min"},
            "transform": (
                "each hour repeated as six 10-minute rows; rain kept as mm/h (plasmopy reads "
                "intensity); leaf wetness scaled to minutes per 10 minutes; times in UTC"
            ),
        },
        "outputs": {
            "description": "plasmopy's <run>.infection_datetimes.csv, one object per row",
            "infections": infections,
        },
    }
    path = out_dir / "season-2025.json"
    path.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print("wrote", path, len(infections), "rows")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
