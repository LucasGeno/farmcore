"""A synthetic ISO 11783-10 `TASKDATA.XML` for demo step 3 (the sprayer's file).

Synthetic demo file, not a real machine record: every value below is a constant chosen for the
demo (10 Apr 2024 copper + sulphur pass on P5 Les Granges and P6 Peyrot, a little under the 2 kg/ha
the farmer says in the voice note). The file is generated on demand and never committed.

Shape (ISO 11783-10 v4): one customer `CTR`, one farm `FRM`, one sprayer `DVC`, two partfields
`PFD` (`C` designator, `D` area in m²), two products `PDT`, and one task `TSK` per partfield
(the standard links a task to one partfield, `E`), status `G="4"` (completed), each with a `TIM`
(`A` start, `B` stop, `D="4"` effective, UTC with `Z`) and one treatment zone `TZN` holding two
`PDV` setpoints, DDI `0006` (mass per area, mg/m²; 194 mg/m² = 1.94 kg/ha).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# --- synthetic demo file, not a real machine record ---
CUSTOMER = "Château demo"
FARM = "Château demo"
DEVICE = "Sprayer (synthetic demo)"
DEVICE_CLIENT_NAME = "A00084000DE0B0C4"  # 64-bit ISO NAME, hex; invented
SOFTWARE = "farmcore isoxml_demo"
DDI_MASS_PER_AREA = "0006"  # setpoint mass per area application rate, mg/m²
TIM_EFFECTIVE = "4"
TASK_COMPLETED = "4"


@dataclass(frozen=True)
class DemoProduct:
    ref: str
    name: str  # the name the machine terminal reports; matched to seed.SHORTLIST by substance word


@dataclass(frozen=True)
class DemoTask:
    ref: str
    partfield_ref: str
    partfield: str  # parcel name (fields seed, IGN demo parcels)
    area_m2: int
    start_local: datetime
    stop_local: datetime
    doses_mg_m2: tuple[tuple[str, int], ...]  # (product ref, mg/m²)


COPPER = DemoProduct("PDT1", "Bouillie bordelaise 20 WG")
SULPHUR = DemoProduct("PDT2", "Soufre mouillable 80 WG")
PRODUCTS = (COPPER, SULPHUR)
COPPER_MG_M2 = 194  # 1.94 kg/ha
SULPHUR_MG_M2 = 206  # 2.06 kg/ha
TASKS = (
    DemoTask(
        "TSK1",
        "PFD1",
        "Les Granges",  # P5, 4.07 ha
        40_700,
        datetime(2024, 4, 10, 7, 12, 5),
        datetime(2024, 4, 10, 7, 58, 40),
        ((COPPER.ref, COPPER_MG_M2), (SULPHUR.ref, SULPHUR_MG_M2)),
    ),
    DemoTask(
        "TSK2",
        "PFD2",
        "Peyrot",  # P6, 2.21 ha
        22_100,
        datetime(2024, 4, 10, 8, 3, 10),
        datetime(2024, 4, 10, 8, 31, 55),
        ((COPPER.ref, COPPER_MG_M2), (SULPHUR.ref, SULPHUR_MG_M2)),
    ),
)


def _utc(local: datetime, tz: str) -> str:
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def demo_taskdata(*, tz: str = "Europe/Paris") -> bytes:
    """The synthetic `TASKDATA.XML` as UTF-8 bytes; task times are local to `tz`, written in UTC."""
    root = ET.Element(
        "ISO11783_TaskData",
        {
            "VersionMajor": "4",
            "VersionMinor": "3",
            "ManagementSoftwareManufacturer": SOFTWARE,
            "ManagementSoftwareVersion": "1",
            "DataTransferOrigin": "1",
        },
    )
    ET.SubElement(root, "CTR", {"A": "CTR1", "B": CUSTOMER})
    ET.SubElement(root, "FRM", {"A": "FRM1", "B": FARM, "I": "CTR1"})
    ET.SubElement(root, "DVC", {"A": "DVC1", "B": DEVICE, "D": DEVICE_CLIENT_NAME})
    for t in TASKS:
        ET.SubElement(
            root,
            "PFD",
            {"A": t.partfield_ref, "C": t.partfield, "D": str(t.area_m2), "E": "CTR1", "F": "FRM1"},
        )
    for p in PRODUCTS:
        ET.SubElement(root, "PDT", {"A": p.ref, "B": p.name})
    for t in TASKS:
        task = ET.SubElement(
            root,
            "TSK",
            {
                "A": t.ref,
                "B": f"Spray {t.partfield}",
                "C": "CTR1",
                "D": "FRM1",
                "E": t.partfield_ref,
                "G": TASK_COMPLETED,
            },
        )
        zone = ET.SubElement(task, "TZN", {"A": "1", "B": "whole partfield"})
        for product_ref, mg_m2 in t.doses_mg_m2:
            ET.SubElement(zone, "PDV", {"A": DDI_MASS_PER_AREA, "B": str(mg_m2), "C": product_ref})
        ET.SubElement(
            task,
            "TIM",
            {"A": _utc(t.start_local, tz), "B": _utc(t.stop_local, tz), "D": TIM_EFFECTIVE},
        )
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
