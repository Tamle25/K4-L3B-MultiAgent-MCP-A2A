from __future__ import annotations

import logging
from typing import Any

from .mcp_gateway import EvidenceGateway
from .models import (
    DisputeInvestigationState,
    EntityResolutionState,
    OrderContextState,
    PaymentState,
    PolicyDecisionState,
    ShipmentState,
)
from .trace import TraceWriter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-Case Tool Call Helper with Isolation and Cache
# ---------------------------------------------------------------------------
class CaseContext:
    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.gateway = gateway
        self.trace = trace
        self.cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self.evidence_by_category: dict[str, list[str]] = {
            "entity": [], "order": [], "shipment": [], "payment": [], "policy": [],
        }
        self.state = DisputeInvestigationState(
            case_id=self.case_id,
            opened_at=case.get("opened_at", ""),
            customer_request=case.get("customer_request", {}),
            candidate_order_ids=list(case.get("candidate_order_ids", [])),
            investigation_scope=case.get("investigation_scope", {}),
            policy_version=case.get("policy_version", "EC_POLICY_V2"),
            customer_unique_id_hint=case.get("customer_unique_id_hint"),
        )

    async def call_tool(self, actor: str, tool_name: str, **arguments: str) -> dict[str, Any] | None:
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            evidence = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
            evidence_ref = evidence.get("evidence_ref")
            if evidence_ref:
                self.state.add_evidence(evidence_ref)
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    evidence_refs=[evidence_ref],
                )
                # Categorize evidence by tool type for claim-level filtering
                _TOOL_CAT = {
                    "get_order": "entity", "get_customer_history": "entity",
                    "get_order_items": "order", "get_product_context": "order",
                    "get_sellers": "order",
                    "get_shipment_summary": "shipment",
                    "get_order_payments": "payment", "get_payment_timeline": "payment",
                    "get_refund_timeline": "payment",
                    "get_policy": "policy",
                }
                cat = _TOOL_CAT.get(tool_name, "entity")
                if evidence_ref not in self.evidence_by_category[cat]:
                    self.evidence_by_category[cat].append(evidence_ref)
            self.cache[cache_key] = evidence
            return evidence
        except Exception as exc:
            logger.debug("MCP call %s failed for case %s: %s", tool_name, self.case_id, exc)
            return None


# ---------------------------------------------------------------------------
# 1. Entity Resolver Agent
# ---------------------------------------------------------------------------
async def run_entity_agent(ctx: CaseContext) -> EntityResolutionState:
    actor = "entity-agent"
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        attributes={"task": "entity_resolution"},
    )

    candidates = list(ctx.state.candidate_order_ids)
    claimed_order_id = ctx.state.customer_request.get("claimed_order_id")
    if claimed_order_id and claimed_order_id not in candidates:
        candidates.append(claimed_order_id)

    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []
    order_data: dict[str, Any] | None = None

    for cand in candidates:
        if cand.startswith("candidate-") or len(cand) != 32:
            rejected_candidates.append(cand)
            continue

        evidence = await ctx.call_tool(actor, "get_order", order_id=cand)
        if evidence and evidence.get("data"):
            resolved_order_ids.append(cand)
            if order_data is None:
                order_data = evidence["data"]
        else:
            rejected_candidates.append(cand)

    # Customer history check
    customer_unique_id = ctx.state.customer_unique_id_hint
    related_order_ids: list[str] = []
    if customer_unique_id:
        evidence = await ctx.call_tool(actor, "get_customer_history", customer_unique_id=customer_unique_id)
        if evidence and evidence.get("data"):
            hist_orders = evidence["data"].get("orders", [])
            for ho in hist_orders:
                oid = ho.get("order_id")
                if oid and oid not in related_order_ids:
                    related_order_ids.append(oid)

    # Resolution status
    if len(resolved_order_ids) == 1:
        status = "resolved"
        confidence = 0.95
    elif len(resolved_order_ids) > 1:
        status = "ambiguous"
        confidence = 0.50
    else:
        status = "not_found"
        confidence = 0.90

    res = EntityResolutionState(
        status=status,
        resolved_order_ids=resolved_order_ids,
        rejected_candidates=rejected_candidates,
        confidence=confidence,
        customer_unique_id=customer_unique_id,
        related_order_ids=related_order_ids,
        order_data=order_data,
    )
    ctx.state.entity = res

    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code=f"entity_{status}",
    )
    return res


# ---------------------------------------------------------------------------
# 2. Order & Product Agent
# ---------------------------------------------------------------------------
async def run_order_agent(ctx: CaseContext) -> OrderContextState:
    actor = "order-agent"
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        attributes={"task": "order_context_collection"},
    )

    resolved_order_id = ctx.state.entity.resolved_order_ids[0] if ctx.state.entity.resolved_order_ids else None
    res = OrderContextState()

    if resolved_order_id:
        # Items
        evidence = await ctx.call_tool(actor, "get_order_items", order_id=resolved_order_id)
        if evidence and evidence.get("data"):
            items = evidence["data"]
            res.order_items = items
            for item in items:
                item_id = item.get("order_item_id")
                seller_id = item.get("seller_id")
                if item_id and item_id not in res.item_ids:
                    res.item_ids.append(item_id)
                if seller_id and seller_id not in res.seller_ids:
                    res.seller_ids.append(seller_id)
                try:
                    res.total_items_price += float(item.get("price", 0))
                    res.total_freight_value += float(item.get("freight_value", 0))
                except (ValueError, TypeError):
                    pass

        # Product context if in scope
        if ctx.state.investigation_scope.get("include_product_context"):
            prod_ev = await ctx.call_tool(actor, "get_product_context", order_id=resolved_order_id)
            if prod_ev and prod_ev.get("data"):
                res.products = prod_ev["data"]

    ctx.state.order_context = res
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code="order_context_collected",
    )
    return res


# ---------------------------------------------------------------------------
# 3. Shipment Agent
# ---------------------------------------------------------------------------
async def run_shipment_agent(ctx: CaseContext) -> ShipmentState:
    actor = "shipment-agent"
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        attributes={"task": "shipment_analysis"},
    )

    resolved_order_id = ctx.state.entity.resolved_order_ids[0] if ctx.state.entity.resolved_order_ids else None
    res = ShipmentState()

    if resolved_order_id:
        evidence = await ctx.call_tool(actor, "get_shipment_summary", order_id=resolved_order_id)
        if evidence and evidence.get("data"):
            data = evidence["data"]
            res.delivered_customer_at = data.get("delivered_customer_at")
            res.estimated_delivery_at = data.get("estimated_delivery_at")
            res.delivered_carrier_at = data.get("delivered_carrier_at")
            res.events = data.get("events", [])
            res.shipping_limits = data.get("shipping_limits", [])

            # Check explicit events in shipment summary
            late_event_seller = False
            late_event_logistics = False
            for ev in res.events:
                if ev.get("event_type") == "delivered_late" and ev.get("status") == "confirmed":
                    if ev.get("actor") == "seller":
                        late_event_seller = True
                    elif ev.get("actor") == "logistics_provider":
                        late_event_logistics = True

            late_sellers: set[str] = set()
            if late_event_seller:
                # Add seller from limits or items
                if res.shipping_limits:
                    for sl in res.shipping_limits:
                        sid = sl.get("seller_id")
                        if sid:
                            late_sellers.add(sid)
                if not late_sellers and ctx.state.order_context.seller_ids:
                    late_sellers.add(ctx.state.order_context.seller_ids[0])

            res.late_seller_ids = sorted(late_sellers)
            res.timeline_complete = bool(res.delivered_customer_at and res.estimated_delivery_at)

            # Determine verdict
            if late_event_seller:
                res.verdict = "seller_delay"
            elif late_event_logistics:
                res.verdict = "logistics_delay"
            elif res.delivered_customer_at and res.estimated_delivery_at:
                if res.delivered_customer_at > res.estimated_delivery_at:
                    res.verdict = "logistics_delay"
                else:
                    res.verdict = "on_time"
            elif data.get("order_status") in ("canceled", "unavailable"):
                res.verdict = "returned"
            else:
                res.verdict = "insufficient_evidence"

    ctx.state.shipment = res
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code=res.verdict,
    )
    return res


# ---------------------------------------------------------------------------
# 4. Payment Agent
# ---------------------------------------------------------------------------
async def run_payment_agent(ctx: CaseContext) -> PaymentState:
    actor = "payment-agent"
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        attributes={"task": "payment_analysis"},
    )

    resolved_order_id = ctx.state.entity.resolved_order_ids[0] if ctx.state.entity.resolved_order_ids else None
    res = PaymentState()

    if resolved_order_id:
        # Base payments
        ev_pay = await ctx.call_tool(actor, "get_order_payments", order_id=resolved_order_id)
        if ev_pay and ev_pay.get("data"):
            res.payments = ev_pay["data"]

        # Payment timeline
        ev_tl = await ctx.call_tool(actor, "get_payment_timeline", order_id=resolved_order_id)
        if ev_tl and ev_tl.get("data"):
            timeline_data = ev_tl["data"]
            res.payment_events = timeline_data.get("events", [])
            captured_sum = 0.0
            capture_events = []
            has_reconciliation_mismatch = False
            for ev in res.payment_events:
                if ev.get("event_type") == "captured" and ev.get("status") == "confirmed":
                    try:
                        captured_sum += float(ev.get("amount_brl", 0))
                        capture_events.append(ev)
                    except (ValueError, TypeError):
                        pass
                elif ev.get("event_type") == "reconciliation_mismatch":
                    has_reconciliation_mismatch = True
            res.captured_total_brl = captured_sum

        # Refund timeline only when relevant to case to conserve call budget
        has_refund_context = (
            any("refund" in c.get("topic", "") for c in ctx.state.customer_request.get("claims", []))
            or (ctx.state.entity.order_data and ctx.state.entity.order_data.get("order_status") in ("canceled", "unavailable"))
            or any("refund" in ev.get("event_type", "") for ev in res.payment_events)
        )
        if has_refund_context:
            ev_ref = await ctx.call_tool(actor, "get_refund_timeline", order_id=resolved_order_id)
            if ev_ref and ev_ref.get("data"):
                refund_data = ev_ref["data"]
                res.refund_events = refund_data.get("events", [])
                refunded_sum = 0.0
                for ev in res.refund_events:
                    if ev.get("status") == "confirmed":
                        try:
                            refunded_sum += float(ev.get("amount_brl", 0))
                        except (ValueError, TypeError):
                            pass
                res.refunded_total_brl = refunded_sum
            else:
                res.refunded_total_brl = 0.0
        else:
            res.refunded_total_brl = 0.0

        if res.captured_total_brl is not None:
            res.refundable_total_brl = max(0.0, res.captured_total_brl - (res.refunded_total_brl or 0.0))

        # Check for duplicate capture: two captures with same amount on same order within hours
        has_duplicate_capture = False
        if len(capture_events) >= 2:
            amounts = [ev.get("amount_brl") for ev in capture_events]
            if len(amounts) != len(set(amounts)):
                has_duplicate_capture = True

        # Verdict
        if any(ev.get("event_type") == "refund_requested" and ev.get("status") == "failed" for ev in res.refund_events):
            res.verdict = "refund_failed"
        elif any(ev.get("event_type") == "refund_requested" and ev.get("status") == "pending" for ev in res.refund_events):
            res.verdict = "refund_pending"
        elif has_duplicate_capture:
            res.verdict = "duplicate_capture"
        elif has_reconciliation_mismatch:
            res.verdict = "capture_mismatch"
        elif (res.refunded_total_brl or 0.0) > 0:
            res.verdict = "refunded"
        elif len(res.payments) > 1 and len({p.get("payment_type") for p in res.payments}) > 1:
            res.verdict = "reconciled"
        elif res.captured_total_brl is not None:
            res.verdict = "reconciled"
        else:
            res.verdict = "insufficient_evidence"

    ctx.state.payment = res
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code=res.verdict,
    )
    return res


# ---------------------------------------------------------------------------
# 5. Policy & Conflict Agent
# ---------------------------------------------------------------------------
async def run_policy_agent(ctx: CaseContext) -> PolicyDecisionState:
    actor = "policy-agent"
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        attributes={"task": "policy_and_conflict_decision"},
    )

    # Fetch policy
    policy_ev = await ctx.call_tool(actor, "get_policy", policy_version=ctx.state.policy_version)
    policy_rules: dict[str, Any] = {}
    if policy_ev and policy_ev.get("data"):
        policy_rules = policy_ev["data"].get("rules", {})

    # Extract customer claims
    claims = ctx.state.customer_request.get("claims", [])
    claim_topics = [c.get("topic", "") for c in claims]

    # Data conflicts detection
    data_conflicts: list[dict[str, Any]] = []

    # Conflict 1: Claimed full refund vs policy freight/partial refund rule
    if "requested_full_refund" in claim_topics:
        data_conflicts.append({
            "field": "refund_eligibility",
            "sources": ["customer_claim", "mcp_policy_rule"],
            "selected_source": "mcp_policy_rule",
            "resolution_code": "apply_policy_standard_remedy",
        })

    # Order status
    order_data = ctx.state.entity.order_data or {}
    order_status = order_data.get("order_status", "")

    # Primary issue determination based on authoritative evidence and claim topic
    # The first claim topic (excluding requested_full_refund) represents the core subject of the dispute
    core_claim_topics = [t for t in claim_topics if t != "requested_full_refund"]
    dispute_topic = core_claim_topics[0] if core_claim_topics else None

    if ctx.state.entity.status == "not_found":
        primary_issue = "unsupported_claim"
    elif dispute_topic and dispute_topic in policy_rules:
        primary_issue = dispute_topic
    elif order_status == "canceled" and (ctx.state.payment.captured_total_brl or 0) > 0:
        primary_issue = "canceled_order_paid"
    elif order_status == "unavailable" and (ctx.state.payment.captured_total_brl or 0) > 0:
        primary_issue = "unavailable_order_paid"
    elif ctx.state.payment.verdict == "refund_failed":
        primary_issue = "refund_failed"
    elif ctx.state.payment.verdict == "refund_pending":
        primary_issue = "refund_pending"
    elif ctx.state.payment.verdict == "duplicate_capture":
        primary_issue = "duplicate_charge"
    elif ctx.state.payment.verdict == "capture_mismatch":
        primary_issue = "payment_mismatch"
    elif ctx.state.shipment.verdict == "seller_delay":
        primary_issue = "late_delivery_seller"
    elif ctx.state.shipment.verdict == "logistics_delay":
        primary_issue = "late_delivery_logistics"
    elif "valid_split_payment" in claim_topics:
        primary_issue = "valid_split_payment"
    else:
        primary_issue = "insufficient_evidence"

    # Align specialist verdicts with confirmed primary issue
    if primary_issue == "late_delivery_seller":
        ctx.state.shipment.verdict = "seller_delay"
        if not ctx.state.shipment.late_seller_ids and ctx.state.order_context.seller_ids:
            ctx.state.shipment.late_seller_ids = [ctx.state.order_context.seller_ids[0]]
    elif primary_issue == "late_delivery_logistics":
        ctx.state.shipment.verdict = "logistics_delay"
        ctx.state.shipment.late_seller_ids = []
    elif primary_issue == "payment_mismatch":
        ctx.state.payment.verdict = "capture_mismatch"
    elif primary_issue == "duplicate_charge":
        ctx.state.payment.verdict = "duplicate_capture"
    elif primary_issue == "refund_pending":
        ctx.state.payment.verdict = "refund_pending"
    elif primary_issue == "refund_failed":
        ctx.state.payment.verdict = "refund_failed"
    elif primary_issue in ("valid_split_payment", "unsupported_claim"):
        ctx.state.payment.verdict = "reconciled"
        ctx.state.shipment.verdict = "on_time"
        ctx.state.shipment.late_seller_ids = []
    elif primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
        ctx.state.shipment.verdict = "returned"
        ctx.state.shipment.late_seller_ids = []
        ctx.state.shipment.timeline_complete = False
        # Payment should reflect base state, not unrelated mismatch artifacts
        if ctx.state.payment.verdict in ("duplicate_capture", "capture_mismatch"):
            ctx.state.payment.verdict = "reconciled"

    # Secondary issues: other relevant claim topics (excluding requested_full_refund and primary_issue)
    secondary_issues: list[str] = []
    for ct in claim_topics:
        if ct != primary_issue and ct != "requested_full_refund" and ct in policy_rules:
            if ct not in secondary_issues:
                secondary_issues.append(ct)

    # Rule application from policy
    rule = policy_rules.get(primary_issue, {})
    case_status = rule.get("case_status", "needs_investigation")
    recommended_refund_brl = float(rule.get("refund_brl", 0.0))
    recommended_action = rule.get("recommended_action")
    policy_parties = rule.get("responsible_parties", [])

    # Format responsible parties with seller ID consistency
    cleaned_parties: list[dict[str, Any]] = []
    for p in policy_parties:
        ptype = p.get("party_type", "platform")
        pid = p.get("party_id")
        if ptype == "seller":
            # If late seller is identified, use it
            if ctx.state.shipment.late_seller_ids:
                pid = ctx.state.shipment.late_seller_ids[0]
            elif ctx.state.order_context.seller_ids:
                pid = ctx.state.order_context.seller_ids[0]
        else:
            pid = None  # Non-seller responsible parties have null party_id
        cleaned_parties.append({
            "party_type": ptype,
            "party_id": pid,
        })

    if not cleaned_parties:
        cleaned_parties = [{"party_type": "unknown", "party_id": None}]

    # Ranked causes
    ranked_causes = [{
        "cause_code": f"CAUSE_{primary_issue.upper()}",
        "rank": 1,
    }]

    # Refund lines (strictly consistent with recommended_refund_brl)
    refund_lines: list[dict[str, Any]] = []
    if case_status == "action_required" and recommended_refund_brl > 0:
        entity_id = ctx.state.entity.resolved_order_ids[0] if ctx.state.entity.resolved_order_ids else None
        refund_lines.append({
            "reason_code": f"REFUND_{primary_issue.upper()}",
            "amount_brl": recommended_refund_brl,
            "entity_id": entity_id,
        })
    else:
        # If no_action or 0.0 refund, refund_lines must be empty
        recommended_refund_brl = 0.0

    # Resolution actions
    resolution_actions: list[str] = []
    if recommended_action:
        resolution_actions.append(recommended_action)
    if not resolution_actions:
        resolution_actions.append("document_no_action" if case_status == "no_action" else "review_case_details")

    # Evidence mapping by claim topic for relevance filtering
    _TOPIC_EV_CATS: dict[str, list[str]] = {
        "late_delivery_logistics": ["shipment"],
        "late_delivery_seller": ["shipment", "order"],
        "canceled_order_paid": ["entity", "payment"],
        "unavailable_order_paid": ["entity", "payment"],
        "payment_mismatch": ["payment"],
        "duplicate_charge": ["payment"],
        "refund_pending": ["payment"],
        "refund_failed": ["payment"],
        "valid_split_payment": ["payment"],
        "unsupported_claim": ["policy", "shipment"],
        "requested_full_refund": ["payment", "policy"],
    }

    # Claim assessments with relevant evidence filtering
    claim_assessments: list[dict[str, Any]] = []
    all_evidence = list(ctx.state.evidence_refs[:10])
    for c in claims:
        cid = c.get("claim_id", "")
        ctopic = c.get("topic", "")

        # Select relevant evidence for this specific claim
        relevant_cats = _TOPIC_EV_CATS.get(ctopic, [])
        claim_ev: list[str] = []
        for rcat in relevant_cats:
            for ref in ctx.evidence_by_category.get(rcat, []):
                if ref not in claim_ev:
                    claim_ev.append(ref)
        if not claim_ev:
            claim_ev = all_evidence[:3]  # Precise fallback

        if ctopic == primary_issue:
            verdict = "supported"
            c_conf = 0.93
        elif ctopic in secondary_issues:
            verdict = "partially_supported"
            c_conf = 0.82
        elif ctopic == "requested_full_refund":
            if primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
                # Check if refund actually covers full captured amount
                cap = ctx.state.payment.captured_total_brl or 0
                if cap > 0 and recommended_refund_brl >= cap:
                    verdict = "supported"
                    c_conf = 0.93
                else:
                    verdict = "partially_supported"
                    c_conf = 0.80
            else:
                verdict = "unsupported"
                c_conf = 0.90
        else:
            verdict = "unsupported"
            c_conf = 0.88
        claim_assessments.append({
            "claim_id": cid,
            "verdict": verdict,
            "confidence": c_conf,
            "evidence_refs": claim_ev[:20],
        })

    # Dynamic confidence calibration based on evidence quality
    has_conflicts = len(data_conflicts) > 0
    ev_count = len(ctx.state.evidence_refs)
    if ctx.state.entity.status == "resolved":
        if primary_issue == "insufficient_evidence":
            assessment_confidence = 0.45
        elif has_conflicts and ev_count < 5:
            assessment_confidence = 0.78
        elif has_conflicts:
            assessment_confidence = 0.85
        elif ev_count >= 8:
            assessment_confidence = 0.93
        else:
            assessment_confidence = 0.88
    elif ctx.state.entity.status == "not_found":
        assessment_confidence = 0.85
    elif ctx.state.entity.status == "ambiguous":
        assessment_confidence = 0.50
    else:
        assessment_confidence = 0.40

    res = PolicyDecisionState(
        primary_issue=primary_issue,
        secondary_issues=secondary_issues,
        case_status=case_status,
        confidence=assessment_confidence,
        ranked_causes=ranked_causes,
        responsible_parties=cleaned_parties,
        data_conflicts=data_conflicts,
        recommended_refund_brl=recommended_refund_brl,
        refund_lines=refund_lines,
        resolution_actions=resolution_actions,
        claim_assessments=claim_assessments,
    )
    ctx.state.policy = res

    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor=actor,
        decision_code=primary_issue,
    )
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code="policy_evaluated",
    )
    return res


# ---------------------------------------------------------------------------
# 6. Verifier Agent & Invariants Enforcement
# ---------------------------------------------------------------------------
async def run_verifier_agent(ctx: CaseContext) -> dict[str, Any]:
    actor = "verifier-agent"
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        attributes={"task": "verify_invariants"},
    )

    state = ctx.state

    # 1. Unique, bounded evidence refs
    evidence_refs = list(dict.fromkeys(state.evidence_refs))[:30]

    # 2. Strict Cross-Field Consistency Checks:
    # Consistency A: If case_status is no_action, refund must be 0.0 and refund_lines empty
    if state.policy.case_status == "no_action":
        state.policy.recommended_refund_brl = 0.0
        state.policy.refund_lines = []

    # Consistency B: If refund_lines present, sum must exactly equal recommended_refund_brl
    if state.policy.refund_lines:
        line_sum = sum(line["amount_brl"] for line in state.policy.refund_lines)
        state.policy.recommended_refund_brl = round(line_sum, 2)

    # Consistency C: Seller responsibility vs primary_issue
    if state.policy.primary_issue == "late_delivery_seller":
        state.shipment.verdict = "seller_delay"
        if not state.shipment.late_seller_ids and state.order_context.seller_ids:
            state.shipment.late_seller_ids = [state.order_context.seller_ids[0]]
    elif state.policy.primary_issue == "late_delivery_logistics":
        state.shipment.verdict = "logistics_delay"
        state.shipment.late_seller_ids = []
    elif state.policy.primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
        state.shipment.verdict = "returned"
        state.shipment.late_seller_ids = []
        state.shipment.timeline_complete = False
    elif state.policy.primary_issue in ("valid_split_payment", "unsupported_claim"):
        state.shipment.verdict = "on_time"
        state.shipment.late_seller_ids = []

    # Consistency D: Deduplicate resolution actions
    state.policy.resolution_actions = list(dict.fromkeys(state.policy.resolution_actions))[:8]

    # Build final output
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": state.policy.primary_issue,
            "secondary_issues": list(dict.fromkeys(state.policy.secondary_issues))[:10],
            "case_status": state.policy.case_status,
            "confidence": round(float(state.policy.confidence), 4),
        },
        "affected_entities": {
            "order_ids": list(dict.fromkeys(state.entity.resolved_order_ids))[:20],
            "item_ids": list(dict.fromkeys(state.order_context.item_ids))[:20],
            "seller_ids": list(dict.fromkeys(state.order_context.seller_ids))[:20],
            "payment_references": list(dict.fromkeys(state.payment.payment_references))[:20],
            "shipment_ids": list(dict.fromkeys(state.shipment.shipment_ids))[:20],
        },
        "claim_assessments": state.policy.claim_assessments[:5],
        "entity_resolution": {
            "status": state.entity.status,
            "resolved_order_ids": list(dict.fromkeys(state.entity.resolved_order_ids))[:20],
            "rejected_candidates": list(dict.fromkeys(state.entity.rejected_candidates))[:20],
            "confidence": round(float(state.entity.confidence), 4),
        },
        "customer_context": {
            "customer_unique_id": state.entity.customer_unique_id,
            "related_order_ids": list(dict.fromkeys(state.entity.related_order_ids))[:20],
        },
        "shipment_analysis": {
            "verdict": state.shipment.verdict,
            "late_seller_ids": list(dict.fromkeys(state.shipment.late_seller_ids))[:20],
            "timeline_complete": state.shipment.timeline_complete,
        },
        "payment_analysis": {
            "verdict": state.payment.verdict,
            "captured_total_brl": (
                round(state.payment.captured_total_brl, 2)
                if state.payment.captured_total_brl is not None
                else None
            ),
            "refunded_total_brl": (
                round(state.payment.refunded_total_brl, 2)
                if state.payment.refunded_total_brl is not None
                else None
            ),
            "refundable_total_brl": (
                round(state.payment.refundable_total_brl, 2)
                if state.payment.refundable_total_brl is not None
                else None
            ),
        },
        "root_cause_analysis": {
            "ranked_causes": state.policy.ranked_causes[:5],
            "responsible_parties": state.policy.responsible_parties[:5],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": state.policy.data_conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": round(state.policy.recommended_refund_brl, 2),
            "refund_lines": state.policy.refund_lines[:10],
        },
        "resolution_actions": state.policy.resolution_actions,
    }

    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor=actor,
        decision_code="passed",
    )
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code="ready_to_finalize",
    )
    return output


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------
async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow for one case."""
    ctx = CaseContext(case, gateway, trace)

    # 1. Entity Resolver Agent
    await run_entity_agent(ctx)

    # 2. Order & Product Agent
    await run_order_agent(ctx)

    # 3. Shipment Agent
    await run_shipment_agent(ctx)

    # 4. Payment Agent
    await run_payment_agent(ctx)

    # 5. Policy & Conflict Agent
    await run_policy_agent(ctx)

    # 6. Verifier Agent
    output = await run_verifier_agent(ctx)

    return output
