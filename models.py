from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Union

ENCODING_TYPES = [
    "SGTIN-96",
    "SGTIN-198",
    "SSCC-96",
    "SGLN-96",
    "GRAI-96",
    "GIAI-96",
    "CPI-96",
    "GDTI-96",
    "Custom EPC / Hex",
    "Other / Customer Specific",
]


@dataclass
class ProofJob:
    customer: str = ""
    preparing_company: str = ""
    job_number: str = ""
    part_number: str = ""
    quantity: str = ""
    revision: str = "A"
    proof_revision: str = "1"
    gtin: str = ""
    encoding_type: str = "SGTIN-96"
    barcode_type_1: str = "UPC-A"
    barcode_data_1: str = ""
    barcode_type_2: str = ""
    barcode_data_2: str = ""
    font_size_callout: str = ""
    label_width: float = 4.0
    label_height: float = 2.0
    inlay: str = ""
    chip: str = ""
    inlay_width_mm: float = 0.0
    inlay_height_mm: float = 0.0
    inlay_image_path: str = ""
    inlay_preview_opacity: int = 45
    inlay_x_mm: float = 0.0
    inlay_y_mm: float = 0.0
    inlay_rotation: int = 0
    filter_value: int = 3
    company_prefix_digits: int = 7
    company_prefix: str = ""
    item_reference: str = ""
    serial: Union[int, str] = 1
    epc_hex: str = ""
    template_path: str = ""
    preview_path: str = ""
    # Beta 14 preview integrity metadata. Existing Beta 13 jobs remain compatible.
    preview_generated_at: str = ""
    preview_template_path: str = ""
    preview_source: str = ""
    preview_fingerprint: str = ""
    notes: str = ""
    operator_initials: str = ""
    internal_comments: str = ""

    # V 1.0.0 Beta traceability/lifecycle fields.
    proof_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_generated_at: str = ""
    lifecycle_status: str = "draft"
    source_proof_id: str = ""
    internal_comment_log: List[Dict[str, Any]] = field(default_factory=list)

    # Beta 9 final-review / revision-guard baseline.
    last_generated_proof_revision: str = ""
    last_generated_master_company: str = ""
    last_generated_snapshot: Dict[str, Any] = field(default_factory=dict)

    # Beta 10 internal operations / release-control fields.
    priority: str = "Normal"
    due_date: str = ""
    release_checklist: Dict[str, bool] = field(default_factory=dict)
    internal_tags: List[str] = field(default_factory=list)

    # Beta 11 approval-control / team-workflow fields.
    approval_status: str = "Draft"
    approval_updated_at: str = ""
    approval_by: str = ""
    approval_note: str = ""
    approval_history: List[Dict[str, Any]] = field(default_factory=list)
    customer_response_due: str = ""
    pinned: bool = False
    batch_source: str = ""

    # Beta 14 operator handoff / queue ownership. Internal only.
    assigned_operator: str = ""
    handoff_note: str = ""

    # Beta 15 output-integrity / production-release control. Internal only.
    last_generated_fingerprint: str = ""
    last_generated_output_path: str = ""
    last_generated_output_kind: str = ""
    production_released_at: str = ""
    production_released_by: str = ""
    production_release_note: str = ""
    production_release_fingerprint: str = ""
    activity_log: List[Dict[str, Any]] = field(default_factory=list)

    # Beta 16 production orchestration / exception control. Internal only.
    production_hold: bool = False
    hold_reason: str = ""
    hold_by: str = ""
    hold_at: str = ""
    release_workcenter: str = ""
    production_release_workcenter: str = ""
    escalation_level: str = "None"
    escalation_note: str = ""
    escalation_at: str = ""
    escalation_by: str = ""
    handoff_packet_path: str = ""
    handoff_packet_generated_at: str = ""

    # Beta 17 production-flow intelligence. Internal only.
    run_sheet_path: str = ""
    run_sheet_generated_at: str = ""

    # Beta 18 controlled rework + internal production bundle. Internal only.
    rework_active: bool = False
    rework_reason: str = ""
    rework_requested_at: str = ""
    rework_requested_by: str = ""
    rework_count: int = 0
    rework_history: List[Dict[str, Any]] = field(default_factory=list)
    production_bundle_path: str = ""
    production_bundle_generated_at: str = ""
    production_bundle_fingerprint: str = ""

    # Job Manager link. Internal only (stripped from customer packages). The job_proofs
    # table in the Job Manager database is the index; these fields let it be rebuilt.
    jm_job_id: int = 0
    jm_job_number: str = ""

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ProofJob":
        known = cls.__dataclass_fields__.keys()
        clean = {key: value for key, value in data.items() if key in known}
        if not isinstance(clean.get("internal_comment_log", []), list):
            clean["internal_comment_log"] = []
        if not isinstance(clean.get("last_generated_snapshot", {}), dict):
            clean["last_generated_snapshot"] = {}
        if not isinstance(clean.get("release_checklist", {}), dict):
            clean["release_checklist"] = {}
        if not isinstance(clean.get("internal_tags", []), list):
            clean["internal_tags"] = []
        if not isinstance(clean.get("approval_history", []), list):
            clean["approval_history"] = []
        if not isinstance(clean.get("activity_log", []), list):
            clean["activity_log"] = []
        if not isinstance(clean.get("rework_history", []), list):
            clean["rework_history"] = []
        try:
            clean["jm_job_id"] = int(clean.get("jm_job_id", 0) or 0)
        except (TypeError, ValueError):
            clean["jm_job_id"] = 0
        return cls(**clean)
