import os
import json
from typing import TypedDict, Optional
from openai import OpenAI
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

load_dotenv()


class OMState(TypedDict):
    # Inputs
    event_details: dict
    loss_kwh: float
    pr_result: dict
    clearsky_result: dict

    # Intermediate
    fault_type: str  # "weather_related" | "electrical_fault" | "data_missing"
    weather_analysis: Optional[dict]
    fault_analysis: Optional[dict]

    # Output
    final_report: Optional[dict]


def _get_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )


def collect_data(state: OMState) -> OMState:
    csi = state["clearsky_result"].get("mean_clearsky_index")
    error_code = state["event_details"].get("error_code")

    if csi is not None and csi < 0.4:
        fault_type = "weather_related"
    elif error_code in (None, "0000000", ""):
        fault_type = "data_missing"
    else:
        fault_type = "electrical_fault"

    return {**state, "fault_type": fault_type}


def weather_analysis(state: OMState) -> OMState:
    ev = state["event_details"]
    csi = state["clearsky_result"].get("mean_clearsky_index") or 0.0
    prompt = (
        f"The inverter {ev.get('inverter_id')} lost {state['loss_kwh']:.2f} kWh "
        f"during a period with clear-sky index {csi:.2f}. "
        "The loss may be partly weather-driven. Assess how much of the loss is attributable "
        "to weather vs equipment. Respond in JSON: "
        '{"weather_contribution_pct": <0-100>, "adjusted_loss_kwh": <float>, '
        '"assessment": "<string>", "recommended_action": "<string>"}'
    )
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that outputs JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
    except Exception:
        result = {
            "weather_contribution_pct": 50,
            "adjusted_loss_kwh": state["loss_kwh"] * 0.5,
            "assessment": "Fallback: Unable to reach API. Weather contribution estimated at 50%.",
            "recommended_action": "Check API key and review weather data manually.",
        }
    return {**state, "weather_analysis": result}


def fault_diagnosis(state: OMState) -> OMState:
    ev = state["event_details"]
    pr = state["pr_result"].get("pr")
    wc_pr = state["pr_result"].get("weather_corrected_pr")
    csi = state["clearsky_result"].get("mean_clearsky_index")

    csi_str = f"{csi:.2f}" if csi is not None else "N/A"
    pr_str = f"{pr:.1%}" if pr is not None else "N/A"
    wc_pr_str = f"{wc_pr:.1%}" if wc_pr is not None else "N/A"

    prompt = f"""
You are an expert Solar Plant O&M Copilot. Analyze the following incident data and provide actionable advice.

Incident Context:
- Inverter ID: {ev.get('inverter_id')}
- Error Code: {ev.get('error_code')}
- Description: {ev.get('description')}
- Linked Ticket: {ev.get('ticket_id', 'None')}
- Duration: {ev.get('start_time')} to {ev.get('end_time')}
- Estimated Energy Loss: {state['loss_kwh']:.2f} kWh

Weather & Performance Context:
- Clear-sky index: {csi_str} (above 0.7 = clear sky confirmed, fault is NOT weather)
- Performance Ratio: {pr_str} (normal range 75-85%)
- Weather-corrected PR: {wc_pr_str}

Provide your response in strictly JSON format:
{{
    "incident_summary": "One sentence summary of what happened and the impact.",
    "likely_cause": "Based on the error description, what physically went wrong.",
    "suggested_action": "What the O&M team should do next.",
    "confidence": "High/Medium/Low"
}}
"""
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that outputs JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
    except Exception:
        result = {
            "incident_summary": f"Fallback: Inverter {ev.get('inverter_id')} lost {state['loss_kwh']:.2f} kWh.",
            "likely_cause": f"Fallback: {ev.get('description')}.",
            "suggested_action": "Check API key. Inspect inverter manually.",
            "confidence": "Low",
        }
    return {**state, "fault_analysis": result}


def data_quality_report(state: OMState) -> OMState:
    result = {
        "incident_summary": "Insufficient data to diagnose fault.",
        "likely_cause": "Error code is missing or zero — may be a data logging issue.",
        "suggested_action": "Check monitoring system connectivity for this inverter.",
        "confidence": "Low",
    }
    return {**state, "fault_analysis": result}


def generate_report(state: OMState) -> OMState:
    if state["fault_type"] == "weather_related":
        base = state.get("weather_analysis") or {}
    else:
        base = state.get("fault_analysis") or {}

    final_report = {
        **base,
        "fault_type": state["fault_type"],
        "pr": state["pr_result"].get("pr"),
        "weather_corrected_pr": state["pr_result"].get("weather_corrected_pr"),
        "clearsky_index": state["clearsky_result"].get("mean_clearsky_index"),
        "workflow_path": state["fault_type"],
    }
    return {**state, "final_report": final_report}


def route_after_collect(state: OMState) -> str:
    return state["fault_type"]


builder = StateGraph(OMState)
builder.add_node("collect_data", collect_data)
builder.add_node("weather_analysis", weather_analysis)
builder.add_node("fault_diagnosis", fault_diagnosis)
builder.add_node("data_quality_report", data_quality_report)
builder.add_node("generate_report", generate_report)

builder.set_entry_point("collect_data")
builder.add_conditional_edges(
    "collect_data",
    route_after_collect,
    {
        "weather_related": "weather_analysis",
        "electrical_fault": "fault_diagnosis",
        "data_missing": "data_quality_report",
    },
)
builder.add_edge("weather_analysis", "generate_report")
builder.add_edge("fault_diagnosis", "generate_report")
builder.add_edge("data_quality_report", "generate_report")
builder.add_edge("generate_report", END)

graph = builder.compile()


class OMAgent:
    def __init__(self):
        self._client = None
        self._graph = graph

    @property
    def client(self):
        if self._client is None:
            self._client = OpenAI(
                api_key=os.environ.get("DEEPSEEK_API_KEY"),
                base_url="https://api.deepseek.com",
            )
        return self._client

    def generate_insights(
        self,
        event_details: dict,
        loss_kwh: float,
        pr_result: dict = None,
        clearsky_result: dict = None,
    ) -> dict:
        initial_state = OMState(
            event_details=event_details,
            loss_kwh=loss_kwh,
            pr_result=pr_result or {},
            clearsky_result=clearsky_result or {},
            fault_type="",
            weather_analysis=None,
            fault_analysis=None,
            final_report=None,
        )
        try:
            result = self._graph.invoke(initial_state)
            return result["final_report"]
        except Exception as e:
            return {
                "incident_summary": f"Fallback: Inverter {event_details.get('inverter_id')} lost {loss_kwh:.2f} kWh.",
                "likely_cause": f"Fallback: {event_details.get('description')}.",
                "suggested_action": "Check API key. Inspect inverter manually.",
                "confidence": "Low",
                "fault_type": "unknown",
                "workflow_path": "fallback",
            }
