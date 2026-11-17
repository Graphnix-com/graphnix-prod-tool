"""Purchase orders, receiving and the release to production (Step 3, PR 2; FR-13 to FR-16)."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.accounts.models import AppSetting
from apps.jobs import board, timeline
from apps.jobs.models import Job, JobItem, JobItemSize, JobStatusHistory, Status
from apps.production.models import DecorationMethod, ProductionTask, TaskStatus
from apps.purchasing import services
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine

pytestmark = pytest.mark.django_db


def item(job, description="Gildan 5000 tee", color="Black", sizes=None, **fields):
    row = JobItem.objects.create(job=job, description=description, color=color, **fields)
    for size, qty in (sizes or {"M": 30, "L": 20}).items():
        JobItemSize.objects.create(job_item=row, size=size, quantity=qty)
    return row


@pytest.fixture
def approved(job_at):
    """A job past the mock-up gate with one garment item: M 30, L 20."""
    job = job_at(Status.MOCKUP_APPROVED)
    item(job)
    return job


def build(user, *jobs, supplier="SanMar"):
    return services.build(user=user, supplier_name=supplier, jobs=jobs)


def receive_all(po, user):
    return services.receive(
        po, user=user, quantities={line.pk: line.outstanding for line in po.lines.all()}
    )


# --- Build ---------------------------------------------------------------------------


def test_a_po_for_two_jobs_fills_its_lines_from_their_items(manager, approved, job_at):
    other = job_at(Status.MATERIALS_ORDERED)
    item(other, "Hoodie", "Navy", {"XL": 5})
    item(other, "Their own caps", "", {"OS": 12}, customer_supplied=True)
    stock = item(other, "Tote", "Natural", {"OS": 10})
    stock.from_stock_at = stock.job.created_at
    stock.save()
    po = build(manager, approved, other)
    assert po.po_number.startswith("PO-") and po.status == PurchaseOrder.Status.DRAFT
    assert po.supplier_name == "SanMar"
    rows = sorted(
        (line.job_id, line.description, line.size, line.qty_ordered) for line in po.lines.all()
    )
    assert rows == sorted(
        [
            (approved.pk, "Gildan 5000 tee", "M", 30),
            (approved.pk, "Gildan 5000 tee", "L", 20),
            (other.pk, "Hoodie", "XL", 5),
        ]
    )


def test_po_numbers_count_up(manager, approved, job_at):
    first = build(manager, approved)
    second_job = job_at(Status.MOCKUP_APPROVED)
    item(second_job)
    second = build(manager, second_job)
    assert int(second.po_number[3:]) == int(first.po_number[3:]) + 1


def test_only_whats_still_needed_goes_on_a_second_po(manager, approved):
    po = build(manager, approved)
    services.change_qty(po.lines.get(size="L"), user=manager, qty=12)  # 8 L ordered elsewhere
    second = build(manager, approved, supplier="S&S")
    assert [(line.size, line.qty_ordered) for line in second.lines.all()] == [("L", 8)]
    with pytest.raises(ValidationError, match="Nothing is left to order"):
        build(manager, approved)


def test_the_mockup_gate_keeps_jobs_off_pos(manager, job_at):
    job = job_at(Status.MOCKUP_SENT)
    item(job)
    with pytest.raises(ValidationError, match="past Mock-up approved"):
        build(manager, job)


def test_a_supplier_name_is_needed(manager, approved):
    with pytest.raises(ValidationError, match="who the order is with"):
        build(manager, approved, supplier="  ")


def test_outsourced_work_goes_on_as_a_service_and_is_done_when_received(manager, approved):
    method, _ = DecorationMethod.objects.get_or_create(name="Embroidery", defaults={"code": "EMB"})
    task = ProductionTask.objects.create(
        job=approved, decoration_method=method, outsourced=True, qty_planned=50
    )
    po = build(manager, approved)
    service = po.lines.get(production_task=task)
    assert service.description == "Embroidery (outsourced)" and service.qty_ordered == 50
    services.mark_ordered(po, user=manager)
    services.receive(po, user=manager, quantities={service.pk: 50})
    task.refresh_from_db()
    assert task.status == TaskStatus.DONE and task.completed_at is not None


# --- Ordered --------------------------------------------------------------------------------


def test_marking_ordered_moves_jobs_to_materials_ordered(manager, approved):
    po = build(manager, approved)
    blocked = services.mark_ordered(po, user=manager, supplier_order_ref="SO-77")
    approved.refresh_from_db()
    assert blocked == [] and approved.status == Status.MATERIALS_ORDERED
    row = approved.history.get()
    assert row.source == JobStatusHistory.Source.SYSTEM and po.po_number in row.note
    po.refresh_from_db()
    assert po.status == PurchaseOrder.Status.ORDERED and po.supplier_order_ref == "SO-77"
    with pytest.raises(ValidationError, match="already ordered"):
        services.mark_ordered(po, user=manager)


def test_a_payment_gate_keeps_a_job_where_it_is(manager, approved, settings):
    settings.SQUARE_ENABLED = True
    AppSetting.objects.update_or_create(
        key="payment_gates", defaults={"value": {"materials_ordered": "paid"}}
    )
    po = build(manager, approved)
    blocked = services.mark_ordered(po, user=manager)
    approved.refresh_from_db()
    assert approved.status == Status.MOCKUP_APPROVED
    assert blocked[0][0] == approved and "payment in full" in blocked[0][1]


# --- Receiving and the release (FR-15, FR-16) ----------------------------------------------


def test_a_partial_delivery_waits_and_the_rest_releases_the_job(make_user, manager, approved):
    floor = make_user("production")
    po = build(manager, approved)
    services.mark_ordered(po, user=manager)
    m, l_line = po.lines.get(size="M"), po.lines.get(size="L")
    assert services.receive(po, user=floor, quantities={m.pk: 30, l_line.pk: 8}) == []
    po.refresh_from_db()
    approved.refresh_from_db()
    assert po.status == PurchaseOrder.Status.PARTLY_RECEIVED
    assert approved.status == Status.MATERIALS_ORDERED
    released = services.receive(po, user=floor, quantities={l_line.pk: 12})
    approved.refresh_from_db()
    po.refresh_from_db()
    assert released == [approved] and approved.status == Status.READY_FOR_PRODUCTION
    assert po.status == PurchaseOrder.Status.RECEIVED
    assert approved.history.last().note == "All materials in"


def test_closing_a_line_short_releases_the_job(manager, approved):
    po = build(manager, approved)
    services.mark_ordered(po, user=manager)
    services.receive(po, user=manager, quantities={po.lines.get(size="M").pk: 30})
    line = po.lines.get(size="L")
    with pytest.raises(ValidationError, match="Say why"):
        services.close_short(line, user=manager, reason="")
    assert services.close_short(line, user=manager, reason="Backordered, swapped for Navy")
    approved.refresh_from_db()
    assert approved.status == Status.READY_FOR_PRODUCTION


def test_customer_goods_must_be_checked_in_too(manager, approved):
    caps = item(approved, "Their own caps", "", {"OS": 12}, customer_supplied=True)
    po = build(manager, approved)
    services.mark_ordered(po, user=manager)
    assert receive_all(po, manager) == []  # the caps aren't in yet
    assert services.check_in_goods(caps, user=manager)
    approved.refresh_from_db()
    assert approved.status == Status.READY_FOR_PRODUCTION


def test_a_job_all_from_stock_releases_on_the_last_tick(make_user, job_at):
    floor = make_user("production")
    job = job_at(Status.MOCKUP_APPROVED)
    tee, tote = item(job), item(job, "Tote", "Natural", {"OS": 10})
    assert services.set_from_stock(tee, user=floor) is False
    assert services.set_from_stock(tote, user=floor) is True
    job.refresh_from_db()
    assert job.status == Status.READY_FOR_PRODUCTION  # no PO needed


def test_from_stock_isnt_offered_for_an_item_already_on_a_po(manager, approved):
    build(manager, approved)
    with pytest.raises(ValidationError, match="on a purchase order already"):
        services.set_from_stock(approved.items.get(), user=manager)


def test_a_consumable_for_the_job_must_arrive_too(manager, approved):
    po = build(manager, approved)
    ink = services.add_line(po, user=manager, description="Plastisol white", qty=2, job=approved)
    services.mark_ordered(po, user=manager)
    garments = {line.pk: line.qty_ordered for line in po.lines.exclude(pk=ink.pk)}
    services.receive(po, user=manager, quantities=garments)
    approved.refresh_from_db()
    assert approved.status == Status.MATERIALS_ORDERED
    services.receive(po, user=manager, quantities={ink.pk: 2})
    approved.refresh_from_db()
    assert approved.status == Status.READY_FOR_PRODUCTION


def test_a_stock_consumable_moves_nothing(manager):
    po = PurchaseOrder.objects.create(supplier_name="Ink Co", kind="consumable")
    line = services.add_line(po, user=manager, description="Emulsion", qty=1)
    services.mark_ordered(po, user=manager)
    assert services.receive(po, user=manager, quantities={line.pk: 1}) == []
    po.refresh_from_db()
    assert po.status == PurchaseOrder.Status.RECEIVED and line.job is None


def test_a_po_with_deliveries_cant_be_cancelled(manager, approved):
    po = build(manager, approved)
    services.mark_ordered(po, user=manager)
    receive_all(po, manager)
    with pytest.raises(ValidationError):
        services.cancel(po, user=manager, reason="Wrong supplier")


def test_cancelling_frees_the_items_for_another_po(manager, approved):
    po = build(manager, approved)
    services.cancel(po, user=manager, reason="Wrong supplier")
    assert build(manager, approved, supplier="S&S").lines.count() == 2


# --- Who may ----------------------------------------------------------------------------------


def test_the_floor_receives_but_doesnt_build_or_order(make_user, manager, approved):
    floor = make_user("production")
    with pytest.raises(PermissionDenied):
        build(floor, approved)
    po = build(manager, approved)
    with pytest.raises(PermissionDenied):
        services.mark_ordered(po, user=floor)
    services.mark_ordered(po, user=manager)
    receive_all(po, floor)
    with pytest.raises(PermissionDenied):
        services.close_short(po.lines.first(), user=floor, reason="x")


def test_customer_entry_cant_receive(make_user, manager, approved):
    po = build(manager, approved)
    services.mark_ordered(po, user=manager)
    with pytest.raises(PermissionDenied):
        receive_all(po, make_user("customer_entry"))


# --- Screens -------------------------------------------------------------------------------------


def test_build_order_and_receive_through_the_pages(client, manager, approved):
    client.force_login(manager)
    page = client.get(reverse("purchasing:build")).content.decode()
    assert approved.display_number in page and "M ×30" in page
    response = client.post(
        reverse("purchasing:build"), {"supplier_name": "SanMar", "jobs": [approved.pk]}
    )
    po = PurchaseOrder.objects.get()
    assert response["Location"] == reverse("purchasing:detail", args=[po.pk])
    client.post(reverse("purchasing:order", args=[po.pk]), {"expected_at": "2026-11-20"})
    detail = client.get(reverse("purchasing:detail", args=[po.pk])).content.decode()
    assert "Receive A Delivery" in detail and "Gildan 5000 tee" in detail
    form = {f"qty_{line.pk}": line.qty_ordered for line in po.lines.all()}
    client.post(reverse("purchasing:receive", args=[po.pk]), form)
    approved.refresh_from_db()
    assert approved.status == Status.READY_FOR_PRODUCTION
    printed = client.get(reverse("purchasing:print", args=[po.pk])).content.decode()
    assert po.po_number in printed and "SanMar" in printed


def test_the_job_page_shows_materials_and_from_stock(client, make_user, job_at):
    job = job_at(Status.MOCKUP_APPROVED)
    tee = item(job)
    client.force_login(make_user("production"))
    html = client.get(reverse("jobs:detail", args=[job.pk])).content.decode()
    assert "Materials: Not ordered" in html and "From stock" in html
    client.post(reverse("purchasing:item", args=[tee.pk]), {"what": "stock", "on": "1"})
    job.refresh_from_db()
    assert job.status == Status.READY_FOR_PRODUCTION


def test_production_sees_pos_but_no_build_button(client, make_user, manager, approved):
    po = build(manager, approved)
    client.force_login(make_user("production"))
    assert "Build a PO" not in client.get(reverse("purchasing:list")).content.decode()
    detail = client.get(reverse("purchasing:detail", args=[po.pk])).content.decode()
    assert po.po_number in detail and "Mark Ordered" not in detail
    assert client.get(reverse("purchasing:build")).status_code == 403


def test_search_finds_a_job_by_po_number(manager, approved):
    po = build(manager, approved)
    services.mark_ordered(po, user=manager, supplier_order_ref="SO-77")
    assert list(board.search(po.po_number, can_see_contacts=False)) == [approved]
    assert list(board.search(po.po_number[3:], can_see_contacts=False)) == [approved]
    assert list(board.search("SO-77", can_see_contacts=False)) == [approved]


def test_late_materials_are_flagged_on_the_board(client, manager, approved):
    po = build(manager, approved)
    services.mark_ordered(
        po, user=manager, expected_at=approved.created_at.date().replace(year=2020)
    )
    assert board.base_queryset().get(pk=approved.pk).materials_late
    client.force_login(manager)
    assert "Materials late" in client.get(reverse("jobs:list")).content.decode()


def test_the_timeline_shows_the_po(manager, approved):
    po = build(manager, approved)
    services.mark_ordered(po, user=manager)
    receive_all(po, manager)
    titles = [e.title for e in timeline.build(Job.objects.get(pk=approved.pk))]
    assert f"Purchase order {po.po_number} marked ordered" in titles
    assert f"Purchase order {po.po_number} delivery received" in titles


def test_lines_cant_be_removed_after_a_delivery(manager, approved):
    po = build(manager, approved)
    services.mark_ordered(po, user=manager)
    line = PurchaseOrderLine.objects.filter(purchase_order=po).first()
    services.receive(po, user=manager, quantities={line.pk: 1})
    with pytest.raises(ValidationError, match="close it short"):
        services.remove_line(line, user=manager)
