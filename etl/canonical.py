"""Canonical schedule model — the boundary between source and warehouse.

Nothing downstream of this module knows what an XER is. A REST or PMDB
adapter produces the same dataclasses and the loader cannot tell the
difference. That is the whole point of the interface.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, List, Dict


@dataclass
class Project:
    project_id: str
    project_name: str
    currency_code: str = "INR"
    value_basis: str = "currency"
    planned_start: Optional[date] = None
    planned_finish: Optional[date] = None


@dataclass
class SnapshotMeta:
    data_date: date
    snapshot_type: str                      # BASELINE | UPDATE | REBASELINE | WHAT_IF
    source_file: str
    file_sha256: str
    source_system: str = "P6"
    source_format: str = "XER"
    retained_logic: Optional[bool] = None
    critical_path_type: Optional[str] = None
    default_pct_type: Optional[str] = None
    project_finish: Optional[datetime] = None
    must_finish_by: Optional[datetime] = None


@dataclass
class CalendarDay:
    calendar_id: str
    calendar_name: str
    cal_date: date
    working_hours: float
    is_working_day: bool
    is_exception: bool


@dataclass
class WbsNode:
    wbs_id: str
    wbs_code: str
    wbs_name: str
    parent_wbs_id: Optional[str]
    wbs_level: int
    wbs_path: str
    area: Optional[str] = None
    phase: Optional[str] = None


@dataclass
class Resource:
    resource_id: str
    resource_name: str
    resource_type: Optional[str] = None
    unit_of_measure: Optional[str] = None
    price_per_unit: Optional[float] = None
    contractor_name: Optional[str] = None
    trade: Optional[str] = None


@dataclass
class Activity:
    """Descriptive attributes (dimension) plus per-snapshot measures (fact).

    They travel together because the source presents them together; the
    loader splits them into dim_activity (SCD2) and fact_activity_snapshot.
    """
    activity_id: str
    activity_name: str
    activity_type: str
    wbs_id: str
    calendar_id: Optional[str]
    duration_type: Optional[str]
    pct_complete_type: Optional[str]
    is_milestone: bool

    status: str                             # NOT_STARTED | IN_PROGRESS | COMPLETE
    original_duration_d: Optional[float]
    remaining_duration_d: Optional[float]
    actual_duration_d: Optional[float]
    early_start: Optional[datetime]
    early_finish: Optional[datetime]
    late_start: Optional[datetime]
    late_finish: Optional[datetime]
    actual_start: Optional[datetime]
    actual_finish: Optional[datetime]
    planned_start: Optional[datetime]
    planned_finish: Optional[datetime]
    total_float_d: Optional[float]
    free_float_d: Optional[float]
    is_critical: Optional[bool]
    is_driving_path: Optional[bool]
    constraint_type: Optional[str] = None
    constraint_date: Optional[datetime] = None
    pct_complete: Optional[float] = None
    budget_value: float = 0.0
    earned_value: float = 0.0

    @property
    def at_completion_duration_d(self) -> Optional[float]:
        if self.actual_duration_d is None or self.remaining_duration_d is None:
            return None
        return self.actual_duration_d + self.remaining_duration_d


@dataclass
class Relationship:
    pred_activity_id: str
    succ_activity_id: str
    relationship_type: str                  # FS | SS | FF | SF
    lag_d: float = 0.0
    is_driving: Optional[bool] = None


@dataclass
class Assignment:
    activity_id: str
    resource_id: str
    budgeted_units: float = 0.0
    actual_units: float = 0.0
    remaining_units: float = 0.0
    budgeted_cost: float = 0.0
    actual_cost: float = 0.0
    remaining_cost: float = 0.0


@dataclass
class CanonicalSchedule:
    project: Project
    snapshot: SnapshotMeta
    wbs: List[WbsNode] = field(default_factory=list)
    activities: List[Activity] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)
    resources: List[Resource] = field(default_factory=list)
    assignments: List[Assignment] = field(default_factory=list)
    calendar_days: List[CalendarDay] = field(default_factory=list)

    def summary(self) -> Dict[str, int]:
        return {
            "activities": len(self.activities),
            "relationships": len(self.relationships),
            "assignments": len(self.assignments),
            "wbs_nodes": len(self.wbs),
            "resources": len(self.resources),
            "calendar_days": len(self.calendar_days),
        }
