"""Quote Approved is merged into Invoiced: jobs there now move to Invoiced, with a line in
their history saying why. Jobs on hold that would return to Quote Approved return to
Invoiced. Older history keeps its "Quote Approved" entries."""

from django.db import migrations
from django.utils import timezone

NOTE = "Quote Approved merged into Invoiced"


def forward(apps, schema_editor):
    Job = apps.get_model("jobs", "Job")
    JobStatusHistory = apps.get_model("jobs", "JobStatusHistory")
    now = timezone.now()
    for job in Job.objects.filter(status="quote_approved"):
        job.status = "invoiced"
        job.status_changed_at = now
        job.quote_approved_at = job.quote_approved_at or now
        job.save(update_fields=["status", "status_changed_at", "quote_approved_at"])
        JobStatusHistory.objects.create(
            job=job,
            from_status="quote_approved",
            to_status="invoiced",
            source="system",
            note=NOTE,
            changed_at=now,
        )
    Job.objects.filter(resume_status="quote_approved").update(resume_status="invoiced")


class Migration(migrations.Migration):
    dependencies = [("jobs", "0007_alter_job_source")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
