"""
Tests for every branch in the Payment Check design.
Run from the project folder with:  pytest
"""

import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as payment_app
from app import Payment, handle_ussd, OWNER

STAFF = "977123456"


@pytest.fixture(autouse=True)
def fresh_data():
    payment_app.reset_demo_data()
    payment_app.payments.clear()
    yield


def add_payment(amount, minutes_ago=2, sender="260955554521", ref="R1"):
    payment_app.payments.append(
        Payment(ref, Decimal(str(amount)), sender,
                payment_app.now() - timedelta(minutes=minutes_ago))
    )


# --- Menus and access ------------------------------------------------------

def test_owner_sees_full_menu():
    assert handle_ussd(OWNER, "").startswith("CON Payment Check\n1. Check a payment\n2. Manage staff")


def test_staff_sees_check_and_help_only():
    reply = handle_ussd(STAFF, "")
    assert reply.startswith("CON ") and "Manage staff" not in reply


def test_unregistered_number_gets_nothing():
    assert handle_ussd("999999999", "") == "END " + payment_app.NOT_LINKED


def test_pending_staff_cannot_check_yet():
    payment_app.staff["955000111"] = "pending"
    assert handle_ussd("955000111", "") == "END " + payment_app.NOT_LINKED


def test_staff_cannot_reach_manage_staff():
    assert handle_ussd(STAFF, "2") == "END " + payment_app.HELP


# --- Check a payment -------------------------------------------------------

def test_payment_found_and_confirmed():
    add_payment(250)
    reply = handle_ussd(STAFF, "1*250")
    assert reply.startswith("END Payment received") and "ends 4521" in reply
    assert "balance" not in reply.lower()


def test_same_payment_cannot_be_confirmed_twice():
    add_payment(250)
    handle_ussd(STAFF, "1*250")
    assert "already confirmed" in handle_ussd(STAFF, "1*250")


def test_confirmed_payment_is_skipped_when_a_new_one_exists():
    add_payment(250, ref="OLD", sender="260955551111")
    handle_ussd(STAFF, "1*250")
    add_payment(250, ref="NEW", sender="260955552222")
    reply = handle_ussd(STAFF, "1*250")
    assert reply.startswith("END Payment received") and "NEW" in reply


def test_two_unconfirmed_payments_same_amount():
    add_payment(250, ref="A1", sender="260955551111")
    add_payment(250, ref="B2", sender="260955552222")
    reply = handle_ussd(STAFF, "1*250")
    assert "2 payments of K250 found" in reply
    assert all(p.confirmed_at is None for p in payment_app.payments)


def test_customer_paid_less():
    add_payment(150)
    reply = handle_ussd(STAFF, "1*250")
    assert "No K250 payment found" in reply and "K150" in reply


def test_no_payment_found():
    reply = handle_ussd(STAFF, "1*250")
    assert "No payment of K250 in the last 10 minutes" in reply


def test_payments_older_than_10_minutes_are_ignored():
    add_payment(250, minutes_ago=15)
    assert "No payment of K250" in handle_ussd(STAFF, "1*250")


def test_invalid_amount_then_valid():
    add_payment(250)
    assert handle_ussd(STAFF, "1*abc") == "CON " + payment_app.INVALID_AMOUNT
    assert handle_ussd(STAFF, "1*abc*250").startswith("END Payment received")


def test_three_invalid_tries_ends_session():
    assert handle_ussd(STAFF, "1*a*b*c") == "END " + payment_app.TOO_MANY_TRIES


# --- Manage staff ----------------------------------------------------------

def test_add_staff_is_pending_until_accepted():
    reply = handle_ussd(OWNER, "2*1*0955000111*1")
    assert reply.startswith("END Staff added")
    assert payment_app.staff["955000111"] == "pending"


def test_cancel_add_staff():
    handle_ussd(OWNER, "2*1*0955000111*2")
    assert "955000111" not in payment_app.staff


def test_remove_staff():
    handle_ussd(OWNER, "2*2*1")
    assert "966234567" not in payment_app.staff


def test_back_returns_to_main_menu():
    assert handle_ussd(OWNER, "2*0").startswith("CON Payment Check")


# --- Screen length ---------------------------------------------------------

@pytest.mark.parametrize("text", ["", "1", "1*abc", "2", "2*1", "2*2", "2*3", "3"])
def test_owner_screens_fit_ussd_limit(text):
    reply = handle_ussd(OWNER, text)
    assert len(reply) - 4 <= payment_app.MAX_SCREEN_CHARS


def test_result_screens_fit_ussd_limit():
    add_payment(250, ref="A1", sender="260955551111")
    add_payment(250, ref="B2", sender="260955552222")
    add_payment(150, ref="C3", sender="260955553333")
    for text in ["1*250", "1*400", "1*9999999"]:
        reply = handle_ussd(STAFF, text)
        assert len(reply) - 4 <= payment_app.MAX_SCREEN_CHARS, reply
