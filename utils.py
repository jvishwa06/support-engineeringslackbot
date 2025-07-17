import csv
import os
import logging
from slack_sdk.errors import SlackApiError

TICKET_CSV = "tickets.csv"

FIELDNAMES = [
    "id","title","description","raised_by","raiser_id","raised_at","client_name","client_app_id","criticality","product","endpoint","assigned_to","assignee_id","frt_hours","message_ts",
    "ttt_hours","triaged_by","triaged_id","triage_ts","ttr_hours","resolved_by","resolver_id","resolve_at","escalate_to","escalate_id", "priority", "summary", "fix", "issue", "labels"
]

def save_ticket(ticket: dict):
    try:
        is_new = not os.path.isfile(TICKET_CSV)
        for field in FIELDNAMES:
            if field not in ticket:
                ticket[field] = ""
        with open(TICKET_CSV, mode="a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if is_new:
                writer.writeheader()
            writer.writerow(ticket)
        logging.info(f"Ticket saved: {ticket}")
    except Exception as e:
        logging.error(f"Failed to save ticket to CSV: {e}. Ticket data: {ticket}")

def update_ticket(ticket_id: str, updates: dict):
    try:
        tickets = get_all_tickets()
        updated = False
        for ticket in tickets:
            if ticket["id"] == ticket_id:
                ticket.update(updates)
                updated = True
                break
        if updated:
            with open(TICKET_CSV, mode="w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerows(tickets)
        else:
            logging.warning(f"Ticket ID {ticket_id} not found for update")
    except Exception as e:
        logging.error(f"Error updating ticket {ticket_id}: {e}")

def get_all_tickets():
    try:
        if not os.path.exists(TICKET_CSV):
            return []
        with open(TICKET_CSV, newline="") as f:
            reader = csv.DictReader(f)
            return list(reader)
    except Exception as e:
        logging.error(f"Failed to read tickets from CSV: {e}")
        return []

def get_escalation_members(product: str):
    if not product:
        logging.warning("Product is None in get_escalation_members")
        return []
    try:
        with open("escalationmatrix.csv", "r") as f:
            reader = csv.reader(f)
            for row in reader:
                if row[0].strip().lower() == product.strip().lower():
                    return [uid.strip() for uid in row[1:] if uid.strip()]
    except Exception as e:
        logging.error(f"Error reading escalationmatrix.csv: {e}")
    return []

def get_assignee_level(product: str, assignee_id: str):
    try:
        with open("escalationmatrix.csv", "r") as f:
            reader = csv.reader(f)
            for row in reader:
                if row[0].strip().lower() == product.strip().lower():
                    levels = [uid.strip() for uid in row[1:] if uid.strip()]
                    if assignee_id in levels:
                        idx = levels.index(assignee_id)
                        return f"L{idx+1}", idx, levels
    except Exception as e:
        logging.error(f"Error reading escalationmatrix.csv for assignee level: {e}")
    return None, None, []

def get_escalation_emails(product: str):
    """Return escalation emails for a product from escalationmatrixmail.csv"""
    try:
        with open("escalationmatrixmail.csv", "r") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if row[0].strip().lower() == product.strip().lower():
                    return [email.strip() for email in row[1:] if email.strip()]
    except Exception as e:
        logging.error(f"Error reading escalationmatrixmail.csv: {e}")
    return []

def get_slack_user_id_by_email(email, web_client):
    """Return Slack user ID for a given email using Slack API"""
    try:
        response = web_client.users_lookupByEmail(email=email)
        return response['user']['id']
    except SlackApiError as e:
        logging.error(f"Slack API error for email {email}: {e.response['error']}")
    except Exception as e:
        logging.error(f"Error looking up Slack user by email {email}: {e}")
    return None