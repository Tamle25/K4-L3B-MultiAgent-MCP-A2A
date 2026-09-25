from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EntityResolutionState:
    status: str = "not_found"  # resolved, ambiguous, not_found
    resolved_order_ids: list[str] = field(default_factory=list)
    rejected_candidates: list[str] = field(default_factory=list)
    confidence: float = 0.5
    customer_unique_id: str | None = None
    related_order_ids: list[str] = field(default_factory=list)
    order_data: dict[str, Any] | None = None


@dataclass
class OrderContextState:
    order_items: list[dict[str, Any]] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    total_items_price: float = 0.0
    total_freight_value: float = 0.0


@dataclass
class ShipmentState:
    verdict: str = "insufficient_evidence"
    late_seller_ids: list[str] = field(default_factory=list)
    timeline_complete: bool = False
    shipment_ids: list[str] = field(default_factory=list)
    delivered_customer_at: str | None = None
    estimated_delivery_at: str | None = None
    delivered_carrier_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    shipping_limits: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PaymentState:
    verdict: str = "insufficient_evidence"
    captured_total_brl: float | None = None
    refunded_total_brl: float | None = None
    refundable_total_brl: float | None = None
    payment_references: list[str] = field(default_factory=list)
    payments: list[dict[str, Any]] = field(default_factory=list)
    payment_events: list[dict[str, Any]] = field(default_factory=list)
    refund_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PolicyDecisionState:
    primary_issue: str = "insufficient_evidence"
    secondary_issues: list[str] = field(default_factory=list)
    case_status: str = "needs_investigation"
    confidence: float = 0.5
    ranked_causes: list[dict[str, Any]] = field(default_factory=list)
    responsible_parties: list[dict[str, Any]] = field(default_factory=list)
    data_conflicts: list[dict[str, Any]] = field(default_factory=list)
    recommended_refund_brl: float = 0.0
    refund_lines: list[dict[str, Any]] = field(default_factory=list)
    resolution_actions: list[str] = field(default_factory=list)
    claim_assessments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DisputeInvestigationState:
    case_id: str
    opened_at: str
    customer_request: dict[str, Any]
    candidate_order_ids: list[str]
    investigation_scope: dict[str, Any]
    policy_version: str
    customer_unique_id_hint: str | None = None

    # Accumulated evidence refs from MCP calls
    evidence_refs: list[str] = field(default_factory=list)

    # Specialist states
    entity: EntityResolutionState = field(default_factory=EntityResolutionState)
    order_context: OrderContextState = field(default_factory=OrderContextState)
    shipment: ShipmentState = field(default_factory=ShipmentState)
    payment: PaymentState = field(default_factory=PaymentState)
    policy: PolicyDecisionState = field(default_factory=PolicyDecisionState)

    def add_evidence(self, *refs: str) -> None:
        for ref in refs:
            if ref and ref not in self.evidence_refs:
                self.evidence_refs.append(ref)
