import csv
import os
import logging
import yaml

TICKET_CSV = "tickets.csv"

FIELDNAMES = [
    "id","title","description","raised_by","raiser_id","raised_by_email","raised_at","client_name","client_app_id","criticality","product","endpoint","assigned_to","assignee_id","assignee_email","frt_hours","message_ts",
    "ttt_hours","triaged_by","triaged_id","triage_ts","ttr_hours","resolved_by","resolver_id","resolve_at","escalate_to","escalate_id", "priority", "summary", "fix", "issue", "labels", "l0_testing", "slack_message_link", "kibana_link"
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
        with open("EMatrix.yml", "r") as f:
            data = yaml.safe_load(f)
            for entry in data:
                if entry["product"].strip().lower() == product.strip().lower():
                    return [entry.get(level) for level in ["L1", "L2", "L3"] if entry.get(level)]
    except Exception as e:
        logging.error(f"Error reading EMatrix.yml: {e}")
    return []