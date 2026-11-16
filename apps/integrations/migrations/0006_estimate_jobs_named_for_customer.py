"""Jobs made for a Square estimate are named for the customer, not "Estimate 000685": the
board already shows the estimate number beside the title. Renames the ones made before."""

from django.db import migrations


def forward(apps, schema_editor):
    SquareEstimate = apps.get_model("integrations", "SquareEstimate")
    for estimate in SquareEstimate.objects.filter(job__isnull=False).select_related(
        "job__customer"
    ):
        job = estimate.job
        if job.title != f"Estimate {estimate.number}":
            continue  # someone gave it a real title
        name = " ".join((estimate.customer_name or job.customer.display_name or "").split())
        if name:
            job.title = name[:200]
            job.save(update_fields=["title"])


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0005_store_order_job_numbers"),
        ("jobs", "0009_retire_mockup_in_progress"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
