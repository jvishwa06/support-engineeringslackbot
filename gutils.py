import gspread
import logging
import os
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger()

G_SHEET_ID = os.getenv("G_SHEET_ID")
G_SHEET_CREDENTIALS_FILE = os.getenv("G_SHEET_CREDENTIALS_FILE")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
try:
    creds = Credentials.from_service_account_file(G_SHEET_CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet_id = G_SHEET_ID
    spreadsheet = client.open_by_key(sheet_id)
    tickets_sheet = spreadsheet.worksheet("Tickets")
    escalation_sheet = spreadsheet.worksheet("EscalationMatrix")
    logger.info("Successfully connected to Google Sheets.")
except Exception as e:
    logger.critical(f"FATAL: Could not connect to Google Sheets. Check credentials and sheet names. Error: {e}")

FIELDNAMES = [
    "id", "title", "description", "raised_by", "raiser_id", "raised_at", "client_name", "client_app_id",
    "criticality", "product", "endpoint", "assigned_to", "assignee_id", "frt_hours", "message_ts",
    "ttt_hours", "triaged_by", "triaged_id", "triage_ts", "ttr_hours", "resolved_by", "resolver_id",
    "resolve_at", "escalate_to", "escalate_id", "priority", "summary", "fix", "issue"
]

def save_ticket(ticket: dict):
    if not tickets_sheet:
        logger.error("Cannot save ticket, Google Sheet not available.")
        return
    try:
        row_to_add = [str(ticket.get(field, "")) for field in FIELDNAMES]
        tickets_sheet.append_row(row_to_add, value_input_option='USER_ENTERED')
        logger.info(f"Ticket saved to Google Sheet: {ticket.get('id')}")
    except Exception as e:
        logger.error(f"Failed to save ticket to Google Sheet: {e}. Ticket data: {ticket}")

def update_ticket(ticket_id: str, updates: dict):
    if not tickets_sheet:
        logger.error("Cannot update ticket, Google Sheet not available.")
        return
    try:
        cell = tickets_sheet.find(ticket_id, in_column=1)
        if not cell:
            logger.warning(f"Ticket ID {ticket_id} not found in Google Sheet for update")
            return
        row_values = tickets_sheet.row_values(cell.row)
        current_ticket_dict = dict(zip(FIELDNAMES, row_values))
        current_ticket_dict.update(updates)
        updated_row = [str(current_ticket_dict.get(field, "")) for field in FIELDNAMES]
        range_to_update = f'A{cell.row}:{chr(ord("A") + len(FIELDNAMES) - 1)}{cell.row}'
        tickets_sheet.update(range_to_update, [updated_row])
        logger.info(f"Ticket {ticket_id} updated in Google Sheet.")
    except Exception as e:
        logger.error(f"Error updating ticket {ticket_id} in Google Sheet: {e}")

def get_all_tickets():
    if not tickets_sheet:
        logger.error("Cannot get tickets, Google Sheet not available.")
        return []
    try:
        records = tickets_sheet.get_all_records()
        for ticket in records:
            ticket['frt_hours'] = str(ticket.get('frt_hours', '')).strip()
        return records
    except Exception as e:
        logger.error(f"Failed to read tickets from Google Sheet: {e}")
        return []

def get_all_products():
    if not escalation_sheet:
        logger.error("Cannot get products, Google Sheet not available.")
        return []
    try:
        products = escalation_sheet.col_values(1)[1:]
        return products
    except Exception as e:
        logger.error(f"Error reading product list from Google Sheet: {e}")
        return []

def get_escalation_members(product: str):
    if not escalation_sheet:
        logger.error("Cannot get escalation members, Google Sheet not available.")
        return []
    try:
        all_values = escalation_sheet.get_all_values()
        for row in all_values:
            if row and row[0].strip().lower() == product.strip().lower():
                logger.info(f"Escalation members for product '{product}': {row[1:]}")
                return [uid.strip() for uid in row[1:] if uid.strip()]
        logger.warning(f"No escalation members found for product '{product}'")
    except Exception as e:
        logger.error(f"Error reading EscalationMatrix from Google Sheet: {e}")
    return []

def get_assignee_level(product: str, assignee_id: str):
    if not escalation_sheet:
        logger.error("Cannot get assignee level, Google Sheet not available.")
        return None, None, []
    try:
        all_values = escalation_sheet.get_all_values()
        for row in all_values:
            if row and row[0].strip().lower() == product.strip().lower():
                levels = [uid.strip() for uid in row[1:] if uid.strip()]
                logger.info(f"Assignee levels for product '{product}': {levels}")
                if assignee_id in levels:
                    idx = levels.index(assignee_id)
                    return f"L{idx+1}", idx, levels
        logger.warning(f"Assignee '{assignee_id}' not found for product '{product}'")
    except Exception as e:
        logger.error(f"Error reading EscalationMatrix from Google Sheet: {e}")
    return None, None, []