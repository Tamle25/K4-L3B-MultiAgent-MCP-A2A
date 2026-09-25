# L3B Architecture Record

Tài liệu thiết kế kiến trúc hệ thống Multi-Agent A2A điều tra khiếu nại thương mại điện tử (K4 L3B).

## 1. System overview

Luồng điều tra khép kín từ input đến output tuân thủ nghiêm ngặt protocol Agent-to-Agent (A2A) và audit log của MCP Evidence Gateway:

```text
[Input Case]
     │
     ▼
[Coordinator Agent] (case_received)
     │
     ├─► [Entity Resolver Agent] ──(MCP: get_order, get_customer_history)──► [Resolved Order & Customer]
     │         │
     │         ▼ (handoff)
     ├─► [Order / Product Agent] ──(MCP: get_order_items, get_sellers, get_product_context)──► [Order Context]
     │         │
     │         ▼ (handoff)
     ├─► [Shipment Agent] ───────(MCP: get_shipment_summary)───────────────► [Shipment Analysis]
     │         │
     │         ▼ (handoff)
     ├─► [Payment Agent] ────────(MCP: get_order_payments, timelines)──────► [Payment & Refund Analysis]
     │         │
     │         ▼ (handoff)
     ├─► [Policy & Conflict Agent] ─(MCP: get_policy)──────────────────────► [Conflict & Policy Decision]
     │         │
     │         ▼ (handoff)
     └─► [Verifier Agent] ───────(Invariants & Schema Checks)─────────────► (verification_completed)
               │
               ▼ (case_finalized)
         [Validated L3B Output V2]
```

Mọi tương tác công cụ đều sinh `evidence_ref` duy nhất và được ghi nhận qua sự kiện trace `tool_result_consumed`.

---

## 2. Agent ownership & Least Privilege Matrix

Hệ thống áp dụng nguyên tắc đặc quyền tối thiểu (Least Privilege). Mỗi agent chỉ được cấp quyền gọi đúng các MCP tools cần thiết cho phạm vi trách nhiệm:

| Actor | Input | Trách nhiệm chính | Tool Permissions | Output / Handoff Target |
| :--- | :--- | :--- | :--- | :--- |
| **coordinator** | Case JSON thô | Tiếp nhận case, điều phối task, nhận handoff, quản lý session cache, phát finalize | *Không gọi trực tiếp MCP* | Phân bổ task tới specialist agents |
| **entity-agent** | `candidate_order_ids`, `customer_unique_id_hint`, `claimed_order_id` | Phân giải thực thể đơn hàng, xác định rejected candidates, truy vết customer history | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context` ➔ coordinator |
| **order-agent** | `resolved_order_id`, `investigation_scope` | Trích xuất items, sellers, giá, phí freight và bối cảnh ngành hàng sản phẩm | `get_order_items`, `get_sellers`, `get_product_context` | `affected_entities` (items, sellers), order items context ➔ coordinator |
| **shipment-agent** | `resolved_order_id`, order status/dates | Phân tích hạn chót giao hàng (`shipping_limits`), phát hiện trễ hạn seller vs logistics, xác định shipment verdict | `get_shipment_summary` | `shipment_analysis` (verdict, `late_seller_ids`, timeline) ➔ coordinator |
| **payment-agent** | `resolved_order_id`, order total | Đối soát thanh toán, capture events, đối chiếu lịch sử hoàn tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis` (verdict, captured, refunded, refundable) ➔ coordinator |
| **policy-agent** | `policy_version`, claims, kết quả specialist, customer history | Đối soát xung đột dữ liệu đa nguồn, map rule nghiệp vụ từ policy, tính toán refund và action | `get_policy` | `primary_issue`, `root_cause_analysis`, `financial_resolution`, `data_conflicts`, `resolution_actions` ➔ coordinator |
| **verifier-agent** | Toàn bộ dự thảo output L3B | Thẩm định tính nhất quán đa trường (invariants), kiểm tra JSON Schema, hiệu chuẩn confidence | *Không gọi MCP* (chỉ audit nội bộ) | `verification_completed` ➔ coordinator (Final Output) |

---

## 3. Entity resolution và A2A protocol

### Quy trình phân giải thực thể (Entity Resolution Protocol)
1. **Candidate Verification**: Duyệt danh sách `candidate_order_ids` (và `claimed_order_id`). Với mỗi candidate, gọi `get_order` để xác thực tồn tại trên hệ thống.
   - Nếu MCP trả về order hợp lệ: Candidate được đưa vào `resolved_order_ids`.
   - Nếu MCP trả về lỗi/not found: Đưa candidate vào `rejected_candidates`.
2. **Ambiguity & Customer History Check**:
   - Nếu có nhiều hơn 1 candidate tồn tại hoặc có `customer_unique_id_hint`, gọi `get_customer_history` để đối chiếu `order_id` với lịch sử mua sắm của khách hàng.
   - Nếu xác định chính xác 1 đơn hàng khớp: `status = "resolved"`, `confidence = 0.95`.
   - Nếu không candidate nào hợp lệ: `status = "not_found"`, `confidence = 0.90`.
   - Nếu có nhiều đơn hàng tranh chấp không thể phân định: `status = "ambiguous"`, `confidence = 0.50`.
3. **A2A Message Envelope**:
   Mọi giao tiếp giữa các tác tử sử dụng envelope chuẩn hóa mang `case_id`, `actor`, `target`, `timestamp`, `payload` và danh sách `evidence_refs`. Luồng điều phối đi qua Coordinator tuần tự, loại bỏ hoàn toàn khả năng xảy ra chu trình lặp vô hạn (cycle-free directed acyclic graph).

---

## 4. Evidence và conflict lifecycle

### Vòng đời quản lý Evidence
1. **Tiếp nhận & Validate**: Mọi kết quả trả về từ `EvidenceGateway.call()` đều được kiểm chứng với schema `mcp-evidence-response-v1.schema.json`.
2. **Không tái sử dụng chéo (Isolation)**: Mỗi run của từng `case_id` duy trì một evidence registry độc lập. Nghiêm cấm dùng lại `evidence_ref` giữa các case khác nhau.
3. **Trace Emission**: Ngay khi một specialist agent tiêu thụ dữ liệu từ MCP tool, một event `tool_result_consumed` được phát ra ngay lập tức với `evidence_refs` tương ứng.
4. **Data Conflict Resolution**:
   - Khi phát hiện sai lệch giữa dữ liệu khách hàng khai báo và hồ sơ MCP (ví dụ: ngày giao hàng thực tế vs ngày ước tính, số tiền khiếu nại vs số tiền captured), `policy-agent` ghi nhận vào mảng `data_conflicts`.
   - Cấu trúc: `field`, `sources` (ví dụ: `["customer_claim", "mcp_shipment_summary"]`), `selected_source` (ví dụ: `"mcp_shipment_summary"` theo thứ tự ưu tiên bằng chứng hệ thống), và `resolution_code` (ví dụ: `"prefer_authoritative_carrier_event"`).

---

## 5. Failure and efficiency policy

### Bảng ngân sách thử lại & Chiến lược dự phòng (Retry & Fallback Budget)
| Tình huống lỗi | Retry Budget | Chiến lược xử lý / Fallback | Trace Event / Code |
| :--- | :---: | :--- | :--- |
| **MCP Timeout / HTTP 5xx** | Tối đa 2 lần (backoff 1s) | Trả về `insufficient_evidence` cho specialist tương ứng | `handoff(decision_code="mcp_timeout_fallback")` |
| **Entity not found / Ambiguous** | 0 retry | Ghi nhận `rejected_candidates`, set status `not_found` / `ambiguous` | `handoff(decision_code="entity_unresolved")` |
| **Source Conflict** | 0 retry | Áp dụng ma trận ưu tiên nguồn (MCP > Customer claim) | `policy_decided(decision_code="conflict_resolved")` |
| **Tool không có dữ liệu (vd: refund_timeline rỗng)** | 0 retry | Coi như chưa có refund phát sinh (`refunded_total_brl = 0.0`) | `tool_result_consumed` / fallback graceful |

### Tối ưu hóa hiệu năng gọi tool (Efficiency Strategy)
- **Per-Case Cache**: Lưu trữ kết quả gọi tool trong phạm vi case, không gọi 2 lần cho cùng một tool và tham số.
- **Conditional Querying**: Chỉ gọi `get_sellers` và `get_product_context` khi `investigation_scope` yêu cầu hoặc đơn hàng hợp lệ; chỉ gọi `get_customer_history` khi có hint hoặc cần disambiguate.

---

## 6. Verification invariants (10 Bất biến cốt lõi)

Trước khi phát hành output cuối cùng, `verifier-agent` bắt buộc thẩm định 10 điều kiện bất biến:
1. **Schema Compliance**: Dữ liệu khớp 100% với `l3b-output-v2.schema.json`.
2. **Case ID Matching**: `case_id` trong output khớp chính xác với input.
3. **Entity Scope**: `resolved_order_ids` phải là tập con của `candidate_order_ids` và bằng chứng MCP.
4. **Evidence Ownership**: Mọi `evidence_ref` trong `evidence_refs` phải thực sự được trả về từ MCP calls của chính case này.
5. **Claim Linkage**: Mọi claim trong input đều được đánh giá trong `claim_assessments` kèm `evidence_refs` chứng minh.
6. **Financial Consistency**: `recommended_refund_brl` bằng đúng tổng `amount_brl` của các dòng trong `refund_lines`.
7. **Action Consistency**: Nếu `case_status == "action_required"`, danh sách `resolution_actions` và `recommended_refund_brl` không được để trống / bằng 0 vô lý. Nếu `no_action`, `recommended_refund_brl == 0.0`.
8. **Responsibility Consistency**: Bên chịu trách nhiệm (`responsible_parties`) trong `root_cause_analysis` phải nhất quán với `primary_issue` (ví dụ: `late_delivery_seller` ➔ `seller`, `late_delivery_logistics` ➔ `logistics_provider`).
9. **Shipment & Payment Integrity**: Verdict của shipment và payment phải phù hợp với dữ liệu số liệu (ví dụ: `late_seller_ids` chỉ chứa seller trễ hạn thực tế).
10. **Confidence Calibration**: Điểm tin cậy nằm trong khoảng `[0.0, 1.0]`, được hiệu chuẩn tỉ lệ thuận với mức độ đầy đủ của bằng chứng và tính rõ ràng của timeline.

---

## 7. Reproducibility (Khả năng tái lập)

- **Môi trường thực thi**: Python >= 3.11.9, hệ điều hành Windows / Linux.
- **Dependencies Pinning**: Xem chi tiết tại [pyproject.toml](file:///d:/LabVin_Day9/K4-L3B-MultiAgent-MCP-A2A/pyproject.toml) (`httpx2>=2,<3`, `jsonschema[format]>=4.25,<5`, `mcp>=2,<3`, `python-dotenv>=1.1,<2`).
- **Deterministic Resolution**: Logic phân giải thực thể, trích xuất sự kiện timeline và tính toán tài chính hoàn toàn có tính tất định (deterministic), không phụ thuộc vào random seed hay độ trôi nhiệt độ (temperature drift) của mô hình ngoài khi suy luận logic quy tắc.
- **Lệnh thực thi & Kiểm chứng**:
  ```bash
  day09 mcp-tools
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```

