import uuid
from fastapi import FastAPI
from agent import graph
from slack_webhook import slack_router
from api import api_router

thread_id = f"inc-{uuid.uuid4().hex[:8]}"
app = FastAPI()
app.include_router(slack_router, prefix="/slack")
app.include_router(api_router, prefix="/api")