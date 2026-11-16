"""import_square_invoices: a few real Square invoices onto staging, read only."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.integrations import linking, square
from apps.integrations.models import SquareInvoice
from apps.jobs.models import PaymentStatus, Status

from .test_square_webhooks import invoice

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def square_on(settings, monkeypatch):
    settings.SQUARE_ACCESS_TOKEN = "t"
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = "k"
    settings.SQUARE_ENABLED = True
    settings.SQUARE_LOCATION_ID = "LOC1"
    listed = [
        dict(
            invoice(id="inv-a", number="000201", title="Team hoodies", status="PAID", paid=52500),
            created_at="2026-10-01T10:00:00Z",
        ),
        dict(
            invoice(id="inv-b", number="000202", title="G-1042 tees"),
            created_at="2026-11-01T10:00:00Z",
        ),
        dict(
            invoice(id="inv-c", number="000203", title="Old order"),
            created_at="2026-01-01T10:00:00Z",
        ),
    ]
    by_id = {d["id"]: d for d in listed}
    monkeypatch.setattr(square, "list_locations", lambda: ["LOC1"])
    monkeypatch.setattr(square, "list_invoices", lambda loc, max_pages=50: iter(listed))
    monkeypatch.setattr(square, "get_invoice", lambda i: by_id.get(i))
    monkeypatch.setattr(square, "get_order", lambda i: None)
    return listed


def run(*args):
    out = StringIO()
    call_command("import_square_invoices", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def job(job_at):
    j = job_at(Status.QUOTE_SENT)
    j.job_number = "G-1042"
    j.save()
    return j


def test_dry_run_saves_nothing():
    out = run("--latest", "2", "--dry-run")
    assert "#000202" in out and "#000201" in out and "#000203" not in out
    assert "would import" in out and not SquareInvoice.objects.exists()


def test_latest_imports_the_newest(job):
    out = run("--latest", "2")
    assert "linked to" in out and "waiting under Invoices" in out
    assert set(SquareInvoice.objects.values_list("square_id", flat=True)) == {"inv-a", "inv-b"}
    job.refresh_from_db()
    assert job.status == Status.INVOICED
    assert [i.square_id for i in linking.waiting()] == ["inv-a"]


def test_by_number_and_by_id():
    run("--number", "000203", "--id", "inv-a")
    assert set(SquareInvoice.objects.values_list("square_id", flat=True)) == {"inv-a", "inv-c"}


def test_linking_a_paid_sample_by_hand_marks_the_job_paid(job_at, manager):
    run("--number", "000201")
    target = job_at(Status.QUOTE_SENT)
    linking.link(SquareInvoice.objects.get(square_id="inv-a"), target, user=manager)
    target.refresh_from_db()
    assert target.payment_status == PaymentStatus.PAID


def test_running_again_changes_nothing(job):
    run("--latest", "3")
    run("--latest", "3")
    assert SquareInvoice.objects.count() == 3


@pytest.mark.parametrize(
    "args, message",
    [
        ([], "Say which invoices"),
        (["--latest", "50"], "20 at most"),
        (["--number", "999999"], "no invoice numbered 999999"),
        (["--id", "inv-zzz"], "no invoice inv-zzz"),
    ],
)
def test_mistakes_are_explained(args, message):
    with pytest.raises(CommandError, match=message):
        run(*args)


def test_a_token_for_another_account_is_explained(monkeypatch):
    monkeypatch.setattr(square, "list_locations", lambda: ["OTHER"])
    with pytest.raises(CommandError, match="can't read location LOC1"):
        run("--latest", "1")


def test_no_token(settings):
    settings.SQUARE_ACCESS_TOKEN = ""
    with pytest.raises(CommandError, match="isn't set up"):
        run("--latest", "1")


# --- --live: the real account for one command, token typed at a hidden prompt ---------


@pytest.fixture
def typed(monkeypatch):
    from apps.integrations.management.commands import _live

    answers = []
    monkeypatch.setattr(_live.getpass, "getpass", lambda prompt: answers.pop(0))
    return answers


def test_live_uses_the_typed_token_and_location(settings, typed, monkeypatch):
    settings.SQUARE_ENVIRONMENT = "sandbox"
    seen = {}
    monkeypatch.setattr(
        square,
        "list_locations",
        lambda: (
            seen.update(env=settings.SQUARE_ENVIRONMENT, token=settings.SQUARE_ACCESS_TOKEN)
            or ["LIVELOC1"]
        ),
    )
    typed.append("  live-token  ")
    out = run("--latest", "1", "--dry-run", "--live", "--location", "LIVELOC1")
    assert seen == {"env": "production", "token": "live-token"}
    assert settings.SQUARE_LOCATION_ID == "LIVELOC1" and "would import" in out


def test_live_customers_too(settings, typed, monkeypatch):
    monkeypatch.setattr(square, "list_locations", lambda: ["LIVELOC1"])
    monkeypatch.setattr(square, "get_customer", lambda cid: None)
    settings.SQUARE_ENVIRONMENT = "sandbox"
    typed.append("live-token")
    out = StringIO()
    call_command(
        "import_square_customers", "--dry-run", "--live", "--location", "LIVELOC1", stdout=out
    )
    assert settings.SQUARE_ENVIRONMENT == "production" and "Would" in out.getvalue()


@pytest.mark.parametrize(
    "args, answer, message",
    [
        (["--live"], "t", "needs --location"),
        (["--live", "--location", "bad id!"], "t", "needs --location"),
        (["--live", "--location", "LIVELOC1"], "   ", "No token given"),
        (["--location", "LIVELOC1"], "t", "goes with --live"),
    ],
)
def test_live_mistakes(settings, typed, args, answer, message):
    settings.SQUARE_ENVIRONMENT = "sandbox"
    typed.append(answer)
    with pytest.raises(CommandError, match=message):
        run("--latest", "1", *args)
