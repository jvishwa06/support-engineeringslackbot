# Support Engineering Slack Bot

This project is a Slack bot designed to streamline support engineering workflows by integrating Slack, Jira, and custom ticket management. It automates ticket creation, triage, escalation, and resolution processes, providing reminders and seamless Jira integration.

## Features
- **Slack Integration**: Users can raise support tickets directly from Slack using slash commands and interactive modals.
- **Jira Integration**: Automatically creates and updates Jira issues based on ticket actions in Slack.
- **Automated Reminders**: Schedules and sends reminders for FRT (First Response Time) and escalations based on ticket priority.
- **Custom Ticket Management**: Tracks ticket lifecycle, including triage, assignment, and resolution, with detailed logging and summary posting.
- **Configurable Thresholds**: FRT and escalation thresholds are configurable via environment variables.

## Tech Stack
- **Python 3.10**
- **FastAPI**: For serving the web application and endpoints
- **Slack SDK**: For Slack bot, Socket Mode, and Web API integration
- **APScheduler**: For scheduling reminders and escalations
- **Jira Python Library**: For Jira Cloud API integration

## Requirements
- Python 3.8+
- Slack App with Bot and Socket Mode enabled
- Jira Cloud account with API access
- Environment variables configured (see below)

## Setup
1. **Clone the repository**
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure environment variables**:
   Create a `.env` file with the following variables:
   ```env
   SLACK_BOT_TOKEN=your-slack-bot-token
   SLACK_APP_TOKEN=your-slack-app-token
   PORT=8000
   PRODCHANNEL=your-slack-channel-id
   SUMMARY_CHANNEL=your-summary-channel-id
   FRT_HOURS_CRITICAL=1
   FRT_HOURS_HIGH=2
   FRT_HOURS_MEDIUM=4
   FRT_HOURS_LOW=8
   FRT_ESCALATION_HOURS=0.5
   JIRA_URL=https://your-domain.atlassian.net
   JIRA_USER=your-jira-email
   JIRA_TOKEN=your-jira-api-token
   JIRA_PROJECT_KEY=PROJECTKEY
   ```
4. **Run the bot**:
   ```bash
   python main.py
   ```

## Usage
- Raise a ticket using the `/querybot` command in Slack.
- After the ticket is raised, the assignee - L1, L2, or L3 of that product team should reply in the thread. (A reminder is sent to the assignee based on the level and priority of the issue.) The FRT (First Response Time) is calculated and the Triage button is enabled.
- Fill the triage form. After submission, the TTT (Time To Triage) is calculated and the Create Jira Ticket button is enabled. By clicking that, the Jira ticket is created.
- If the Resolved button is clicked, the TTR (Time To Resolve) is calculated and in Jira the ticket’s status is transitioned to Done.
- The detailed summary is sent to the `querybot-summary` channel.


## File Structure
- `main.py` - Main application logic, Slack and Jira integration, ticket workflow
- `utils.py` - Utility functions for ticket management
- `EMatrix.yml` - Product escalation matrix
- `requirements.txt` - Python dependencies

## License
This project is intended for internal use. Please contact the maintainer for licensing information.