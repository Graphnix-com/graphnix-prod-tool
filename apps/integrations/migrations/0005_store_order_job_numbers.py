"""Store-order jobs made before this change take their order number as the job number."""

from django.db import migrations


def forwards(apps, schema_editor):
    SquareOrder = apps.get_model("integrations", "SquareOrder")
    Job = apps.get_model("jobs", "Job")
    for order in SquareOrder.objects.filter(job__isnull=False).exclude(reference=""):
        job = order.job
        if job.square_number or Job.objects.filter(square_number=order.reference).exists():
            continue
        job.square_number = order.reference
        job.save(update_fields=["square_number"])


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0004_square_estimates"),
        ("jobs", "0006_title_case_labels"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
