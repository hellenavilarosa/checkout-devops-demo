"""EventBridge -> AWS DevOps Agent bridge.

Triggered by a CloudWatch "Alarm State Change" event. When the alarm enters
ALARM, opens an INVESTIGATION backlog task in the configured Agent Space so
the agent starts investigating without anyone clicking anything.

Env:
  AGENT_SPACE_ID   target Agent Space
  REGION           DevOps Agent region (defaults to the function's region)
"""

import json
import os

import boto3

AGENT_SPACE_ID = os.environ["AGENT_SPACE_ID"]
REGION = os.environ.get("REGION") or os.environ["AWS_REGION"]
client = boto3.client("devops-agent", region_name=REGION)


def handler(event, _ctx):
    detail = event.get("detail", {})
    alarm = detail.get("alarmName", "unknown-alarm")
    state = detail.get("state", {})
    if state.get("value") != "ALARM":
        return {"skipped": True, "alarm": alarm, "state": state.get("value")}

    account = event.get("account", "")
    region = event.get("region", REGION)
    reason = state.get("reason", "")
    desc = detail.get("configuration", {}).get("description", "")
    console_url = (f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
                   f"#alarmsV2:alarm/{alarm}")

    description = (
        f"CloudWatch alarm `{alarm}` entered ALARM in account {account} ({region}).\n\n"
        f"Alarm description: {desc}\n\n"
        f"State reason: {reason}\n\n"
        f"Alarm: {console_url}\n\n"
        "Investigate the root cause across EKS workloads (Container Insights metrics "
        "and application logs), recent configuration/deployment changes (ArgoCD-managed "
        "Kubernetes resources, ConfigMaps) and dependencies. Propose a mitigation plan."
    )

    # Dedup: a flapping alarm (OK->ALARM->OK->ALARM) must not open a second
    # investigation while one for the same alarm is still running.
    title = f"[ALARM] {alarm}"[:400]
    try:
        tasks = client.list_backlog_tasks(agentSpaceId=AGENT_SPACE_ID).get("tasks", [])
        active = [t for t in tasks if t.get("title") == title
                  and t.get("status") in ("PENDING_START", "IN_PROGRESS", "PENDING")]
    except Exception as exc:  # listing is best-effort; never block the investigation
        print(json.dumps({"alarm": alarm, "dedup_check_failed": str(exc)}))
        active = []
    if active:
        print(json.dumps({"alarm": alarm, "skipped": "investigation already active",
                          "taskId": active[0].get("taskId")}))
        return {"skipped": True, "alarm": alarm, "reason": "active investigation",
                "taskId": active[0].get("taskId")}

    # Idempotent per alarm transition: one task per alarm + state change timestamp
    token = f"{alarm}-{state.get('timestamp', '')}".replace(":", "").replace(".", "")[:128]

    resp = client.create_backlog_task(
        agentSpaceId=AGENT_SPACE_ID,
        taskType="INVESTIGATION",
        priority="HIGH",
        title=title,
        description=description[:10000],
        clientToken=token,
    )
    task_id = resp.get("taskId")
    print(json.dumps({"alarm": alarm, "taskId": task_id, "executionId": resp.get("executionId")}))
    return {"alarm": alarm, "taskId": task_id}
