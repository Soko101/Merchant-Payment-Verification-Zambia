"""
Payment Check - USSD prototype
==============================

Lets a market trader, or staff they approve, check that a mobile money payment
arrived, without ever seeing the owner's balance.

How USSD works with Africa's Talking:
- Each time the user presses Send, Africa's Talking POSTs to /ussd with:
    sessionId, serviceCode, phoneNumber, text
- `text` is EVERYTHING the user has typed this session, joined by "*".
  e.g. "" = just dialled, "1" = chose option 1, "1*250" = option 1 then 250.
- We reply with plain text starting with:
    "CON " -> show the screen and wait for input
    "END " -> show the screen and close the session

IMPORTANT: this is a demo. Payments are synthetic and live in memory, so they
reset whenever the app restarts. Nothing here touches a real mobile money network.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify

app = Flask(__name__)

ZAMBIA_TZ = ZoneInfo("Africa/Lusaka")
CHECK_WINDOW = timedelta(minutes=10)   # Design decision 3: only the last 10 minutes
MAX_INVALID_TRIES = 3                  # Stop looping on bad input
MAX_SCREEN_CHARS = 160                 # USSD screens must stay short


# ---------------------------------------------------------------------------
# Data (in memory, synthetic)
# ---------------------------------------------------------------------------

@dataclass
class Payment:
    ref: str
    amount: Decimal
    sender: str                         # full number, never shown in full
    received_at: datetime
    confirmed_at: datetime | None = None


# One demo business. Phone numbers are stored as their last 9 digits
# (e.g. 977123456) so "+260977123456" and "0977123456" match.
OWNER = "970000001"
staff = {}          # phone -> "accepted" or "pending"
payments = []       # list of Payment


def now():
    return datetime.now(ZAMBIA_TZ)


def reset_demo_data(current_time=None):
    """Load a fresh set of synthetic data. Called at startup and by /demo/reset."""
    t = current_time or now()
    staff.clear()
    staff.update({"977123456": "accepted", "966234567": "accepted"})
    payments.clear()
    payments.extend([
        Payment("8F3K2", Decimal("250"), "260955554521", t - timedelta(minutes=3)),
        Payment("4B7N1", Decimal("150"), "260966617788", t - timedelta(minutes=5)),
        Payment("9Q2X8", Decimal("75"), "260977703344", t - timedelta(minutes=25)),  # outside window
    ])


def normalise_phone(raw):
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return digits[-9:]


def pretty_phone(phone9):
    """977123456 -> 0977 123 456"""
    p = "0" + phone9
    return f"{p[:4]} {p[4:7]} {p[7:]}"


def fmt_amount(amount):
    return f"K{amount:,.2f}"


def fmt_time(dt):
    return dt.astimezone(ZAMBIA_TZ).strftime("%H:%M")


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

def con(message):
    return _reply("CON", message)


def end(message):
    return _reply("END", message)


def _reply(prefix, message):
    if len(message) > MAX_SCREEN_CHARS:
        app.logger.warning("Screen too long (%d chars): %r", len(message), message)
    return f"{prefix} {message}"


# ---------------------------------------------------------------------------
# Screens (text matches the design file)
# ---------------------------------------------------------------------------

OWNER_MENU = "Payment Check\n1. Check a payment\n2. Manage staff\n3. Help"
STAFF_MENU = "Payment Check\n1. Check a payment\n2. Help"
ENTER_AMOUNT = "Enter the amount the customer says they paid (K):"
INVALID_AMOUNT = "Please enter numbers only, e.g. 250"
TOO_MANY_TRIES = "Too many tries. Please dial again."
HELP = ("Payment Check lets you and your staff confirm a payment arrived. "
        "It never shows your balance. Demo only: uses test data.")
NOT_LINKED = "This number is not linked to a business. Ask the business owner to add you."
MANAGE_STAFF = "Manage staff\n1. Add staff\n2. Remove staff\n3. View staff\n0. Back"
INVALID_OPTION = "Invalid option. Please dial again."


# ---------------------------------------------------------------------------
# The core check (design decisions 3, 4, 5, 7, 8)
# ---------------------------------------------------------------------------

def check_payment(claimed, current_time=None):
    """Return the END screen for a claimed amount, confirming a payment if found."""
    t = current_time or now()
    recent = [p for p in payments if t - p.received_at <= CHECK_WINDOW]

    exact = [p for p in recent if p.amount == claimed]
    unconfirmed = [p for p in exact if p.confirmed_at is None]

    # Exactly one unconfirmed match: confirm it (decision 8: confirm only once)
    if len(unconfirmed) == 1:
        p = unconfirmed[0]
        p.confirmed_at = t
        return end(
            f"Payment received\n{fmt_amount(p.amount)} at {fmt_time(p.received_at)}\n"
            f"Ref: {p.ref}\nSender number ends {p.sender[-4:]}"
        )

    # Two or more unconfirmed matches: list them, confirm nothing
    if len(unconfirmed) >= 2:
        lines = [f"{fmt_time(p.received_at)} ref {p.ref} (ends {p.sender[-4:]})"
                 for p in sorted(unconfirmed, key=lambda p: p.received_at, reverse=True)[:2]]
        return end(
            f"{len(unconfirmed)} payments of K{claimed:,.0f} found:\n" + "\n".join(lines) +
            "\nAsk the customer for the last 4 digits of their number."
        )

    # Only already-confirmed matches: someone may be claiming another customer's payment
    if exact:
        p = max(exact, key=lambda p: p.confirmed_at)
        return end(
            f"K{p.amount:,.0f} at {fmt_time(p.received_at)} (ends {p.sender[-4:]}) "
            f"was already confirmed at {fmt_time(p.confirmed_at)}. "
            "Ask the customer for the last 4 digits of their number."
        )

    # Customer paid less than claimed (decision 4). Confirmed payments are skipped.
    lower = [p for p in recent if p.amount < claimed and p.confirmed_at is None]
    if lower:
        p = max(lower, key=lambda p: p.amount)
        return end(
            f"No K{claimed:,.0f} payment found.\n"
            f"Closest in last 10 min: K{p.amount:,.0f} at {fmt_time(p.received_at)} "
            f"(ends {p.sender[-4:]})\nDo not hand over goods yet."
        )

    # Nothing at all (decision 7: say what to do next)
    return end(
        f"No payment of K{claimed:,.0f} in the last 10 minutes.\n"
        "Do not hand over goods yet. Ask the customer to check their payment."
    )


def parse_amount(value):
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        return None
    return amount


def amount_flow(steps):
    """steps = everything typed after choosing 'Check a payment'."""
    if not steps:
        return con(ENTER_AMOUNT)
    amount = parse_amount(steps[-1])
    if amount is not None:
        return check_payment(amount)
    if len(steps) >= MAX_INVALID_TRIES:
        return end(TOO_MANY_TRIES)
    return con(INVALID_AMOUNT)


# ---------------------------------------------------------------------------
# Staff management (owner only, design decision 6)
# ---------------------------------------------------------------------------

def staff_flow(steps):
    if not steps:
        return con(MANAGE_STAFF)
    choice, rest = steps[0], steps[1:]

    if choice == "1":                           # Add staff
        if not rest:
            return con("Enter staff phone number:")
        number = normalise_phone(rest[0])
        if len(number) != 9:
            return end("That number doesn't look right. Please dial again.")
        if len(rest) == 1:
            return con(f"Add {pretty_phone(number)} as staff?\n1. Confirm\n2. Cancel")
        if rest[1] == "1":
            staff[number] = "pending"           # becomes "accepted" after SMS consent
            return end("Staff added. They will get an SMS and must accept "
                       "before they can check payments.")
        return end("Cancelled. No staff added.")

    if choice == "2":                           # Remove staff
        numbers = sorted(staff)
        if not numbers:
            return end("You have no staff.")
        if not rest:
            options = "\n".join(f"{i}. {pretty_phone(n)}" for i, n in enumerate(numbers, 1))
            return con(f"Choose staff to remove:\n{options}")
        if rest[0].isdigit() and 1 <= int(rest[0]) <= len(numbers):
            number = numbers[int(rest[0]) - 1]
            del staff[number]
            return end(f"{pretty_phone(number)} removed.\nThey can no longer check payments.")
        return end(INVALID_OPTION)

    if choice == "3":                           # View staff
        if not staff:
            return end("You have no staff.")
        lines = [pretty_phone(n) + (" (pending)" if s == "pending" else "")
                 for n, s in sorted(staff.items())]
        return end("Your staff:\n" + "\n".join(lines))

    return end(INVALID_OPTION)


# ---------------------------------------------------------------------------
# Main USSD endpoint
# ---------------------------------------------------------------------------

def handle_ussd(phone, text):
    """Pure function: given who is dialling and what they typed, return the reply."""
    steps = text.split("*") if text else []

    if phone == OWNER:
        # "0. Back" from Manage staff returns to the main menu: drop each "2*0"
        while steps[:2] == ["2", "0"]:
            steps = steps[2:]
        if not steps:
            return con(OWNER_MENU)
        if steps[0] == "1":
            return amount_flow(steps[1:])
        if steps[0] == "2":
            return staff_flow(steps[1:])
        if steps[0] == "3":
            return end(HELP)
        return end(INVALID_OPTION)

    if staff.get(phone) == "accepted":          # staff never see Manage staff
        if not steps:
            return con(STAFF_MENU)
        if steps[0] == "1":
            return amount_flow(steps[1:])
        if steps[0] == "2":
            return end(HELP)
        return end(INVALID_OPTION)

    return end(NOT_LINKED)                      # unregistered or not yet accepted


@app.post("/ussd")
def ussd():
    phone = normalise_phone(request.values.get("phoneNumber", ""))
    text = request.values.get("text", "")
    return handle_ussd(phone, text), 200, {"Content-Type": "text/plain"}


# ---------------------------------------------------------------------------
# Demo controls (so visitors can trigger payments and try the flow)
# ---------------------------------------------------------------------------

@app.post("/demo/pay")
def demo_pay():
    """Simulate a customer paying. JSON: {"amount": 250, "sender": "0955554521"}"""
    data = request.get_json(silent=True) or {}
    amount = parse_amount(str(data.get("amount", "")))
    if amount is None:
        return jsonify(error="amount must be a positive number"), 400
    sender = "260" + normalise_phone(data.get("sender", "0955550000"))
    ref = f"D{len(payments) + 1:04d}"
    payments.append(Payment(ref, amount, sender, now()))
    return jsonify(ref=ref, amount=str(amount))


@app.post("/demo/accept")
def demo_accept():
    """Simulate a staff member accepting the SMS invitation. JSON: {"phone": "0977123456"}"""
    phone = normalise_phone((request.get_json(silent=True) or {}).get("phone", ""))
    if staff.get(phone) != "pending":
        return jsonify(error="no pending invitation for that number"), 404
    staff[phone] = "accepted"
    return jsonify(phone=pretty_phone(phone), status="accepted")


@app.post("/demo/reset")
def demo_reset():
    reset_demo_data()
    return jsonify(status="reset")


@app.get("/")
def health():
    return "Payment Check demo is running."


reset_demo_data()

if __name__ == "__main__":
    app.run(port=5000, debug=True)
