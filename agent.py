from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI
from langchain_core.runnables import RunnableLambda, RunnableConfig
from langsmith import traceable
from typing import TypedDict, Dict, Any, List, Optional
from enum import Enum
from pydantic import BaseModel
from dotenv import dotenv_values
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from langgraph.checkpoint.sqlite import SqliteSaver
import os
import json
import sqlite3

config = {**dotenv_values(".env"), **os.environ}

llm = ChatOpenAI(
    openai_api_key=config["OPEN_API_KEY"],
    model_name="azure/gpt-4.1",
    openai_api_base=config["OPEN_API_URL"],
)

TRIAGE_LEVELS = [
    ("P0 Critical", "P0"),
    ("P1 High", "P1"),
    ("P2 Medium", "P2"),
    ("P3 Low", "P3"),
]

slack = WebClient(token=config["SLACK_BOT_TOKEN"])

class IssueType(str, Enum):
    APP_ISSUE = "APP_ISSUE"
    WEBSITE_ISSUE = "WEBSITE_ISSUE"
    PASSENGER_ISSUE = "PASSENGER_ISSUE"
    CHAUFFEUR_ISSUE = "CHAUFFEUR_ISSUE"
    SERVICE_PROVIDER_ISSUE = "SERVICE_PROVIDER_ISSUE"

CHANNEL_MAP = {
    IssueType.APP_ISSUE: config.get("SLACK_CH_APP"),
    IssueType.WEBSITE_ISSUE: config.get("SLACK_CH_WEBSITE"),
    IssueType.PASSENGER_ISSUE: config.get("SLACK_CH_PASSENGER"),
    IssueType.CHAUFFEUR_ISSUE: config.get("SLACK_CH_CHAUFFEUR"),
    IssueType.SERVICE_PROVIDER_ISSUE: config.get("SLACK_CH_PROVIDER"),
}
DEFAULT_CHANNEL = config.get("SLACK_CH_DEFAULT")

class IssueClassification(BaseModel):
    type: IssueType
    rationale: str

class AdviceModel(BaseModel):
    summary: str
    steps: List[str]
    notify: Optional[List[str]] = None

class IncidentState(TypedDict, total=False):
    report: str
    type: IssueType
    rationale: str
    slack: Dict[str, Any]
    triage: Dict[str, Any]

SYSTEM_PROMPT = (
    "You are a strict classifier. "
    "Choose exactly one label from: APP_ISSUE, WEBSITE_ISSUE, PASSENGER_ISSUE, "
    "COURIER_ISSUE, SERVICE_PROVIDER_ISSUE. "
    "Return JSON with fields: type, rationale. Do not add extra fields."
)

ADVICE_PROMPT = (
    "You are an SRE triage advisor. Based on issue type, triage level, and the user report, "
    "produce a concise summary and 3–5 safe, read-only diagnostic or procedural steps. "
    "If driver drunk immediately terminate the ride and give steps for passenger safety"
    "Tailor steps to the type. For P0/P1 include who to notify. "
    "Return JSON with: summary, steps, notify (optional)."
)

FALLBACK_STEPS = {
    IssueType.APP_ISSUE: [
        "Check recent mobile release crash rate and top stack traces.",
        "Correlate crashes with OS/app version and device model.",
        "Review last app release notes and error telemetry.",
    ],
    IssueType.WEBSITE_ISSUE: [
        "Check upstream error rates and latency for affected endpoints.",
        "Verify recent deploys to web/app gateway and roll back if correlated.",
        "Inspect CDN/cache status and purge if serving stale/error content.",
    ],
    IssueType.PASSENGER_ISSUE: [
        "Review user-facing flows affected; replicate the issue if possible.",
        "Check payment/location services health and recent changes.",
        "Prepare status page update if widespread.",
    ],
    IssueType.CHAUFFEUR_ISSUE: [
        "Check courier app API endpoints and auth flows.",
        "Validate geolocation and job assignment services.",
        "Coordinate with ops for manual dispatch fallback.",
    ],
    IssueType.SERVICE_PROVIDER_ISSUE: [
        "Contact the provider’s status page/support for incident notices.",
        "Validate API error codes and rate limits.",
        "Implement retries/backoff and failover if available.",
    ],
}

classifier = llm.with_structured_output(IssueClassification)
adviser = llm.with_structured_output(AdviceModel)

def triage_actions_block(thread_id: str, issue_type: IssueType):
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": f"set_triage_{value}",
            "value": json.dumps({"thread_id": thread_id, "level": value, "type": issue_type}),
        }
        for label, value in TRIAGE_LEVELS
    ]
    return {"type": "actions", "block_id": "triage", "elements": buttons}

def generate_advice(type_: IssueType, level: str, report: str) -> AdviceModel:
    try:
        res = adviser.invoke([
            {"role": "system", "content": ADVICE_PROMPT},
            {"role": "user", "content": f"type={type_.value}, level={level}, report={report}"},
        ])
        if not res.steps or len(res.steps) < 3:
            raise ValueError("Too few steps")
        return res
    except Exception:
        base = FALLBACK_STEPS.get(type_, ["Gather logs and metrics for the affected service."])
        if level in ("P0", "P1"):
            base = [f"[PRIORITY {level}] Page on-call and create an incident channel."] + base
            return AdviceModel(summary=f"Triage guidance for {type_.value} at {level}", steps=base, notify=(["on-call", "incident_commander"] if level in ("P0","P1") else None))
@traceable(run_type="tool", name="give_advice")
def give_advice(state: IncidentState) -> IncidentState:
    type_ = state.get("type")
    triage = state.get("triage", {})
    level = triage.get("level")
    report = state.get("report", "")
    slack_meta = state.get("slack", {})
    channel = slack_meta.get("channel")
    parent_ts = slack_meta.get("ts")

    if not type_ or not level:
        print("Missing type or triage level; cannot generate advice.")
        return {}

    advice = generate_advice(type_, level, report)

    steps_text = "\n".join(f"- {s}" for s in advice.steps)
    notify_text = f"\nNotify: {', '.join(advice.notify)}" if advice.notify else ""
    text = f"Advice for {type_.value} at {level}\nSummary: {advice.summary}\n{steps_text}{notify_text}"

    posted = {}
    try:
        if channel and parent_ts:
            resp = slack.chat_postMessage(channel=channel, thread_ts=parent_ts, text=text)
            posted = {"channel": resp["channel"], "ts": resp["ts"]}
        else:
            print("Slack metadata missing; printing advice instead:\n", text)
    except SlackApiError as e:
        print(f"Slack post failed: {e.response.get('error')}")

    return {
        "advice": {
            "summary": advice.summary,
            "steps": advice.steps,
            "notify": advice.notify,
            "posted": posted or None,
            "level": level,
        }
    }

@traceable(run_type="tool", name="classify_issue")
def classify_issue(state: IncidentState) -> IncidentState:
    report = state["report"]
    result = classifier.invoke([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": report},
    ])
    return {"report": report, "type": result.type, "rationale": result.rationale}

@traceable(run_type="tool", name="post_to_slack")
def post_to_slack(state: IncidentState, config: RunnableConfig) -> IncidentState:
    issue_type = state["type"]
    report = state.get("report", "")
    rationale = state.get("rationale", "")
    channel = CHANNEL_MAP.get(issue_type) or DEFAULT_CHANNEL
    if not channel:
        print("No Slack channel configured; skipping post.")
        return {}
    configurable = config.get("configurable", {}) or {}
    thread_id = configurable.get("thread_id")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":rotating_light: {issue_type.value}\nReport: {report}\nReasoning: {rationale}"}},
        triage_actions_block(thread_id, issue_type),
    ]

    try:
        resp = slack.chat_postMessage(channel=channel, text="Incident triage", blocks=blocks)
        return {"slack": {"channel": resp["channel"], "ts": resp["ts"]}}
    except SlackApiError as e:
        err = e.response.get("error", str(e))
        print(f"Slack post failed: {err}")
        return {"slack": {"error": err}}

builder = StateGraph(IncidentState)
builder.add_node("classify_issue", RunnableLambda(classify_issue).with_config(tags=["node:classify_issue"]))
builder.add_node("post_to_slack", RunnableLambda(post_to_slack).with_config(tags=["node:post_to_slack"]))
builder.add_node("give_advice", RunnableLambda(give_advice).with_config(tags=["node:give_advice"]))
builder.add_edge(START, "classify_issue")
builder.add_edge("classify_issue", "post_to_slack")
builder.add_edge("post_to_slack", "give_advice")
builder.add_edge("give_advice", END)


conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False)
memory = SqliteSaver(conn)
graph = builder.compile(checkpointer=memory, interrupt_after=["post_to_slack"])

tid = "test-" + os.urandom(4).hex()
graph.invoke({"thread_id": tid, "report": "test report"},
config={"configurable": {"thread_id": tid}})
snap = graph.get_state(config={"configurable": {"thread_id": tid}})
