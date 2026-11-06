"""Optional resume-link email through a dedicated Gmail account."""
import smtplib
from email.message import EmailMessage

SMTP_HOST = 'smtp.gmail.com'
SMTP_PORT = 587
SMTP_TIMEOUT_SECONDS = 15


def email_configured(settings):
    return bool(settings.gmail_address and settings.gmail_app_password)


def build_message(sender, recipient, link, answered, total):
    message = EmailMessage()
    message['Subject'] = 'Continue your TFM ADP Player Pricing votes'
    message['From'] = sender
    message['To'] = recipient
    message.set_content(
        f'You have answered {answered} of {total} players.\n\n'
        f'Pick up where you left off:\n{link}\n\n'
        'Please don\'t share this link. Anyone who has it can vote as you.\n'
    )
    return message


def send_resume_email(settings, recipient, link, answered, total):
    """Blocking send: call it from a worker thread. The address is never stored or logged."""
    message = build_message(settings.gmail_address, recipient, link, answered, total)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        smtp.starttls()
        smtp.login(settings.gmail_address, settings.gmail_app_password)
        smtp.send_message(message)
