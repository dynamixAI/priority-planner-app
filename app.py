from flask import Flask, render_template, request, jsonify
from datetime import datetime

app = Flask(__name__)

# Default priority schema
state = {
    "priorities": [
        {"id": "p1", "label": "Urgent / Core", "color": "#ef5350"},
        {"id": "p2", "label": "Teaching / Delivery", "color": "#fdd835"},
        {"id": "p3", "label": "1-to-1 Support", "color": "#fb8c00"},
        {"id": "p4", "label": "Admin / Prep", "color": "#64b5f6"},
        {"id": "break", "label": "Break / Lunch", "color": "#b0bec5"}
    ],
    "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
    "time_slots": [
        "8.30–9.10", "9.10–10.15", "10.15–10.40",
        "10.40–11.45", "11.50–12.55", "12.55–13.40",
        "13.40–14.45", "14.50–15.55", "16.00–16.30"
    ],
    "tasks": [
        {
            "id": 1,
            "day": "Tuesday",
            "slot_idx": 1,
            "title": "English GCSE",
            "detail": "Victoria (A115)",
            "priority_id": "p2",
            "status": "pending"  # pending, completed, struggling, rescheduled
        },
        {
            "id": 2,
            "day": "Tuesday",
            "slot_idx": 4,
            "title": "Criminology",
            "detail": "Katie (B005)",
            "priority_id": "p1",
            "status": "pending"
        },
        {
            "id": 3,
            "day": "Wednesday",
            "slot_idx": 4,
            "title": "Daisie Price",
            "detail": "1-1 Support",
            "priority_id": "p3",
            "status": "pending"
        }
    ]
}

@app.route("/")
def index():
    return render_template("dashboard.html", state=state)

@app.route("/api/priorities", methods=["POST"])
def update_priorities():
    data = request.get_json()
    if "priorities" in data:
        state["priorities"] = data["priorities"]
    return jsonify({"status": "success", "priorities": state["priorities"]})

@app.route("/api/task/add", methods=["POST"])
def add_task():
    data = request.get_json()
    new_task = {
        "id": len(state["tasks"]) + 1,
        "day": data.get("day"),
        "slot_idx": int(data.get("slot_idx")),
        "title": data.get("title", "New Task"),
        "detail": data.get("detail", ""),
        "priority_id": data.get("priority_id", "p1"),
        "status": "pending"
    }
    state["tasks"].append(new_task)
    return jsonify({"status": "success", "task": new_task})

@app.route("/api/task/status", methods=["POST"])
def update_status():
    data = request.get_json()
    task_id = int(data.get("task_id"))
    new_status = data.get("status")

    for task in state["tasks"]:
        if task["id"] == task_id:
            task["status"] = new_status
            return jsonify({"status": "success", "task": task})

    return jsonify({"status": "error", "message": "Task not found"}), 404

@app.route("/api/task/reschedule", methods=["POST"])
def reschedule_task():
    data = request.get_json()
    task_id = int(data.get("task_id"))
    target_day = data.get("new_day")
    target_slot = int(data.get("new_slot"))

    for task in state["tasks"]:
        if task["id"] == task_id:
            task["day"] = target_day
            task["slot_idx"] = target_slot
            task["status"] = "rescheduled"
            return jsonify({"status": "success", "task": task})

    return jsonify({"status": "error", "message": "Task not found"}), 404

@app.route("/api/analytics", methods=["GET"])
def get_analytics():
    total = len(state["tasks"])
    completed = sum(1 for t in state["tasks"] if t["status"] == "completed")
    rescheduled = sum(1 for t in state["tasks"] if t["status"] == "rescheduled")
    pending = sum(1 for t in state["tasks"] if t["status"] == "pending")

    # Priority distribution
    distribution = {}
    for p in state["priorities"]:
        count = sum(1 for t in state["tasks"] if t["priority_id"] == p["id"])
        distribution[p["label"]] = count

    return jsonify({
        "total": total,
        "completed": completed,
        "rescheduled": rescheduled,
        "pending": pending,
        "completion_rate": round((completed / total) * 100, 1) if total > 0 else 0,
        "distribution": distribution
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
