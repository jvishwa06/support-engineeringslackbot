import os
import re
import logging
import yaml
from datetime import datetime, timedelta
from fastapi import FastAPI
from slack_sdk.web import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from dotenv import load_dotenv
from dateutil import parser
import pytz
from utils import get_all_tickets, update_ticket, save_ticket, get_escalation_members
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import requests
from jira import JIRA

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
PORT = int(os.getenv("PORT"))
CHANNEL = os.getenv("PRODCHANNEL")
SUMMARY_CHANNEL = os.getenv("SUMMARY_CHANNEL")
FRT_THRESHOLDS = {
    "Critical": float(os.getenv("FRT_HOURS_CRITICAL")),
    "High": float(os.getenv("FRT_HOURS_HIGH")),
    "Medium": float(os.getenv("FRT_HOURS_MEDIUM")),
    "Low": float(os.getenv("FRT_HOURS_LOW"))
}
FRT_ESCALATION_HOURS = float(os.getenv("FRT_ESCALATION_HOURS"))
JIRA_URL = os.getenv("JIRA_URL")
JIRA_USER = os.getenv("JIRA_USER")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")

app = FastAPI()
web_client = WebClient(token=BOT_TOKEN)
socket_client = SocketModeClient(app_token=APP_TOKEN, web_client=web_client)
scheduler = BackgroundScheduler()
scheduler.start()

jira_labels = []
def fetch_jira_labels():
    global jira_labels
    if not (JIRA and JIRA_URL and JIRA_USER and JIRA_TOKEN and JIRA_PROJECT_KEY):
        logging.warning("Jira config or JIRA lib missing, cannot fetch labels.")
        return []
    try:
        jira_client = JIRA(server=JIRA_URL, basic_auth=(JIRA_USER, JIRA_TOKEN))
        issues = jira_client.search_issues(f'project = {JIRA_PROJECT_KEY}', fields="labels", maxResults=1000)
        label_set = set()
        for issue in issues:
            if issue.fields.labels:
                label_set.update(issue.fields.labels)
        jira_labels = sorted(list(label_set))
        logging.info(f"Fetched {len(jira_labels)} Jira labels.")
    except Exception as e:
        logging.error(f"Error fetching Jira labels: {e}")
        jira_labels = []
    return jira_labels

def clean_endpoint(endpoint):
    endpoint = re.sub(r'<([^|>]+)(\|[^>]*)?>', r'\1', endpoint)
    endpoint = re.sub(r':[a-zA-Z0-9_+-]+:', '', endpoint)
    return endpoint.strip()

def is_valid_curl_command(cmd):
    cmd = cmd.strip()
    if not cmd.lower().startswith("curl "):
        return False
    url_pattern = r"https?://[\w\.-]+(:\d+)?(/[\w\.-]*)*"
    return bool(re.search(url_pattern, cmd))

def get_jira_account_id_by_email(jira_client, email):
    try:
        users = jira_client.search_users(query=email)
        logging.info(f"Users found for '{email}': {users}")
        for user in users:
            if hasattr(user, 'emailAddress'):
                if user.emailAddress and user.emailAddress.lower() == email.lower():
                    logging.info(f"Matched by emailAddress: {user.displayName}")
                    return user.accountId
        if users:
            logging.warning(f"Email match not found, falling back to first user: {users[0].displayName}")
            return users[0].accountId
    except Exception as e:
        logging.error(f"Error fetching Jira user for email {email}: {e}")
    return None

def get_slack_user_by_email(email):
    try:
        resp = web_client.users_lookupByEmail(email=email)
        user = resp.get("user", {})
        user_id = user.get("id", "")
        display_name = user.get("real_name") or user.get("profile", {}).get("display_name") or email
        return user_id, display_name
    except Exception as e:
        logging.error(f"Error looking up Slack user for email {email}: {e}")
        return "", email

def send_fr_reminder(ticket):
    if ticket.get('frt_hours'):
        logging.info(f"FRT already set for ticket #{ticket['id']}, skipping FRT reminder.")
        return
    try:
        reminder_text = (f"⏰ <@{ticket['assignee_id']}>, please take a look and respond as soon as possible.")
        web_client.chat_postMessage(channel=CHANNEL,thread_ts=ticket['message_ts'],text=reminder_text)
        logging.info(f"Reminder sent for ticket #{ticket['id']} to <@{ticket['assignee_id']}>.")
    except Exception as e:
        logging.error(f"Error sending FRT reminder: {e}")

def send_escalation_reminder(ticket):
    if ticket.get('frt_hours'):
        logging.info(f"FRT already set for ticket #{ticket['id']}, skipping escalation reminder.")
        return
    try:
        if not ticket.get("frt_hours"):
            product = ticket["product"]
            assignee_email = ticket.get("assignee_email")
            escalation_emails = get_escalation_members(product)
            if assignee_email in escalation_emails:
                idx = escalation_emails.index(assignee_email)
                if idx+1 < len(escalation_emails):
                    next_level_email = escalation_emails[idx+1]
                    user_id, display_name = get_slack_user_by_email(next_level_email)
                    if user_id:
                        reminder_text = f"\U0001F6A8 <@{user_id}>, kindly check this matter. No response received so far."
                    else:
                        reminder_text = f"\U0001F6A8 @{display_name}, kindly check this matter. No response received so far."
                    web_client.chat_postMessage(channel=CHANNEL, thread_ts=ticket['message_ts'], text=reminder_text)
                    logging.info(f"Escalation reminder sent for ticket #{ticket['id']} to {display_name}.")
                else:
                    logging.info(f"No higher escalation level found for ticket #{ticket['id']}.")
            else:
                logging.info(f"Assignee {assignee_email} not found in escalation list for product {product}.")
    except Exception as e:
        logging.error(f"Error sending escalation reminder: {e}")
        return

def create_jira_ticket(summary, issue, fix, priority, resolved, ticket, reporter_email=None):
    if not all([JIRA_URL, JIRA_USER, JIRA_TOKEN, JIRA_PROJECT_KEY]):
        logging.error("Jira configuration missing.")
        return None
    try:
        jira_client = JIRA(server=JIRA_URL, basic_auth=(JIRA_USER, JIRA_TOKEN))
        assignee_email = ticket.get('assignee_email', '')
        assignee_account_id = get_jira_account_id_by_email(jira_client, assignee_email) if assignee_email else None
        if not assignee_account_id:
            logging.warning(f"Assignee Jira account not found for email: {assignee_email}")
        reporter_account_id = None
        if reporter_email:
            reporter_account_id = get_jira_account_id_by_email(jira_client, reporter_email)
            if not reporter_account_id:
                logging.warning(f"Reporter Jira account not found for email: {reporter_email}")
        slack_message_link = f"https://slack.com/app_redirect?channel={CHANNEL}&message_ts={ticket.get('message_ts', '')}"
        endpoint_raw = ticket.get('endpoint', '')
        endpoint_clean = clean_endpoint(endpoint_raw)
        jira_description = (
        f"*Issue*: {issue}\n"
        f"*Fix*: {fix}\n"
        f"*Client Name*: {ticket.get('client_name', '')}\n"
        f"*Client App ID*: {ticket.get('client_app_id', '')}\n"
        f"*Product*: {ticket.get('product', '')}\n"
        f"*Endpoint*: {endpoint_clean}\n"
        f"*Slack Message*: {slack_message_link}"
        )
        labels = ticket.get('labels', [])
        if isinstance(labels, str):
            if labels.startswith("[") and labels.endswith("]"):
                import ast
                try:
                    labels = ast.literal_eval(labels)
                except Exception:
                    labels = [labels.strip("[]' ")]
            elif "," in labels:
                labels = [l.strip() for l in labels.split(',') if l.strip()]
            else:
                labels = [labels.strip()]
        elif not isinstance(labels, list):
            labels = []
        cleaned_labels = []
        for l in labels:
            l = str(l).strip().strip("[]'\"")
            l = l.replace(' ', '-')
            if l:
                cleaned_labels.append(l)
        labels = cleaned_labels
        issue_dict = {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": summary,
            "description": jira_description,
            "issuetype": {"name": "Task"},
            "priority": {"name": priority},
            "labels": labels
        }
        if resolved:
            issue_dict["duedate"] = resolved
        if assignee_account_id:
            issue_dict["assignee"] = {"accountId": assignee_account_id}
        if reporter_account_id:
            issue_dict["reporter"] = {"accountId": reporter_account_id}
        issue = jira_client.create_issue(fields=issue_dict)
        logging.info(f"Issue created: {issue.key}")
        return issue.key
    except Exception as e:
        logging.error(f"Failed to create Jira ticket: {e}")
        return None

def post_ticket_message(ticket):
    try:
        now = datetime.now(pytz.timezone("Asia/Kolkata"))
        formatted_time = now.strftime("%b %d, %Y, %-I:%M %p")
        message_text = (
        f"*Title*: {ticket['title']}\n\n"
        f"*Description*: {ticket['description']}\n\n"
        f"*Client Name*: {ticket['client_name']}\n\n"
        f"*Client App ID*: {ticket['client_app_id']}\n\n"
        f"*Product*: {ticket['product']}\n\n"
        f"*Curl Command*: {ticket['endpoint']}\n\n"
        f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
        f"*Criticality*: {ticket['criticality']}\n\n"
        f"*L0 Testing Done*: {ticket.get('l0_testing', '')}\n\n"
        f"*Assigned To*: <@{ticket['assignee_id']}>\n\n"
        f"*Raised By*: <@{ticket['raiser_id']}>\n\n"
        f"*Raised At*: {formatted_time}\n"
        )
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": message_text}}
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
            blocks.append({"type": "actions","elements": action_elements})
        result = web_client.chat_postMessage(
            channel=CHANNEL,
            blocks=blocks,
            text=message_text
        )
        logging.info(f"Ticket #{ticket['id']} posted to Slack with ts={result.get('ts')}")
        ticket['message_ts'] = result.get('ts')

        ticket['slack_message_link'] = f"https://slack.com/app_redirect?channel={CHANNEL}&message_ts={ticket['message_ts']}"

        frt_value = FRT_THRESHOLDS.get(ticket['criticality'], '')
        if frt_value:
            frt_text = f"FRT for the {ticket['criticality']} is {frt_value} hrs 🟡⏳"
            web_client.chat_postMessage(channel=CHANNEL, thread_ts=ticket['message_ts'], text=frt_text)

        if not ticket.get('frt_hours'):
            frt_hours = FRT_THRESHOLDS[ticket['criticality']]
            half_frt = max(frt_hours / 2, 0.0167)
            reminder_time = now + timedelta(hours=half_frt)
            scheduler.add_job(send_fr_reminder,trigger=DateTrigger(run_date=reminder_time),args=[ticket],id=f"reminder_{ticket['id']}")
            logging.info(f"Scheduled FRT reminder for ticket #{ticket['id']} at {reminder_time}")

            escalation_time = now + timedelta(hours=frt_hours - FRT_ESCALATION_HOURS)
            scheduler.add_job(send_escalation_reminder,trigger=DateTrigger(run_date=escalation_time),args=[ticket],id=f"escalation_{ticket['id']}")
            logging.info(f"Scheduled escalation reminder for ticket #{ticket['id']} at {escalation_time}")
        else:
            logging.info(f"FRT already set for ticket #{ticket['id']}, not scheduling reminders.")
        return ticket['message_ts']
    except Exception as e:
        logging.error(f"Error posting message to Slack: {e}")
        return None


@socket_client.socket_mode_request_listeners.append
def handle_events(client: SocketModeClient, req: SocketModeRequest):
    payload = req.payload
    if req.type == "slash_commands" and payload.get("command") == "/querybot":
        logging.info("Received /querybot command")
        product_options = []
        try:
            with open("EMatrix.yml", "r") as f:
                data = yaml.safe_load(f)
                for entry in data:
                    product_options.append({"text": {"type": "plain_text", "text": entry["product"]}, "value": entry["product"]})
        except Exception as e:
            logging.error(f"Error reading EMatrix.yml: {e}")
        if not product_options:
            product_options.append({"text": {"type": "plain_text", "text": "No products available"}, "value": "no_product"})

        client.web_client.views_open(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "submit_ticket",
                "title": {"type": "plain_text", "text": "Raise Query"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "blocks": [
                    {"type": "input", "block_id": "title", "label": {"type": "plain_text", "text": "Title"}, "element": {"type": "plain_text_input", "action_id": "value"}},
                    {
                        "type": "input",
                        "block_id": "criticality",
                        "label": {"type": "plain_text", "text": "Priority"},
                        "element": {
                            "type": "static_select",
                            "action_id": "value",
                            "options": [
                                {"text": {"type": "plain_text", "text": "Critical"}, "value": "Critical"},
                                {"text": {"type": "plain_text", "text": "High"}, "value": "High"},
                                {"text": {"type": "plain_text", "text": "Medium"}, "value": "Medium"},
                                {"text": {"type": "plain_text", "text": "Low"}, "value": "Low"},
                            ]
                        }
                    },
                    {"type": "input", "block_id": "description", "label": {"type": "plain_text", "text": "Description"}, "element": {"type": "rich_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "client_name", "label": {"type": "plain_text", "text": "Client Name"}, "element": {"type": "plain_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "client_app_id", "label": {"type": "plain_text", "text": "Client App ID"}, "element": {"type": "plain_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "product", "label": {"type": "plain_text", "text": "Product"}, "element": {"type": "static_select", "action_id": "value", "options": product_options}},
                    {"type": "input", "block_id": "endpoint", "label": {"type": "plain_text", "text": "Curl Command"}, "element": {"type": "rich_text_input", "action_id": "value"}, "optional": True},
                    {"type": "input", "block_id": "kibana_link", "label": {"type": "plain_text", "text": "Kibana Link"}, "element": {"type": "plain_text_input", "action_id": "value"}, "optional": True},
                    {"type": "input", "block_id": "assignee", "label": {"type": "plain_text", "text": "Assign To"}, "element": {"type": "users_select", "action_id": "value"}},
                    {
                        "type": "input",
                        "block_id": "l0_testing",
                        "label": {"type": "plain_text", "text": "Was L0 testing/debugging done already?"},
                        "element": {
                            "type": "static_select",
                            "action_id": "value",
                            "options": [
                                {"text": {"type": "plain_text", "text": "Yes"}, "value": "Yes"},
                                {"text": {"type": "plain_text", "text": "No"}, "value": "No"}
                            ]
                        }
                    }
                ]
            }
        )
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    if payload.get("type") == "view_submission" and payload["view"]["callback_id"] == "submit_ticket":
        state = payload["view"]["state"]["values"]
        endpoint_block = state["endpoint"]["value"]
        endpoint = ""
        if endpoint_block:
            if "rich_text_value" in endpoint_block:
                endpoint_raw = endpoint_block["rich_text_value"]
            elif "value" in endpoint_block:
                endpoint_raw = endpoint_block["value"]
            else:
                endpoint_raw = ""
            def extract_plain_text_from_rich_text(rich_text):
                if isinstance(rich_text, str):
                    return rich_text
                if isinstance(rich_text, dict) and rich_text.get('type') == 'rich_text':
                    text = ''
                    for section in rich_text.get('elements', []):
                        if section.get('type') == 'rich_text_section':
                            for el in section.get('elements', []):
                                if el.get('type') == 'text':
                                    text += el.get('text', '')
                                elif el.get('type') == 'emoji':
                                    text += f":{el.get('name', '')}:"
                                elif el.get('type') == 'link':
                                    url = el.get('url', '')
                                    text += url
                        elif section.get('type') == 'text':
                            text += section.get('text', '')
                    return text
                return str(rich_text)
            endpoint = extract_plain_text_from_rich_text(endpoint_raw).strip()

        if endpoint:
            if not is_valid_curl_command(endpoint):
                client.send_socket_mode_response(SocketModeResponse(
                    envelope_id=req.envelope_id,
                    payload={
                        "response_action": "errors",
                        "errors": {
                            "endpoint": "Please enter a valid curl command starting with 'curl' and containing a URL."
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
            raised_by_email = raised_by_info["user"].get("profile", {}).get("email", "")
        except Exception as e:
            logging.error(f"Error fetching raised_by name/email: {e}")
            raised_by_name = user_id
            raised_by_email = ""
        try:
            assignee_id = state["assignee"]["value"]["selected_user"]
            assigned_to_info = client.web_client.users_info(user=assignee_id)
            assigned_to_name = assigned_to_info["user"].get("real_name") or assigned_to_info["user"].get("profile", {}).get("display_name") or assignee_id
            assigned_to_email = assigned_to_info["user"].get("profile", {}).get("email", "")
        except Exception as e:
            logging.error(f"Error fetching assigned_to name/email: {e}")
            assigned_to_name = assignee_id
            assigned_to_email = ""
        description_block = state["description"]["value"]
        if "rich_text_value" in description_block:
            description_raw = description_block["rich_text_value"]
        elif "value" in description_block:
            description_raw = description_block["value"]
        else:
            description_raw = ""
        def extract_plain_text_from_rich_text(rich_text):
            if isinstance(rich_text, str):
                return rich_text
            if isinstance(rich_text, dict) and rich_text.get('type') == 'rich_text':
                text = ''
                for section in rich_text.get('elements', []):
                    if section.get('type') == 'rich_text_section':
                        for el in section.get('elements', []):
                            if el.get('type') == 'text':
                                text += el.get('text', '')
                            elif el.get('type') == 'emoji':
                                text += f":{el.get('name', '')}:"
                            elif el.get('type') == 'link':
                                url = el.get('url', '')
                                text += url
                    elif section.get('type') == 'text':
                        text += section.get('text', '')
                return text
            return str(rich_text)
        description = extract_plain_text_from_rich_text(description_raw)

        ticket = {}
        kibana_link_value = ""
        try:
            kibana_link_block = state["kibana_link"]["value"]
            if "value" in kibana_link_block:
                kibana_link_value = kibana_link_block["value"]
            else:
                kibana_link_value = ""
        except Exception:
            kibana_link_value = ""

        for field in [
            "id","title","description","raised_by","raiser_id","raised_by_email","raised_at","client_name","client_app_id","criticality","product","endpoint","kibana_link","assigned_to","assignee_id","assignee_email","frt_hours","message_ts",
            "ttt_hours","triaged_by","triaged_id","triage_ts","ttr_hours","resolved_by","resolver_id","resolve_at","escalate_to","escalate_id", "priority", "summary", "fix", "issue", "labels", "l0_testing"
        ]:
            if field == "id":
                ticket[field] = ticket_id
            elif field == "title":
                ticket[field] = state["title"]["value"]["value"]
            elif field == "description":
                ticket[field] = description
            elif field == "raised_by":
                ticket[field] = raised_by_name
            elif field == "raiser_id":
                ticket[field] = user_id
            elif field == "raised_by_email":
                ticket[field] = raised_by_email
            elif field == "raised_at":
                ticket[field] = raised_at
            elif field == "client_name":
                ticket[field] = state["client_name"]["value"]["value"]
            elif field == "client_app_id":
                ticket[field] = state["client_app_id"]["value"]["value"]
            elif field == "criticality":
                ticket[field] = state["criticality"]["value"]["selected_option"]["value"]
            elif field == "product":
                ticket[field] = state["product"]["value"]["selected_option"]["value"]
            elif field == "endpoint":
                ticket[field] = endpoint
            elif field == "kibana_link":
                ticket[field] = kibana_link_value
            elif field == "assigned_to":
                ticket[field] = assigned_to_name
            elif field == "assignee_id":
                ticket[field] = assignee_id
            elif field == "assignee_email":
                ticket[field] = assigned_to_email
            elif field == "frt_hours":
                ticket[field] = ""
            elif field == "message_ts":
                ticket[field] = ""
            elif field == "l0_testing":
                ticket[field] = state["l0_testing"]["value"]["selected_option"]["value"]
            else:
                ticket[field] = ""
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
                        responder_email = user_info["user"].get("profile", {}).get("email", "")
                        responder_name = user_info["user"].get("real_name") or user_info["user"].get("profile", {}).get("display_name") or user
                    except Exception as e:
                        logging.error(f"Error fetching responder name/email: {e}")
                        responder_name = user
                        responder_email = ""
                    escalation_emails = get_escalation_members(ticket["product"])
                    assignee_id = ticket.get("assignee_id")
                    is_assignee = (user == assignee_id)
                    if (responder_email in escalation_emails or is_assignee) and not ticket.get("frt_hours"):
                        raised_at = parser.isoparse(ticket["raised_at"])
                        now = datetime.now(pytz.timezone("Asia/Kolkata"))
                        frt = (now - raised_at).total_seconds() / 3600
                        update_ticket(ticket["id"], {"frt_hours": f"{frt:.2f}"})
                        logging.info(f"✅ FRT for ticket #{ticket['id']} set to {frt:.2f} hours by {responder_email} ({responder_name})")
                        try:
                            scheduler.remove_job(f"reminder_{ticket['id']}")
                        except Exception as e:
                            logging.info(f"No FRT reminder job to remove for ticket #{ticket['id']}: {e}")
                        try:
                            scheduler.remove_job(f"escalation_{ticket['id']}")
                        except Exception as e:
                            logging.info(f"No escalation reminder job to remove for ticket #{ticket['id']}: {e}")
                        blocks = [
                            {"type": "section", "text": {"type": "mrkdwn", "text": (
                            f"*Title*: {ticket['title']}\n\n"
                            f"*Description*: {ticket['description']}\n\n"
                            f"*Client Name*: {ticket['client_name']}\n\n"
                            f"*Client App ID*: {ticket['client_app_id']}\n\n"
                            f"*Product*: {ticket['product']}\n\n"
                            f"*Curl Command*: {ticket['endpoint']}\n\n"
                            f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
                            f"*Criticality*: {ticket['criticality']}\n\n"
                            f"*L0 Testing Done*: {ticket.get('l0_testing', '')}\n\n"
                            f"*Assigned To*: <@{ticket['assignee_id']}>\n\n"
                            f"*Raised By*: <@{ticket['raiser_id']}>\n\n"
                            f"*Raised At*: {ticket['raised_at']}\n"
                            )}},
                            {"type": "actions", "elements": [
                                {"type": "button", "text": {"type": "plain_text", "text": "Triage"}, "action_id": "triage_button", "value": ticket['id']}
                            ]}
                        ]
                        raised_by_display = ticket.get('raised_by', ticket.get('raiser_id', ''))
                        formatted_raised_at = ''
                        if ticket.get('raised_at'):
                            try:
                                dt = parser.isoparse(ticket['raised_at'])
                                formatted_raised_at = dt.strftime('%b %d, %Y, %-I:%M %p')
                            except Exception as e:
                                logging.error(f"Error formatting raised_at: {e}")
                                formatted_raised_at = ticket['raised_at']
                        blocks = [
                            {"type": "section", "text": {"type": "mrkdwn", "text": (
                            f"*Title*: {ticket['title']}\n\n"
                            f"*Description*: {ticket['description']}\n\n"
                            f"*Client Name*: {ticket['client_name']}\n\n"
                            f"*Client App ID*: {ticket['client_app_id']}\n\n"
                            f"*Product*: {ticket['product']}\n\n"
                            f"*Curl Command*: {ticket['endpoint']}\n\n"
                            f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
                            f"*Criticality*: {ticket['criticality']}\n\n"
                            f"*L0 Testing Done*: {ticket.get('l0_testing', '')}\n\n"
                            f"*Assigned To*: <@{ticket['assignee_id']}>\n\n"
                            f"*Raised By*: <@{ticket['raiser_id']}>\n\n"
                            f"*Raised At*: {formatted_raised_at}\n"
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
                                f"*Title*: {ticket['title']}\n\n"
                                f"*Description*: {ticket['description']}\n\n"
                                f"*Client Name*: {ticket['client_name']}\n\n"
                                f"*Client App ID*: {ticket['client_app_id']}\n\n"
                                f"*Product*: {ticket['product']}\n\n"
                                f"*Curl Command*: {ticket['endpoint']}\n\n"
                                f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
                                f"*Criticality*: {ticket['criticality']}\n\n"
                                f"*L0 Testing Done*: {ticket.get('l0_testing', '')}\n\n"
                                f"*Assigned To*: <@{ticket['assignee_id']}>\n\n"
                                f"*Raised By*: <@{ticket['raiser_id']}>\n\n"
                                f"*Raised At*: {ticket['raised_at']}\n"
                            )
                        )
                        return
                    else:
                        logging.info(f"User {responder_email} ({responder_name}) is not an escalation member or assignee for ticket #{ticket['id']}. FRT will not be set.")
                        return
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    if req.type == "interactive" and payload.get("actions", [{}])[0].get("action_id") == "triage_button":
        ticket_id = payload["actions"][0]["value"]
        fetch_jira_labels()
        label_options = []
        for label in jira_labels:
            label_options.append({
                "text": {"type": "plain_text", "text": label},
                "value": label
            })
        if not label_options:
            label_options.append({
                "text": {"type": "plain_text", "text": "No labels available"},
                "value": "no_label"
            })
        client.web_client.views_open(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "triage_submit",
                "title": {"type": "plain_text", "text": "Jira Ticket"},
                "submit": {"type": "plain_text", "text": "Submit"},
                "blocks": [
                    {"type": "input", "block_id": "summary", "label": {"type": "plain_text", "text": "Summary"}, "element": {"type": "plain_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "issue", "label": {"type": "plain_text", "text": "What's the issue"}, "element": {"type": "rich_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "fix", "label": {"type": "plain_text", "text": "What's the fix"}, "element": {"type": "rich_text_input", "action_id": "value"}},
                    {"type": "input", "block_id": "priority", "label": {"type": "plain_text", "text": "Priority"}, "element": {"type": "static_select", "action_id": "value", "options": [
                        {"text": {"type": "plain_text", "text": "Critical"}, "value": "Critical"},
                        {"text": {"type": "plain_text", "text": "High"}, "value": "High"},
                        {"text": {"type": "plain_text", "text": "Medium"}, "value": "Medium"},
                        {"text": {"type": "plain_text", "text": "Low"}, "value": "Low"},
                        {"text": {"type": "plain_text", "text": "None"}, "value": "None"}
                    ]}},
                    {"type": "input", "block_id": "resolved_date", "label": {"type": "plain_text", "text": "Expected Date of resolution"}, "element": {"type": "datepicker", "action_id": "value", "placeholder": {"type": "plain_text", "text": "Select a date"}}},
                    {"type": "input", "block_id": "escalate_to", "label": {"type": "plain_text", "text": "Assignee"}, "element": {"type": "users_select", "action_id": "value"}},
                    {"type": "input", "block_id": "labels", "label": {"type": "plain_text", "text": "Labels (select)"}, "element": {
                        "type": "multi_static_select",
                        "action_id": "value",
                        "options": label_options,
                        "placeholder": {"type": "plain_text", "text": "Select labels"}
                    }, "optional": True},
                    {"type": "input", "block_id": "new_labels", "label": {"type": "plain_text", "text": "Add new label(s) (comma separated)"}, "element": {"type": "plain_text_input", "action_id": "value"}, "optional": True}
                ],
                "private_metadata": ticket_id
            }
        )
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    if payload.get("type") == "view_submission" and payload["view"]["callback_id"] == "triage_submit":
        state = payload["view"]["state"]["values"]
        ticket_id = payload["view"].get("private_metadata")
        summary = state["summary"]["value"]["value"]
        def extract_plain_text_from_rich_text(rich_text):
            if isinstance(rich_text, str):
                return rich_text
            if isinstance(rich_text, dict) and rich_text.get('type') == 'rich_text':
                text = ''
                for section in rich_text.get('elements', []):
                    if section.get('type') == 'rich_text_section':
                        for el in section.get('elements', []):
                            if el.get('type') == 'text':
                                text += el.get('text', '')
                            elif el.get('type') == 'emoji':
                                text += f":{el.get('name', '')}:"
                            elif el.get('type') == 'link':
                                url = el.get('url', '')
                                text += url
                    elif section.get('type') == 'text':
                        text += section.get('text', '')
                return text
            return str(rich_text)
        issue_block = state["issue"]["value"]
        if "rich_text_value" in issue_block:
            issue_raw = issue_block["rich_text_value"]
        elif "value" in issue_block:
            issue_raw = issue_block["value"]
        else:
            issue_raw = ""
        issue = extract_plain_text_from_rich_text(issue_raw)
        fix_block = state["fix"]["value"]
        if "rich_text_value" in fix_block:
            fix_raw = fix_block["rich_text_value"]
        elif "value" in fix_block:
            fix_raw = fix_block["value"]
        else:
            fix_raw = ""
        fix = extract_plain_text_from_rich_text(fix_raw)
        priority = state["priority"]["value"]["selected_option"]["value"]
        resolved_date = state["resolved_date"]["value"].get("selected_date", "")
        formatted_resolved_date = ""
        if resolved_date:
            try:
                dt = datetime.strptime(resolved_date, "%Y-%m-%d")
                formatted_resolved_date = dt.strftime("%b %d, %Y")
            except Exception as e:
                logging.error(f"Error formatting resolved_date: {e}")
                formatted_resolved_date = resolved_date
        else:
            formatted_resolved_date = resolved_date
        escalate_to = state.get("escalate_to", {}).get("value", {}).get("selected_user", "")
        labels = []
        labels_block = state.get("labels", {}).get("value", {})
        if "selected_options" in labels_block:
            labels = [opt["value"] for opt in labels_block["selected_options"]]
        new_labels_raw = state.get("new_labels", {}).get("value", {}).get("value", "")
        if new_labels_raw:
            new_labels = [l.strip() for l in new_labels_raw.split(",") if l.strip()]
            labels.extend(new_labels)
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
                    escalate_to_email = ""
                    if escalate_to:
                        try:
                            escalate_to_info = client.web_client.users_info(user=escalate_to)
                            escalate_to_name = escalate_to_info["user"].get("real_name") or escalate_to_info["user"].get("profile", {}).get("display_name") or escalate_to
                            escalate_to_email = escalate_to_info["user"].get("profile", {}).get("email", "")
                        except Exception as e:
                            logging.error(f"Error fetching escalate_to name/email: {e}")
                            escalate_to_name = escalate_to
                            escalate_to_email = ""
                    update_dict = {
                        "ttt_hours": f"{ttt_hours:.2f}",
                        "triaged_by": triaged_by_name,
                        "triaged_id": triaged_by_id,
                        "resolve_at": resolved_date,
                        "priority": priority,
                        "summary": summary,
                        "issue": issue,
                        "fix": fix,
                        "labels": labels,
                        "assigned_to": escalate_to_name,
                        "assignee_id": escalate_to_id,
                        "assignee_email": escalate_to_email
                    }
                    if escalate_to:
                        update_dict["escalate_to"] = escalate_to_name
                        update_dict["escalate_id"] = escalate_to_id
                    triage_msg = client.web_client.chat_postMessage(
                        channel=CHANNEL,
                        thread_ts=thread_ts,
                        text=(
                        f"*Summary*: {summary}\n"
                        f"*Issue*: {issue}\n"
                        f"*Fix*: {fix}\n"
                        f"*Priority*: {priority}\n"
                        f"*Date of resolution*: {formatted_resolved_date}\n"
                        f"*Triaged by*: <@{triaged_by_id}>\n"
                        f"*Assignee*: <@{escalate_to_id}>"
                        ),
                        blocks=[
                            {"type": "section", "text": {"type": "mrkdwn", "text": (
                            f"*Summary*: {summary}\n\n"
                            f"*Issue*: {issue}\n\n"
                            f"*Fix*: {fix}\n\n"
                            f"*Priority*: {priority}\n\n"
                            f"*Date of resolution*: {formatted_resolved_date}\n\n"
                            f"*Triaged by*: <@{triaged_by_id}>\n\n"
                            f"*Assignee*: <@{escalate_to_id}>")}},
                            {"type": "actions", "elements": [
                                {"type": "button", "text": {"type": "plain_text", "text": "Create Jira Ticket"}, "action_id": "create_jira_button", "value": ticket_id}
                            ]}
                        ]
                    )
                    update_dict["triage_ts"] = triage_msg.get("ts")
                    update_ticket(ticket_id, update_dict)
                    formatted_raised_at = datetime.strptime(ticket["raised_at"], "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%b %d, %Y, %-I:%M %p") if ticket.get('raised_at') else ""
                    blocks_main = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": (
                        f"*Title*: {ticket['title']}\n\n"
                        f"*Description*: {ticket['description']}\n\n"
                        f"*Client Name*: {ticket['client_name']}\n\n"
                        f"*Client App ID*: {ticket['client_app_id']}\n\n"
                        f"*Product*: {ticket['product']}\n\n"
                        f"*Curl Command*: {ticket['endpoint']}\n\n"
                        f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
                        f"*Criticality*: {ticket['criticality']}\n\n"
                        f"*L0 Testing Done:* {ticket.get('l0_testing', '')}\n\n"
                        f"*Assigned To*: <@{escalate_to_id}>\n\n"
                        f"*Raised By*: <@{ticket['raiser_id']}>\n\n"
                        f"*Raised At*: {formatted_raised_at}"
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
                        f"*Title*: {ticket['title']}\n\n"
                        f"*Description*: {ticket['description']}\n\n"
                        f"*Client Name*: {ticket['client_name']}\n\n"
                        f"*Client App ID*: {ticket['client_app_id']}\n\n"
                        f"*Product*: {ticket['product']}\n\n"
                        f"*Curl Command*: {ticket['endpoint']}\n\n"
                        f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
                        f"*Criticality*: {ticket['criticality']}\n\n"
                        f"*L0 Testing Done*: {ticket.get('l0_testing', '')}\n\n"
                        f"*Assigned To*: <@{escalate_to_id}>\n\n"
                        f"*Raised By*: <@{ticket['raiser_id']}>\n\n"
                        f"*Raised At*: {formatted_raised_at}\n"
                        )
                    )
                    break
        except Exception as e:
            logging.error(f"Error fetching ticket for triage thread or updating TTT: {e}")
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    if req.type == "interactive" and payload.get("actions", [{}])[0].get("action_id") == "create_jira_button":
        ticket_id = payload["actions"][0]["value"]
        user_id = payload["user"]["id"]
        try:
            all_tickets = get_all_tickets()
            ticket = next((t for t in all_tickets if t["id"] == ticket_id), None)
            if ticket:
                summary = ticket.get("summary", "")
                issue = ticket.get("issue", "")
                fix = ticket.get("fix", "")
                priority = ticket.get("priority", "")
                resolved = ticket.get("resolve_at", "")
                formatted_resolved_date = resolved
                if resolved:
                    try:
                        dt = datetime.strptime(resolved, "%Y-%m-%d")
                        formatted_resolved_date = dt.strftime("%b %d, %Y")
                    except Exception:
                        formatted_resolved_date = resolved
                triaged_by_name = ticket.get("triaged_by", "")
                jira_issue_key = create_jira_ticket(summary, issue, fix, priority, resolved, ticket, reporter_email=ticket.get("raised_by_email", ""))
                if jira_issue_key:
                    jira_link = f"{JIRA_URL}/browse/{jira_issue_key}"
                    blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": (
                        f"*Summary*: {summary}\n"
                        f"*Issue*: {issue}\n"
                        f"*Fix*: {fix}\n"
                        f"*Priority*: {priority}\n"
                        f"*Date of resolution*: {formatted_resolved_date}\n"
                        f"*Triaged by*: <@{ticket.get('triaged_id', user_id)}>\n"
                        f"*Assignee*: <@{ticket['assignee_id']}>"
                        )}},
                        {"type": "context", "elements": [
                            {"type": "mrkdwn", "text": f":white_check_mark: Jira ticket created: <{jira_link}|{jira_issue_key}>"}
                        ]}
                    ]
                    client.web_client.chat_update(
                        channel=CHANNEL,
                        ts=ticket.get("triage_ts"),
                        blocks=blocks,
                        text=(
                        f"*Summary*: {summary}\n\n"
                        f"*Issue*: {issue}\n\n"
                        f"*Fix*: {fix}\n\n"
                        f"*Priority*: {priority}\n\n"
                        f"*Date of resolution*: {formatted_resolved_date}\n\n"
                        f"*Triaged by*: <@{ticket.get('triaged_id', user_id)}>\n\n"
                        f"*Assignee*: <@{ticket['assignee_id']}>\n\n"
                        f"Jira ticket created: {jira_link}"
                        )
                    )
        except Exception as e:
            logging.error(f"Error handling create_jira_button: {e}")
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

    if req.type == "interactive" and payload.get("actions", [{}])[0].get("action_id") == "resolved_button":
        ticket_id = payload["actions"][0]["value"]
        user_id = payload["user"]["id"]
        try:
            all_tickets = get_all_tickets()
            for ticket in all_tickets:
                if ticket["id"] == ticket_id:
                    now = datetime.now(pytz.timezone("Asia/Kolkata"))
                    resolved_by_info = client.web_client.users_info(user=user_id)
                    resolved_by_name = resolved_by_info["user"].get("real_name") or resolved_by_info["user"].get("profile", {}).get("display_name") or user_id
                    update_dict = {
                        "resolved_by": resolved_by_name,
                        "resolver_id": user_id,
                        "resolve_at": now.strftime("%Y-%m-%d"),
                        "ttr_hours": ""
                    }
                    if ticket.get("raised_at"):
                        try:
                            raised_at = parser.isoparse(ticket["raised_at"])
                            ttr_hours = (now - raised_at).total_seconds() / 3600
                            update_dict["ttr_hours"] = f"{ttr_hours:.2f}"
                        except Exception as e:
                            logging.error(f"Error calculating TTR: {e}")
                    update_ticket(ticket_id, update_dict)
                    jira_issue_key = None
                    jira_link = None
                    if ticket.get("summary") and JIRA_URL and JIRA_USER and JIRA_TOKEN:
                        try:
                            search_url = f"{JIRA_URL}/rest/api/2/search"
                            jql = f'summary ~ "{ticket["summary"]}" AND project = {JIRA_PROJECT_KEY} order by created DESC'
                            resp = requests.get(search_url, params={"jql": jql}, auth=(JIRA_USER, JIRA_TOKEN), headers={"Content-Type": "application/json"})
                            if resp.status_code == 200:
                                issues = resp.json().get("issues", [])
                                if issues:
                                    jira_issue_key = issues[0]["key"]
                                    jira_link = f"{JIRA_URL}/browse/{jira_issue_key}"
                                    transitions_url = f"{JIRA_URL}/rest/api/2/issue/{jira_issue_key}/transitions"
                                    transitions_resp = requests.get(transitions_url, auth=(JIRA_USER, JIRA_TOKEN), headers={"Content-Type": "application/json"})
                                    if transitions_resp.status_code == 200:
                                        transitions = transitions_resp.json().get("transitions", [])
                                        done_transition = next((t for t in transitions if t["name"].lower() == "done"), None)
                                        if done_transition:
                                            transition_id = done_transition["id"]
                                            transition_payload = {"transition": {"id": transition_id}}
                                            trans_resp = requests.post(transitions_url, json=transition_payload, auth=(JIRA_USER, JIRA_TOKEN), headers={"Content-Type": "application/json"})
                                            if trans_resp.status_code == 204:
                                                logging.info(f"Jira issue {jira_issue_key} transitioned to Done.")
                                            else:
                                                logging.error(f"Failed to transition Jira issue: {trans_resp.text}")
                                        else:
                                            logging.error("No 'Done' transition found for Jira issue.")
                                    else:
                                        logging.error(f"Failed to get Jira transitions: {transitions_resp.text}")
                                else:
                                    logging.error("No Jira issue found for summary.")
                            else:
                                logging.error(f"Failed to search Jira issue: {resp.text}")
                        except Exception as e:
                            logging.error(f"Error updating Jira status: {e}")
                    formatted_raised_at = datetime.strptime(ticket["raised_at"], "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%b %d, %Y, %-I:%M %p") if ticket.get('raised_at') else ""
                    blocks = [
                        {"type": "section", "text": {"type": "mrkdwn", "text": (
                        f"*Title*: {ticket['title']}\n\n"
                        f"*Description*: {ticket['description']}\n\n"
                        f"*Client Name*: {ticket['client_name']}\n\n"
                        f"*Client App ID*: {ticket['client_app_id']}\n\n"
                        f"*Product*: {ticket['product']}\n\n"
                        f"*Curl Command*: {ticket['endpoint']}\n\n"
                        f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
                        f"*Criticality*: {ticket['criticality']}\n\n"
                        f"*L0 Testing Done*: {ticket.get('l0_testing', '')}\n\n"
                        f"*Assigned To*: <@{ticket['assignee_id']}>\n\n"
                        f"*Raised By*: <@{ticket['raiser_id']}>\n\n"
                        f"*Raised At*: {formatted_raised_at}\n\n"
                        f"*Resolved By*: <@{user_id}>\n\n"
                        f"*Resolved At*: {now.strftime('%b %d, %Y, %-I:%M %p')}"
                        )}},
                        {"type": "context", "elements": [
                            {"type": "mrkdwn", "text": "✅ Issue has been resolved"}
                        ]}
                    ]
                    client.web_client.chat_update(
                        channel=CHANNEL,
                        ts=ticket["message_ts"],
                        blocks=blocks,
                        text = (
                            f"*Title*: {ticket['title']}\n\n"
                            f"*Description*: {ticket['description']}\n\n"
                            f"*Client Name*: {ticket['client_name']}\n\n"
                            f"*Client App ID*: {ticket['client_app_id']}\n\n"
                            f"*Product*: {ticket['product']}\n\n"
                            f"*Curl Command*: {ticket['endpoint']}\n\n"
                            f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
                            f"*Criticality*: {ticket['criticality']}\n\n"
                            f"*L0 Testing Done*: {ticket.get('l0_testing', '')}\n\n"
                            f"*Assigned To*: <@{ticket['assignee_id']}>\n\n"
                            f"*Raised By*: <@{ticket['raiser_id']}>\n\n"
                            f"*Raised At*: {formatted_raised_at}\n\n"
                            f"*Resolved By*: <@{user_id}>\n\n"
                            f"*Resolved At*: {now.strftime('%b %d, %Y, %-I:%M %p')}"
                        )
                    )
                    client.web_client.chat_postMessage(channel=CHANNEL,thread_ts=ticket["message_ts"],text=f"Ticket marked as resolved by <@{user_id}>.")
                    def get_display_name(user_id):
                        try:
                            info = client.web_client.users_info(user=user_id)
                            return info["user"].get("real_name") or info["user"].get("profile", {}).get("display_name") or user_id
                        except Exception:
                            return user_id
                    raised_by_name = get_display_name(ticket.get("raiser_id", ""))
                    assigned_to_name = get_display_name(ticket.get("assignee_id", ""))
                    triaged_by_name = get_display_name(ticket.get("triaged_id", ""))
                    resolved_by_name = get_display_name(user_id)
                    formatted_raised_at = datetime.strptime(ticket["raised_at"], "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%b %d, %Y, %-I:%M %p") if ticket.get('raised_at') else ""
                    updated_tickets = get_all_tickets()
                    updated_ticket = next((t for t in updated_tickets if t["id"] == ticket_id), ticket)
                    ttr_hours_val = updated_ticket.get("ttr_hours", "")
                    summary_text = (
                    f"*Title*: {ticket['title']}\n"
                    f"*Description*: {ticket['description']}\n"
                    f"*Client Name*: {ticket['client_name']}\n"
                    f"*Client App ID*: {ticket['client_app_id']}\n"
                    f"*Product*: {ticket['product']}\n"
                    f"*Curl Command*: {ticket['endpoint']}\n"
                    f"*Kibana Link*: {ticket.get('kibana_link', '')}\n\n"
                    f"*Criticality*: {ticket['criticality']}\n"
                    f"*L0 Testing Done*: {ticket.get('l0_testing', '')}\n\n"
                    f"*Assigned To*: <@{ticket.get('assignee_id', '')}>\n"
                    f"*Raised By*: <@{ticket.get('raiser_id', '')}>\n"
                    f"*Raised At*: {formatted_raised_at}\n"
                    f"*Slack Message*: {ticket.get('slack_message_link', '')}\n"
                    f"*Resolved By*: <@{user_id}>\n"
                    f"*Resolved At*: {ticket.get('resolve_at', '')}\n"
                    f"{'_' * 80}\n"
                    f"*Summary*: {ticket.get('summary', '')}\n"
                    f"*Issue*: {ticket.get('issue', '')}\n"
                    f"*Fix*: {ticket.get('fix', '')}\n"
                    f"*Priority*: {ticket.get('priority', '')}\n"
                    f"*Triaged by*: <@{ticket.get('triaged_id', '')}>\n"
                    f"*Assignee*: <@{ticket.get('assignee_id', '')}>\n"
                    f"*Jira Ticket*: <{jira_link}|{jira_issue_key}>\n"
                    f"{'_' * 80}\n"
                    f"*FRT*: {ticket.get('frt_hours', '')} hours\n"
                    f"*TTT*: {ticket.get('ttt_hours', '')} hours\n"
                    f"*TTR*: {ttr_hours_val} hours"
                    )
                    client.web_client.chat_postMessage(channel=SUMMARY_CHANNEL,text=summary_text)
                    break
        except Exception as e:
            logging.error(f"Error handling resolved_button: {e}")
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        return

@app.get("/")
async def root():
    return {"status": "FRT bot running"}

if __name__ == "__main__":
    import uvicorn
    socket_client.connect()
    uvicorn.run(app, host="0.0.0.0", port=PORT)