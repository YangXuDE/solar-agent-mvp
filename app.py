import streamlit as st
import plotly.graph_objects as go
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "src"))
from data_pipeline import init_duckdb
from analytics_engine import AnalyticsEngine
from agent_core import OMAgent

st.set_page_config(page_title="Solar O&M Copilot", layout="wide")


@st.cache_resource
def setup():
    init_duckdb()
    return AnalyticsEngine(), OMAgent()


engine, agent = setup()

st.title("☀️ Solar O&M Copilot")

# ── Plant Overview KPI Dashboard ────────────────────────────────────────────
with st.spinner("Loading plant overview..."):
    ov = engine.get_plant_overview()

st.subheader(f"🏭 Plant Overview — {ov['latest_date']}")

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "⚡ Total Generation",
    f"{ov['total_kwh'] / 1000:.2f} MWh",
    help="Sum of all inverter output for the latest data day (5-min resolution, ÷12 for kWh)"
)
k2.metric(
    "✅ Online Inverters",
    f"{ov['online_inv']} / {ov['total_inv']}",
    delta=f"{ov['total_inv'] - ov['online_inv']} offline" if ov['total_inv'] - ov['online_inv'] else "All online",
    delta_color="inverse",
)
k3.metric(
    "🚨 Active Alarms",
    ov["active_alarms"],
    delta_color="inverse",
)
k4.metric(
    "📉 Est. Energy Loss",
    f"{ov['est_loss_kwh']:.1f} kWh",
    help="Estimated loss for fault events active on the latest data day (actual vs peer baseline)"
)

# Hourly generation bar chart
hourly = ov["hourly_df"]
if not hourly.empty:
    fig_hourly = go.Figure(go.Bar(
        x=hourly["hour"],
        y=hourly["kwh"],
        marker_color="gold",
        name="Generation (kWh)",
    ))
    fig_hourly.update_layout(
        height=200,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="kWh",
        xaxis_title=None,
        showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(gridcolor="rgba(200,200,200,0.3)"),
    )
    st.plotly_chart(fig_hourly, use_container_width=True)

st.markdown("---")

# ── Load all events once ────────────────────────────────────────────────────
events_df = engine.get_events()

if events_df.empty:
    st.error("No events found in database.")
    st.stop()

# ── Sidebar: filter panel ───────────────────────────────────────────────────
st.sidebar.header("🔍 Filter Events")

all_inverters = sorted(events_df["inverter_id"].unique().tolist())
selected_inverters = st.sidebar.multiselect(
    "Inverter", all_inverters, placeholder="All inverters"
)

min_date = events_df["start_time"].min().date()
max_date = events_df["start_time"].max().date()
date_range = st.sidebar.date_input(
    "Date Range",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date,
)

keyword = st.sidebar.text_input("Search description / error code", "")

# ── Apply filters ───────────────────────────────────────────────────────────
filtered = events_df.copy()

if selected_inverters:
    filtered = filtered[filtered["inverter_id"].isin(selected_inverters)]

if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    filtered = filtered[
        (filtered["start_time"].dt.date >= date_range[0])
        & (filtered["start_time"].dt.date <= date_range[1])
    ]

if keyword:
    mask = filtered["description"].str.contains(
        keyword, case=False, na=False
    ) | filtered["error_code"].str.contains(keyword, case=False, na=False)
    filtered = filtered[mask]

# ── Events table ────────────────────────────────────────────────────────────
st.subheader(f"📋 Fault Events  —  {len(filtered):,} found")

display_df = filtered[
    ["event_id", "inverter_id", "start_time", "end_time", "error_code", "description"]
].copy()
display_df["start_time"] = display_df["start_time"].dt.strftime("%Y-%m-%d %H:%M")
display_df["end_time"] = display_df["end_time"].dt.strftime("%Y-%m-%d %H:%M")
display_df.columns = ["ID", "Inverter", "Start", "End", "Error Code", "Description"]

selection = st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    height=320,
)

# ── Detail view ─────────────────────────────────────────────────────────────
if not selection.selection.rows:
    st.info("👆 Click a row above to analyze the event in detail.")
    st.stop()

row_idx = selection.selection.rows[0]
selected_event_id = filtered.iloc[row_idx]["event_id"]

st.markdown("---")

with st.spinner("Analyzing data and calculating impact..."):
    event_details = engine.get_event_details(selected_event_id)
    loss_kwh, ts_df = engine.calculate_impact(
        event_details["inverter_id"],
        event_details["start_time"],
        event_details["end_time"],
    )

# ── Summary cards ───────────────────────────────────────────────────────────
col1, col2, col3, col4 = st.columns(4)
col1.metric("Inverter", event_details["inverter_id"])
col2.metric("Error Code", event_details["error_code"])
col3.metric("Est. Energy Loss", f"{loss_kwh:.2f} kWh")
col4.metric("Linked Ticket", event_details.get("ticket_id") or "—")

st.markdown("---")

# ── Power chart ─────────────────────────────────────────────────────────────
st.subheader("📈 Power Output vs Peer Baseline")

if ts_df.empty:
    st.warning("No daytime power data available for this event period.")
else:
    actual_all_zero = (ts_df["actual_power"] == 0).all()
    if actual_all_zero:
        st.warning(
            "⚠️ This inverter reported **0 kW output** throughout the entire chart window "
            "(including the 30-minute context before/after the event). "
            "The inverter was likely completely offline — not just during this specific error code window."
        )

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=ts_df["timestamp"],
            y=ts_df["baseline_power"],
            mode="lines",
            name="Peer Baseline (Expected)",
            line=dict(dash="dash", color="gray"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=ts_df["timestamp"],
            y=ts_df["actual_power"],
            mode="lines",
            name="Actual Power",
            line=dict(color="royalblue"),
        )
    )

    # Clamp vrect to the plotted data range — the solar_altitude filter can
    # exclude sunrise/sunset boundary timestamps, making ev_start/ev_end fall
    # outside the chart's x range and producing empty whitespace on the edges.
    data_start = ts_df["timestamp"].min()
    data_end = ts_df["timestamp"].max()
    vrect_x0 = max(event_details["start_time"], data_start)
    vrect_x1 = min(event_details["end_time"], data_end)
    if vrect_x0 < vrect_x1:
        fig.add_vrect(
            x0=vrect_x0,
            x1=vrect_x1,
            fillcolor="red",
            opacity=0.15,
            layer="below",
            line_width=0,
            annotation_text="Error Window",
            annotation_position="top left",
        )

    fig.update_layout(
        height=400,
        margin=dict(l=0, r=0, t=30, b=0),
        yaxis_title="Active Power (kW)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)

# ── AI insights ─────────────────────────────────────────────────────────────
st.subheader("🤖 Agent Insights & Recommendations")

with st.spinner("Generating AI insights..."):
    insights = agent.generate_insights(event_details, loss_kwh)

st.info(f"**Incident Summary:** {insights.get('incident_summary')}")
st.warning(f"**Likely Cause:** {insights.get('likely_cause')}")
st.success(f"**Suggested Action:** {insights.get('suggested_action')}")
st.caption(f"Agent Confidence: {insights.get('confidence')}")
