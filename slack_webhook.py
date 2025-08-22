from fastapi import APIRouter, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from slack_sdk.signature import SignatureVerifier
from slack_sdk import WebClient
import os, json
from dotenv import dotenv_values
from agent import graph, IssueType

slack_router = APIRouter()

config = {**dotenv_values(".env"), **os.environ}

sig_verifier = SignatureVerifier(signing_secret=config["SLACK_SIGNING_SECRET"])
slack = WebClient(token=config["SLACK_BOT_TOKEN"])

@slack_router.post("/actions")
async def slack_actions(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not sig_verifier.is_valid_request(body, request.headers):
        raise HTTPException(status_code=403, detail="Invalid signature")

    form = await request.form()
    payload = json.loads(form["payload"])
    action = payload["actions"][0]
    data = json.loads(action.get("value", "{}"))
    thread_id = data.get("thread_id")
    level = data.get("level")
    user_id = payload["user"]["id"]
    channel = payload["channel"]["id"]
    ts = payload["message"]["ts"]
    payload = json.loads(form["payload"])
    type = IssueType(data["type"])

    slack.chat_update(
        channel=channel,
        ts=ts,
        text=f"Triage set to {level} by <@{user_id}>",
        blocks=None
    )

    if not thread_id:
        thread_id = f"{channel}:{ts}"

    background.add_task(resume_graph, level, thread_id, channel, ts, user_id, type)
    return JSONResponse({"response_action": "update", "text": "Thanks — processing your decision…"})

def resume_graph(level: str, thread_id: str, channel: str, ts: str, user_id: str, type: IssueType):
    snap = graph.get_state(config={"configurable": {"thread_id": thread_id}})
    new_config = graph.update_state(snap.config, values={"triage": {"level": level, "by": user_id},  "tags": [f"type:{type.value}" if type else "type:unknown", "source:slack"],  "run_name": "advice_flow"})
    graph.invoke(None, config=new_config)
    try:
        slack.chat_update(
            channel=channel,
            ts=ts,
            text=f"Decision: {level} by <@{user_id}>",
        )
    except Exception as e:
        print(f"Slack update error: {e}")