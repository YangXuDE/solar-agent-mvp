# Solar O&M Agent — Hackathon Plan (Parts 1–12)

## Part 1. Final Recommendation

- **Primary track**: C. O&M Copilot with analytics + visualization + decision support.
- **Supporting capability**: Ticket / error-code correlation (linking error codes to production impact).
- **Rationale**: Pure anomaly detection (track A) tends to get bogged down in model tuning and is hard to explain; pure error-code mapping (track B) looks too much like traditional BI. The Copilot (track C) best demonstrates Agent value. Combined with error-code correlation, it creates a compelling evidence chain: data anomaly → matching error code → linked ticket → calculated loss → actionable advice. This end-to-end logic can be fully implemented within 6 hours using DuckDB (data processing) + simple rules (loss calculation) + LLM (summary and recommendations) — immediately legible to any judge.

---

## Part 2. MVP Definition

**User story**: As an O&M manager, I open the system in the morning and the Agent tells me that Inverter-A lost 500 kWh yesterday (worth $xxx), caused by Error Code 123 (over-temperature), linked to Ticket #456. It recommends dispatching someone to clean the heat sink immediately and shows me the power-curve comparison chart from the incident window.

**Must-have**
- DuckDB data cleaning and wide-table generation.
- Time-window-based production-loss calculation (Peer Comparison or Expected vs Actual).
- Correlation of error codes with production loss.
- Single-page Streamlit UI: event selector, charts, Agent conclusion panel.
- Core Agent explanation and recommendation generation.

**Nice-to-have**
- Natural-language chat input.
- Multi-site / multi-inverter switching.
- Risk-ranked dashboard.

**Skip**
- Complex ML-based anomaly detection.
- Complex RAG architecture or vector databases.
- Intricate LangGraph orchestration.
- Real-time data stream processing.

---

## Part 3. 6-Hour Execution Plan

| Hour | Goal | Tasks | Deliverable | Risk & Fallback |
|------|------|-------|-------------|-----------------|
| **1** | Get the data pipeline running | Define schema; write SQL to clean raw CSVs; generate `event_impact_wide` wide table | `data_pipeline.py` + DuckDB file | Field mismatch → use mock data first |
| **2** | Implement core analytics | Write Python functions; DuckDB queries for impact; match error codes | `analytics_engine.py` | Loss logic too complex → use `(avg_power − actual_power) × time` approximation |
| **3** | Wrap the Agent + API | Write prompt; feed metrics and error code to LLM; parse structured output | `agent_core.py` | LLM slow or malformed response → use fixed template strings; LLM only for the final recommendation line |
| **4** | Build the Streamlit frontend | `app.py`: event selector, Plotly power chart, Agent result panel | `app.py` | UI debugging overruns → hard-code a few canonical cases for display |
| **5** | Integration test + find real demo cases | Run the full pipeline on real data; lock query parameters | Confirmed demo case IDs | No good cases found → fabricate a short anomaly segment for demonstration |
| **6** | Demo prep + bug fixes | Record a backup video; write demo script; clean up dead code | Demo script + screen recording | App fails on stage → play the recording |

---

## Part 4. Data Contract

**Minimum table list**
1. `telemetry_minute` — minute-level inverter telemetry
2. `error_events` — inverter error records
3. `service_tickets` — O&M work orders

**Key fields**

| Table | Fields |
|-------|--------|
| `telemetry_minute` | `timestamp`, `inverter_id`, `active_power_kw`, `daily_yield_kwh` |
| `error_events` | `start_time`, `end_time`, `inverter_id`, `error_code`, `description` |
| `service_tickets` | `ticket_id`, `create_time`, `close_time`, `inverter_id`, `issue_category` |

**Join logic**: join on `inverter_id` and time window (`telemetry_minute.timestamp BETWEEN error_events.start_time AND error_events.end_time`).

**Degradation strategy**
- Missing irradiance → skip theoretical-yield calculation; use the average power of other healthy inverters at the same site as the baseline.
- Missing `end_time` → assume the error persists for 1 hour or until the next error code appears.

---

## Part 5. System Architecture

```
Data layer:   CSV / Parquet  →  DuckDB (in-memory or local file)

Logic layer (Python):
  ├── Data Pipeline   — SQL extraction → wide table
  ├── Analytics Engine — rule-based (Impact = Baseline − Actual)
  └── Agent Core      — assemble prompt → call LLM API → parse JSON

Presentation layer:   Streamlit Web App
  ├── Left sidebar:  Inverter + time-range selector / preset cases
  └── Right main:    Plotly chart (top) + Agent analysis report (bottom)
```

**Required modules**: DuckDB queries, rule-based calculation, Streamlit UI, fixed-prompt LLM call.

**Optional modules**: chat input (RAG or dynamic SQL).

---

## Part 6. DuckDB Plan

**Directory layout**: `/data` for raw files; `/db` for the `.duckdb` file.

**Wide table design (`event_impact_wide`)**

| Column | Description |
|--------|-------------|
| `event_id` | Unique event identifier |
| `inverter_id` | Inverter identifier |
| `start_time` | Error window start |
| `end_time` | Error window end |
| `error_code` | Error code |
| `ticket_id` | Linked service ticket |
| `duration_mins` | Event duration in minutes |
| `avg_power_during_event` | Actual average power during the event (kW) |
| `baseline_power` | Expected power from peer average (kW) |
| `estimated_loss_kwh` | Estimated production loss (kWh) |

**Core SQL (MVP)**

```sql
-- Calculate production loss during an event
-- (using site-peer average as baseline)
WITH peer_avg AS (
    SELECT timestamp, AVG(active_power_kw) AS baseline_power
    FROM telemetry_minute
    WHERE inverter_id != 'TARGET_INV'
    GROUP BY timestamp
),
event_telemetry AS (
    SELECT t.timestamp, t.active_power_kw, p.baseline_power
    FROM telemetry_minute t
    JOIN peer_avg p ON t.timestamp = p.timestamp
    WHERE t.inverter_id = 'TARGET_INV'
      AND t.timestamp BETWEEN '2023-01-01 10:00:00' AND '2023-01-01 14:00:00'
)
SELECT
    SUM(baseline_power - active_power_kw) / 60.0 AS estimated_loss_kwh
FROM event_telemetry
WHERE baseline_power > active_power_kw;
```

---

## Part 7. App Plan

**Framework**: Streamlit — fastest to build, great built-in charting.

**Single-page layout**

```
Header:  ☀️ Solar O&M Copilot

Sidebar:
  ├── "Select Demo Case"  (pre-loaded canonical events — safest for live demo)
  └── (optional) "Custom Query"  (date + device picker)

Main area — top:    Incident Summary card
                    (loss kWh, error code, linked ticket)

Main area — middle: Visual Evidence
                    Plotly line chart — Actual Power vs Baseline Power,
                    error window highlighted in red

Main area — bottom: Agent Insights & Actionable Advice
                    LLM-generated structured text:
                    Cause Analysis · Suggested Actions
```

---

## Part 8. Agent Workflow

**Primary Agent: O&M Analyst**

- **Trigger**: User selects an event or inverter in the UI.
- **Inputs**:
  1. Aggregated stats for the selected time window (loss kWh, duration).
  2. Relevant error-code description.
  3. Linked ticket history.
- **Tools**: calls `analytics_engine` only to retrieve a data dict — the LLM does not write SQL directly.
- **Reasoning steps**:
  1. Receive structured JSON data.
  2. Combine with error-code knowledge to explain why this power drop occurred.
  3. Produce next-step O&M recommendations (e.g., check fuses, restart device).

**Output schema (JSON)**

```json
{
  "incident_summary": "Inverter 1A lost 50 kWh due to Over-Temperature.",
  "likely_cause": "Cooling fan failure indicated by Error 404.",
  "suggested_action": "Dispatch technician to inspect cooling fans.",
  "confidence": "High"
}
```

**What each layer handles**

| Layer | Responsibility |
|-------|----------------|
| DuckDB | Data filtering, aggregation, joins |
| Python rules | Baseline calculation, loss integration |
| LLM | Translate error codes; synthesise data into a human-readable report and recommendations |

---

## Part 9. File-by-File Repo Blueprint

```
solar-agent-mvp/
├── data/                    mock or real CSVs
├── src/
│   ├── data_pipeline.py     DuckDB initialisation + CSV import
│   ├── analytics_engine.py  SQL execution, loss calculation, chart data extraction
│   └── agent_core.py        Prompt assembly, LLM API call (DeepSeek / OpenAI-compatible)
├── app.py                   Streamlit entry point
├── requirements.txt         Python dependencies
└── README.md                Project overview and quick-start guide
```

**Development order**: `data_pipeline.py` → `analytics_engine.py` → `agent_core.py` → `app.py`

---

## Part 11. Demo and Judging Pack

### 3-Minute Demo Script

| Time | Content |
|------|---------|
| **0:00–0:30** | **Pain point & value prop** — "O&M teams face hundreds of error codes every day and struggle to know which ones are costing real money. Our Solar O&M Copilot automatically correlates telemetry, error events, and work orders — telling you not just what broke, but how much it cost and exactly what to do next." |
| **0:30–1:30** | **Core demo** — "In this demo interface, we select an Error-404 event from today's fault list on the left. The chart in the middle clearly shows that when the error occurred (red zone), actual power (blue line) immediately dropped away from the peer-based baseline (grey dashed line). The system automatically calculated a loss of 50 kWh for that window." |
| **1:30–2:30** | **Agent value** — "The most important part is the Agent Insights panel at the bottom. It doesn't just read the data — it cross-references the ticket system and tells you this is a cooling-fan failure, and directly recommends dispatching a technician. This is not a static dashboard; it's an intelligent assistant that gives you actionable advice." |
| **2:30–3:00** | **Wrap-up** — "We used DuckDB for ultra-fast local data processing combined with an LLM for logical reasoning. This system can be deployed on a single machine and running at your plant tomorrow. Thank you!" |

### One-Line Value Proposition
> **Turn raw telemetry and cryptic error codes into instant financial impact and actionable O&M advice.**

### Five Core KPIs on the Home Screen

| # | KPI | Purpose |
|---|-----|---------|
| 1 | Inverter ID | Which device has the issue |
| 2 | Error Code | The specific fault code |
| 3 | Est. Energy Loss (kWh) | Quantified production loss |
| 4 | Linked Ticket | Work-order status |
| 5 | Agent Confidence | Reliability of the recommendation |

### Anticipated Judge Questions

**Q: How do you calculate the loss? What if no peer-inverter data is available?**
> In the MVP we use the average output of other healthy inverters at the same site as the baseline (Peer Comparison). If peer data is unavailable, our fallback is the same inverter's historical average for the equivalent time window on prior days, or integrating a PVsyst theoretical model.

**Q: How do you prevent LLM hallucinations?**
> We never let the LLM write SQL or retrieve data directly. Python + DuckDB compute the exact loss figures and event facts; we hand only the structured, verified facts to the LLM for "translation and summarisation." This keeps the LLM in a narrow, well-bounded role where hallucination risk is minimal.

### Scope-Cut Plan (if the final hour runs short)

| Cut | Replacement |
|-----|-------------|
| Real data ingestion | Use `generate_mock_data` output for the demo |
| Complex LLM prompt | If the API is unreachable, fall back to Python `if/else` rules that emit fixed recommendation strings |
| Natural-language chat box | Keep only the click-to-select main flow |

---

## Part 12. Next Actions

The three files to generate first (validation methods below):

### 1. `src/data_pipeline.py`
**Validation**: run `python src/data_pipeline.py`. The console should print `"Database initialized successfully."`, the `solar_om.duckdb` file should appear in the project root, and three mock CSV files should appear under `data/`.

### 2. `src/analytics_engine.py`
**Validation**: in a Python session or a quick `test.py`, run:
```python
from src.analytics_engine import AnalyticsEngine
e = AnalyticsEngine()
print(e.get_events())
```
Confirm that a DataFrame with an `event_id` column is printed without errors.

### 3. `app.py`
**Validation**: run `streamlit run app.py`, open a browser, and confirm:
- The page renders correctly.
- The Plotly chart shows a red "Error Window" highlight.
- The Agent Insights panel at the bottom displays content (fallback output is acceptable at this stage).
