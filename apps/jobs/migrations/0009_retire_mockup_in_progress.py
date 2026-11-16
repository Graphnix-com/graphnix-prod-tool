"""Mock-up In Progress is retired: Invoiced goes straight to Mock-up Sent. Jobs there, whose
mock-up isn't sent yet, move to Invoiced with a line in their history; jobs on hold that
would return there return to Invoiced. Older history keeps its entries."""

from django.db import migrations
from django.utils import timezone

NOTE = "Mock-up In Progress retired: the mock-up isn't sent yet"


def forward(apps, schema_editor):
    Job = apps.get_model("jobs", "Job")
    JobStatusHistory = apps.get_model("jobs", "JobStatusHistory")
    now = timezone.now()
    for job in Job.objects.filter(status="mockup_in_progress"):
        job.status = "invoiced"
        job.status_changed_at = now
        job.save(update_fields=["status", "status_changed_at"])
        JobStatusHistory.objects.create(
            job=job,
            from_status="mockup_in_progress",
            to_status="invoiced",
            source="system",
            note=NOTE,
            changed_at=now,
        )
    Job.objects.filter(resume_status="mockup_in_progress").update(resume_status="invoiced")


class Migration(migrations.Migration):
    dependencies = [("jobs", "0008_merge_quote_approved_into_invoiced")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
