import csv
import os
import logging

TICKET_CSV = "tickets.csv"

FIELDNAMES = [
    "id","title","description","raised_by","raiser_id","raised_at","client_name","client_app_id","criticality","product","endpoint","assigned_to","assignee_id","frt_hours","message_ts",
    "ttt_hours","triaged_by","triaged_id","triage_ts","ttr_hours","resolved_by","resolver_id","resolve_at","escalate_to","escalate_id"
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

def get_all_tickets():
    if not os.path.exists(TICKET_CSV):
        return []
    with open(TICKET_CSV, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)

def update_ticket(ticket_id: str, updates: dict):
    tickets = get_all_tickets()
    updated = False
    for ticket in tickets:
        if ticket["id"] == ticket_id:
            ticket.update(updates)
            updated = True
            break
    if updated:
        try:
            with open(TICKET_CSV, mode="w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerows(tickets)
        except Exception as e:
            logging.error(f"Failed to update ticket in CSV: {e}")
    else:
        logging.warning(f"Ticket ID {ticket_id} not found for update")

def get_escalation_members(product: str):
    try:
        with open("escalationmatrix.csv", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["Product"].strip().lower() == product.strip().lower():
                    return [row["L1"].strip(), row["L2"].strip(), row["L3"].strip()]
    except Exception as e:
        logging.error(f"Failed to read escalation matrix: {e}")
    return []