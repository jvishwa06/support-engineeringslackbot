import os
import logging
import csv
from datetime import datetime, timedelta
from fastapi import FastAPI
from slack_sdk.web import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from dotenv import load_dotenv
from dateutil import parser
import pytz
from ticketstore import get_all_tickets, update_ticket, save_ticket, get_escalation_members
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
PORT = int(os.getenv("PORT"))
CHANNEL = os.getenv("CHANNEL")
FRT_THRESHOLDS = {
    "High": float(os.getenv("FRT_HOURS_HIGH")),      # 2 min
    "Medium": float(os.getenv("FRT_HOURS_MEDIUM")),  # 3 min
    "Low": float(os.getenv("FRT_HOURS_LOW"))         # 5 min
}
FRT_ESCALATION_HOURS = float(os.getenv("FRT_ESCALATION_HOURS", "0.0167"))

app = FastAPI()
web_client = WebClient(token=BOT_TOKEN)
socket_client = SocketModeClient(app_token=APP_TOKEN, web_client=web_client)
scheduler = BackgroundScheduler()
scheduler.start()

def get_escalation_members(product):
    if not product:
        logging.warning("Product is None in get_escalation_members")
        return []
    try:
        with open("escalationmatrixid.csv", "r") as f:
            reader = csv.reader(f)
            for row in reader:
                if row[0].strip().lower() == product.strip().lower():
                    return [uid.strip() for uid in row[1:] if uid.strip()]
    except Exception as e:
        logging.error(f"Error reading escalationmatrixid.csv: {e}")
    return []

def get_assignee_level(product, assignee_id):
    """Return the level of the assignee for the product (L1, L2, L3)."""
    try:
        with open("escalationmatrixid.csv", "r") as f:
            reader = csv.reader(f)
            for row in reader:
                if row[0].strip().lower() == product.strip().lower():
                    levels = [uid.strip() for uid in row[1:] if uid.strip()]
                    if assignee_id in levels:
                        idx = levels.index(assignee_id)
                        return f"L{idx+1}", idx, levels
    except Exception as e:
        logging.error(f"Error reading escalationmatrixid.csv for assignee level: {e}")
    return None, None, []

def post_ticket_message(ticket):
    try:
        now = datetime.now(pytz.timezone("Asia/Kolkata"))
        formatted_time = now.strftime("%b %d, %Y, %-I:%M %p")
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*Ticket #{ticket['id']}*\n"
                f"*Title:* {ticket['title']}\n"
                f"*Description:* {ticket['description']}\n"
                f"*Client Name:* {ticket['client_name']}\n"
                f"*Client App ID:* {ticket['client_app_id']}\n"
                f"*Product:* {ticket['product']}\n"
                f"*Endpoint:* `{ticket['endpoint']}`\n"
                f"*Criticality:* {ticket['criticality']}\n"
                f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                f"*Raised By:* <@{ticket['raiser_id']}>\n"
                f"*Raised At:* {formatted_time}"
            )}}
        ]
        action_elements = []
        if ticket.get('frt_hours'):
            action_elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "Triage"},
                "action_id": "triage_button",
                "value": ticket['id']
            })
        if ticket.get('ttt_hours'):
            action_elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "Resolved"},
                "action_id": "resolved_button",
                "value": ticket['id']
            })
        if action_elements:
            blocks.append({
                "type": "actions",
                "elements": action_elements
            })
        result = web_client.chat_postMessage(
            channel=CHANNEL,
            blocks=blocks,
            text=(
                f"*Ticket #{ticket['id']}*\n"
                f"*Title:* {ticket['title']}\n"
                f"*Description:* {ticket['description']}\n"
                f"*Client Name:* {ticket['client_name']}\n"
                f"*Client App ID:* {ticket['client_app_id']}\n"
                f"*Product:* {ticket['product']}\n"
                f"*Endpoint:* `{ticket['endpoint']}`\n"
                f"*Criticality:* {ticket['criticality']}\n"
                f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                f"*Raised By:* <@{ticket['raiser_id']}>\n"
                f"*Raised At:* {formatted_time}"
            )
        )
        logging.info(f"Ticket #{ticket['id']} posted to Slack with ts={result.get('ts')}")
        ticket['message_ts'] = result.get('ts')

        frt_hours = FRT_THRESHOLDS[ticket['criticality']]
        half_frt = max(frt_hours / 2, 0.0167)
        reminder_time = now + timedelta(hours=half_frt)
        scheduler.add_job(
            send_fr_reminder,
            trigger=DateTrigger(run_date=reminder_time),
            args=[ticket],
            id=f"reminder_{ticket['id']}"
        )
        logging.info(f"Scheduled FRT reminder for ticket #{ticket['id']} at {reminder_time}")

        escalation_time = now + timedelta(hours=frt_hours - FRT_ESCALATION_HOURS)
        scheduler.add_job(
            send_escalation_reminder,
            trigger=DateTrigger(run_date=escalation_time),
            args=[ticket],
            id=f"escalation_{ticket['id']}"
        )
        logging.info(f"Scheduled escalation reminder for ticket #{ticket['id']} at {escalation_time}")
        return ticket['message_ts']
    except Exception as e:
        logging.error(f"Error posting message to Slack: {e}")
        return None

def send_fr_reminder(ticket):
    try:
        reminder_text = (
            f"⏰ FRT Reminder for Ticket #{ticket['id']}\n"
            f"<@{ticket['assignee_id']}> Please respond!"
        )
        web_client.chat_postMessage(
            channel=CHANNEL,
            thread_ts=ticket['message_ts'],
            text=reminder_text
        )
        logging.info(f"Reminder sent for ticket #{ticket['id']} to <@{ticket['assignee_id']}>.")
    except Exception as e:
        logging.error(f"Error sending FRT reminder: {e}")

def send_escalation_reminder(ticket):
    if not ticket.get("frt_hours"):
        assignee_level, idx, levels = get_assignee_level(ticket["product"], ticket["assignee_id"])
        if assignee_level and idx is not None and idx+1 < len(levels):
            next_level_id = levels[idx+1]
            reminder_text = (
                f"🚨 FRT Escalation for Ticket #{ticket['id']}\n"
                f"<@{next_level_id}> Please look into this ticket. Assignee <@{ticket['assignee_id']}> did not respond in time."
            )
            web_client.chat_postMessage(
                channel=CHANNEL,
                thread_ts=ticket['message_ts'],
                text=reminder_text
            )
            logging.info(f"Escalation reminder sent for ticket #{ticket['id']} to <@{next_level_id}>.")
        else:
            logging.info(f"No higher escalation level found for ticket #{ticket['id']}.")

@socket_client.socket_mode_request_listeners.append
def handle_events(client: SocketModeClient, req: SocketModeRequest):
    payload = req.payload

    if req.type == "slash_commands" and payload.get("command") == "/ticketbot":
        logging.info("Received /ticketbot command")
        product_options = []
        try:
            with open("escalationmatrixid.csv", "r") as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    product_options.append({
                        "text": {"type": "plain_text", "text": row[0]},
                        "value": row[0]
                    })
        except Exception as e:
            logging.error(f"Error reading escalationmatrixid.csv: {e}")

        client.web_client.views_open(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "submit_ticket",
                "title": {"type": "plain_text", "text": "New Ticket"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "blocks": [
                    {"type": "input", "block_id": "title", "label": {"type": "plain_text", "text": "Title"}, "element": {"type": "plain_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "description", "label": {"type": "plain_text", "text": "Description"}, "element": {"type": "plain_text_input", "action_id": "value", "multiline": True}},
                    {"type": "input", "block_id": "client_name", "label": {"type": "plain_text", "text": "Client Name"}, "element": {"type": "plain_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "client_app_id", "label": {"type": "plain_text", "text": "Client App ID"}, "element": {"type": "plain_text_input", "action_id": "value"}},
                    {
                        "type": "input",
                        "block_id": "criticality",
                        "label": {"type": "plain_text", "text": "Criticality"},
                        "element": {
                            "type": "static_select",
                            "action_id": "value",
                            "options": [
                                {"text": {"type": "plain_text", "text": "Low"}, "value": "Low"},
                                {"text": {"type": "plain_text", "text": "Medium"}, "value": "Medium"},
                                {"text": {"type": "plain_text", "text": "High"}, "value": "High"}
                            ]
                        }
                    },

                    {"type": "input", "block_id": "product", "label": {"type": "plain_text", "text": "Product"}, "element": {"type": "static_select", "action_id": "value", "options": product_options}},
                    {"type": "input", "block_id": "endpoint", "label": {"type": "plain_text", "text": "Endpoint (starts with /)"}, "element": {"type": "plain_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "assignee", "label": {"type": "plain_text", "text": "Assign To"}, "element": {"type": "users_select", "action_id": "value"}}
                ]
            }
        )
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    if payload.get("type") == "view_submission" and payload["view"]["callback_id"] == "submit_ticket":
        state = payload["view"]["state"]["values"]
        endpoint = state["endpoint"]["value"]["value"].strip()

        if not endpoint.startswith("/") or endpoint == "/":
            client.send_socket_mode_response(SocketModeResponse(
                envelope_id=req.envelope_id,
                payload={
                    "response_action": "errors",
                    "errors": {
                        "endpoint": "Invalid API endpoint. It must start with '/' and cannot be just '/'"
                    }
                }
            ))
            return

        ticket_id = datetime.now().strftime("%Y%m%d%H%M%S")
        raised_at = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
        user_id = payload["user"]["id"]

        try:
            raised_by_info = client.web_client.users_info(user=user_id)
            raised_by_name = raised_by_info["user"].get("real_name") or raised_by_info["user"].get("profile", {}).get("display_name") or user_id
        except Exception as e:
            logging.error(f"Error fetching raised_by name: {e}")
            raised_by_name = user_id
        try:
            assignee_id = state["assignee"]["value"]["selected_user"]
            assigned_to_info = client.web_client.users_info(user=assignee_id)
            assigned_to_name = assigned_to_info["user"].get("real_name") or assigned_to_info["user"].get("profile", {}).get("display_name") or assignee_id
        except Exception as e:
            logging.error(f"Error fetching assigned_to name: {e}")
            assigned_to_name = assignee_id

        ticket = {
            "id": ticket_id,
            "title": state["title"]["value"]["value"],
            "description": state["description"]["value"]["value"],
            "raised_by": raised_by_name,
            "raiser_id": user_id,
            "raised_at": raised_at,
            "client_name": state["client_name"]["value"]["value"],
            "client_app_id": state["client_app_id"]["value"]["value"],
            "criticality": state["criticality"]["value"]["selected_option"]["value"],
            "product": state["product"]["value"]["selected_option"]["value"],
            "endpoint": endpoint,
            "assigned_to": assigned_to_name,
            "assignee_id": assignee_id,
            "frt_hours": "",
            "message_ts": ""
        }

        logging.info(f"Saving ticket (pre-post): {ticket}")
        ticket["message_ts"] = post_ticket_message(ticket)
        save_ticket(ticket)
        
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    if req.type == "events_api" and payload.get("event", {}).get("type") == "message":
        event = payload["event"]
        thread_ts = event.get("thread_ts")
        user = event.get("user")
        if thread_ts and user:
            all_tickets = get_all_tickets()
            for ticket in all_tickets:
                if ticket.get("message_ts") == thread_ts or ticket.get("triage_ts") == thread_ts:
                    logging.info(f"Checking ticket #{ticket['id']} with thread_ts={thread_ts}")
                    try:
                        user_info = client.web_client.users_info(user=user)
                        responder_name = user_info["user"].get("real_name") or user_info["user"].get("profile", {}).get("display_name") or user
                    except Exception as e:
                        logging.error(f"Error fetching responder name: {e}")
                        responder_name = user
                    escalation_ids = get_escalation_members(ticket["product"])
                    if user in escalation_ids and not ticket.get("frt_hours"):
                        raised_at = parser.isoparse(ticket["raised_at"])
                        now = datetime.now(pytz.timezone("Asia/Kolkata"))
                        frt = (now - raised_at).total_seconds() / 3600
                        update_ticket(ticket["id"], {"frt_hours": f"{frt:.2f}"})
                        logging.info(f"✅ FRT for ticket #{ticket['id']} set to {frt:.2f} hours by <@{user}> ({responder_name})")
                        blocks = [
                            {"type": "section", "text": {"type": "mrkdwn", "text": (
                                f"*Ticket #{ticket['id']}*\n"
                                f"*Title:* {ticket['title']}\n"
                                f"*Description:* {ticket['description']}\n"
                                f"*Client Name:* {ticket['client_name']}\n"
                                f"*Client App ID:* {ticket['client_app_id']}\n"
                                f"*Product:* {ticket['product']}\n"
                                f"*Endpoint:* `{ticket['endpoint']}`\n"
                                f"*Criticality:* {ticket['criticality']}\n"
                                f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                                f"*Raised By:* <@{ticket['raiser_id']}> ({ticket['raised_by']})\n"
                                f"*Raised At:* {ticket['raised_at']}"
                            )}},
                            {"type": "actions", "elements": [
                                {"type": "button", "text": {"type": "plain_text", "text": "Triage"}, "action_id": "triage_button", "value": ticket['id']}
                            ]}
                        ]
                        client.web_client.chat_update(
                            channel=CHANNEL,
                            ts=ticket["message_ts"],
                            blocks=blocks,
                            text=(
                                f"*Ticket #{ticket['id']}*\n"
                                f"*Title:* {ticket['title']}\n"
                                f"*Description:* {ticket['description']}\n"
                                f"*Client Name:* {ticket['client_name']}\n"
                                f"*Client App ID:* {ticket['client_app_id']}\n"
                                f"*Product:* {ticket['product']}\n"
                                f"*Endpoint:* `{ticket['endpoint']}`\n"
                                f"*Criticality:* {ticket['criticality']}\n"
                                f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                                f"*Raised By:* <@{ticket['raiser_id']}> ({ticket['raised_by']})\n"
                                f"*Raised At:* {ticket['raised_at']}"
                            )
                        )
                        return
                    else:
                        logging.info(f"User <@{user}> ({responder_name}) is not an escalation member for ticket #{ticket['id']}. FRT will not be set.")
                        return

        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    if req.type == "interactive" and payload.get("actions", [{}])[0].get("action_id") == "triage_button":
        ticket_id = payload["actions"][0]["value"]
        client.web_client.views_open(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "triage_submit",
                "title": {"type": "plain_text", "text": "Triage Ticket"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "blocks": [
                    {"type": "input", "block_id": "issue", "label": {"type": "plain_text", "text": "What's the issue"}, "element": {"type": "plain_text_input", "action_id": "value", "multiline": True}},
                    {"type": "input", "block_id": "fix", "label": {"type": "plain_text", "text": "How to fix"}, "element": {"type": "plain_text_input", "action_id": "value", "multiline": True}},
                    {"type": "input", "block_id": "resolved_date", "label": {"type": "plain_text", "text": "When resolved (Date)"}, "element": {"type": "datepicker", "action_id": "value", "placeholder": {"type": "plain_text", "text": "Select a date"}}},
                    {"type": "input", "block_id": "resolved_time", "label": {"type": "plain_text", "text": "When resolved (Time)"}, "element": {"type": "timepicker", "action_id": "value", "placeholder": {"type": "plain_text", "text": "Select time"}}},
                    {"type": "input", "block_id": "escalate_to", "optional": True, "label": {"type": "plain_text", "text": "Escalate To"}, "element": {"type": "users_select", "action_id": "value"}}
                ],
                "private_metadata": ticket_id
            }
        )
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    if payload.get("type") == "view_submission" and payload["view"]["callback_id"] == "triage_submit":
        state = payload["view"]["state"]["values"]
        ticket_id = payload["view"].get("private_metadata")
        issue = state["issue"]["value"]["value"]
        fix = state["fix"]["value"]["value"]
        resolved_date = state["resolved_date"]["value"].get("selected_date", "")
        resolved_time = state["resolved_time"]["value"].get("selected_time", "")
        resolved = f"{resolved_date} {resolved_time}".strip()
        escalate_to = state.get("escalate_to", {}).get("value", {}).get("selected_user", "")
        thread_ts = None
        triaged_by_id = payload["user"]["id"]
        try:
            triaged_by_info = client.web_client.users_info(user=triaged_by_id)
            triaged_by_name = triaged_by_info["user"].get("real_name") or triaged_by_info["user"].get("profile", {}).get("display_name") or triaged_by_id
        except Exception as e:
            logging.error(f"Error fetching triaged_by name: {e}")
            triaged_by_name = triaged_by_id
        ttt_hours = None
        try:
            all_tickets = get_all_tickets()
            for ticket in all_tickets:
                if ticket["id"] == ticket_id:
                    thread_ts = ticket.get("message_ts")
                    raised_at = parser.isoparse(ticket["raised_at"])
                    now = datetime.now(pytz.timezone("Asia/Kolkata"))
                    ttt_hours = (now - raised_at).total_seconds() / 3600
                    escalate_to_id = escalate_to
                    escalate_to_name = ""
                    if escalate_to:
                        try:
                            escalate_to_info = client.web_client.users_info(user=escalate_to)
                            escalate_to_name = escalate_to_info["user"].get("real_name") or escalate_to_info["user"].get("profile", {}).get("display_name") or escalate_to
                        except Exception as e:
                            logging.error(f"Error fetching escalate_to name: {e}")
                            escalate_to_name = escalate_to
                    update_dict = {
                        "ttt_hours": f"{ttt_hours:.2f}",
                        "triaged_by": triaged_by_name,
                        "triaged_id": triaged_by_id,
                        "resolve_at": resolved
                    }
                    if escalate_to:
                        update_dict["escalate_to"] = escalate_to_name
                        update_dict["escalate_id"] = escalate_to_id
                    triage_msg = client.web_client.chat_postMessage(
                        channel=CHANNEL,
                        thread_ts=thread_ts,
                        text=(
                            f"*Triage for Ticket #{ticket_id}*\n"
                            f"*Issue:* {issue}\n"
                            f"*How to fix:* {fix}\n"
                            f"*When resolved:* {resolved}\n"
                            f"*Triaged by:* <@{triaged_by_id}>"
                        ),
                        blocks=[
                            {"type": "section", "text": {"type": "mrkdwn", "text": (
                                f"*Triage for Ticket #{ticket_id}*\n"
                                f"*Issue:* {issue}\n"
                                f"*How to fix:* {fix}\n"
                                f"*When resolved:* {resolved}\n"
                                f"*Triaged by:* <@{triaged_by_id}>"
                            )}}
                        ]
                    )
                    update_dict["triage_ts"] = triage_msg.get("ts")
                    update_ticket(ticket_id, update_dict)
                    blocks_main = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": (
                            f"*Ticket #{ticket['id']}*\n"
                            f"*Title:* {ticket['title']}\n"
                            f"*Description:* {ticket['description']}\n"
                            f"*Client Name:* {ticket['client_name']}\n"
                            f"*Client App ID:* {ticket['client_app_id']}\n"
                            f"*Product:* {ticket['product']}\n"
                            f"*Endpoint:* `{ticket['endpoint']}`\n"
                            f"*Criticality:* {ticket['criticality']}\n"
                            f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                            f"*Raised By:* <@{ticket['raiser_id']}> ({ticket['raised_by']})\n"
                            f"*Raised At:* {ticket['raised_at']}"
                        )}},
                        {"type": "actions", "elements": [
                            {"type": "button", "text": {"type": "plain_text", "text": "Triage"}, "action_id": "triage_button", "value": ticket['id']},
                            {"type": "button", "text": {"type": "plain_text", "text": "Resolved"}, "action_id": "resolved_button", "value": ticket['id']}
                        ]}
                    ]
                    client.web_client.chat_update(
                        channel=CHANNEL,
                        ts=ticket["message_ts"],
                        blocks=blocks_main,
                        text=(
                            f"*Ticket #{ticket['id']}*\n"
                            f"*Title:* {ticket['title']}\n"
                            f"*Description:* {ticket['description']}\n"
                            f"*Client Name:* {ticket['client_name']}\n"
                            f"*Client App ID:* {ticket['client_app_id']}\n"
                            f"*Product:* {ticket['product']}\n"
                            f"*Endpoint:* `{ticket['endpoint']}`\n"
                            f"*Criticality:* {ticket['criticality']}\n"
                            f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                            f"*Raised By:* <@{ticket['raiser_id']}> ({ticket['raised_by']})\n"
                            f"*Raised At:* {ticket['raised_at']}"
                        )
                    )
                    break
        except Exception as e:
            logging.error(f"Error fetching ticket for triage thread or updating TTT: {e}")
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    if req.type == "interactive" and payload.get("actions", [{}])[0].get("action_id") == "resolved_button":
        ticket_id = payload["actions"][0]["value"]
        user_id = payload["user"]["id"]
        try:
            all_tickets = get_all_tickets()
            for ticket in all_tickets:
                if ticket["id"] == ticket_id:
                    raised_at = parser.isoparse(ticket["raised_at"])
                    now = datetime.now(pytz.timezone("Asia/Kolkata"))
                    ttr_hours = (now - raised_at).total_seconds() / 3600
                    resolver_id = user_id
                    try:
                        resolver_info = client.web_client.users_info(user=resolver_id)
                        resolver_name = resolver_info["user"].get("real_name") or resolver_info["user"].get("profile", {}).get("display_name") or resolver_id
                    except Exception as e:
                        logging.error(f"Error fetching resolver name: {e}")
                        resolver_name = resolver_id
                    update_ticket(ticket_id, {"ttr_hours": f"{ttr_hours:.2f}", "resolved_by": resolver_name, "resolver_id": resolver_id})
                    blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": (
                            f"*Ticket #{ticket['id']}*\n"
                            f"*Title:* {ticket['title']}\n"
                            f"*Description:* {ticket['description']}\n"
                            f"*Client Name:* {ticket['client_name']}\n"
                            f"*Client App ID:* {ticket['client_app_id']}\n"
                            f"*Product:* {ticket['product']}\n"
                            f"*Endpoint:* `{ticket['endpoint']}`\n"
                            f"*Criticality:* {ticket['criticality']}\n"
                            f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                            f"*Raised By:* <@{ticket['raiser_id']}> ({ticket['raised_by']})\n"
                            f"*Raised At:* {ticket['raised_at']}"
                        )}},
                        {"type": "actions", "elements": [
                            {"type": "button", "text": {"type": "plain_text", "text": "Triage"}, "action_id": "triage_button", "value": ticket['id']},
                            {"type": "button", "text": {"type": "plain_text", "text": "✅ Resolved"}, "action_id": "noop", "value": ticket_id, "style": "primary"}
                        ]}
                    ]
                    client.web_client.chat_update(
                        channel=CHANNEL,
                        ts=ticket["message_ts"],
                        blocks=blocks,
                        text=(
                            f"*Ticket #{ticket['id']}*\n"
                            f"*Title:* {ticket['title']}\n"
                            f"*Description:* {ticket['description']}\n"
                            f"*Client Name:* {ticket['client_name']}\n"
                            f"*Client App ID:* {ticket['client_app_id']}\n"
                            f"*Product:* {ticket['product']}\n"
                            f"*Endpoint:* `{ticket['endpoint']}`\n"
                            f"*Criticality:* {ticket['criticality']}\n"
                            f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                            f"*Raised By:* <@{ticket['raiser_id']}> ({ticket['raised_by']})\n"
                            f"*Raised At:* {ticket['raised_at']}"
                        )
                    )
                    summary_blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": (
                            f"*Ticket #{ticket['id']}*\n"
                            f"*Title:* {ticket['title']}\n"
                            f"*Description:* {ticket['description']}\n"
                            f"*Client Name:* {ticket['client_name']}\n"
                            f"*Client App ID:* {ticket['client_app_id']}\n"
                            f"*Product:* {ticket['product']}\n"
                            f"*Endpoint:* `{ticket['endpoint']}`\n"
                            f"*Criticality:* {ticket['criticality']}\n"
                            f"*Assigned To:* <@{ticket['assignee_id']}>\n"
                            f"*Raised By:* <@{ticket['raiser_id']}> ({ticket['raised_by']})\n"
                            f"*Raised At:* {ticket['raised_at']}"
                        )}},
                        {"type": "section", "text": {"type": "mrkdwn", "text": (
                            f"*FRT:* {ticket.get('frt_hours', 'N/A')} hours\n"
                            f"*TTT:* {ticket.get('ttt_hours', 'N/A')} hours\n"
                            f"*TTR:* {ttr_hours:.2f} hours\n"
                            f"*Resolved by:* <@{resolver_id}> ({resolver_name})"
                        )}}
                    ]
                    client.web_client.chat_postMessage(
                        channel="C0958JC4GCE",
                        blocks=summary_blocks,
                        text=(
                            f"Ticket #{ticket['id']} resolved.\n"
                            f"FRT: {ticket.get('frt_hours', 'N/A')} hours, "
                            f"TTT: {ticket.get('ttt_hours', 'N/A')} hours, "
                            f"TTR: {ttr_hours:.2f} hours, "
                            f"Resolved by: {resolver_name}"
                        )
                    )
                    break
        except Exception as e:
            logging.error(f"Error handling resolved button: {e}")
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

@app.get("/")
async def root():
    return {"status": "FRT bot running"}

if __name__ == "__main__":
    import uvicorn
    socket_client.connect()
    uvicorn.run(app, host="0.0.0.0", port=PORT)