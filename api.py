from fastapi import BackgroundTasks, Form, APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from agent import graph
import uuid

INDEX_HTML = """<!doctype html>

<html lang="en">
<head><meta charset="utf-8"><title>Incident Reporter</title></head>
<body>
<h1>Report an Issue</h1>
<form id="reportForm">
<textarea id="report" name="report" required></textarea>
<button type="submit">Submit</button>
</form>
<div id="result"></div>
<script>
document.addEventListener('DOMContentLoaded', () => {
const form = document.getElementById('reportForm');
const result = document.getElementById('result');
form.addEventListener('submit', async (e) => {
e.preventDefault();
const report = document.getElementById('report').value.trim();
if (!report) return;
result.textContent = "Submitting...";
const res = await fetch('/api/report', {
method: 'POST',
headers: {'Content-Type': 'application/x-www-form-urlencoded'},
body: new URLSearchParams({ report }),
});
const data = await res.json();
result.textContent = JSON.stringify(data);
});
});
</script>
</body>
</html>"""

api_router = APIRouter()

@api_router.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML

@api_router.post("/report")
async def submit_report(report: str = Form(...), background: BackgroundTasks = None):
    tid = "web-" + uuid.uuid4().hex[:8]

    async def start():
        graph.invoke({"thread_id": tid, "report": report}, config={"configurable": {"thread_id": tid}, "tags": ["source:web"], "run_name": "triage_flow"})

    if background is not None:
        background.add_task(start)
    else:
        graph.invoke({"thread_id": tid, "report": report}, config={"configurable": {"thread_id": tid}, "tags": ["source:web"], "run_name": "triage_flow"})

    return JSONResponse({"ok": True, "thread_id": tid})

@api_router.get("/status/{thread_id}")
def status(thread_id: str):
    state = graph.get_state(config={"configurable": {"thread_id": thread_id}})
    values = state.get("values") if isinstance(state, dict) and "values" in state else state
    view = {
        "type": getattr(values.get("type"), "value", values.get("type")),
        "rationale": values.get("rationale"),
        "triage": values.get("triage"),
        "slack": values.get("slack"),
        "has_report": bool(values.get("report")),
    }
    return JSONResponse({"thread_id": thread_id, "state": view})
