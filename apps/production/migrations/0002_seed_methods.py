"""The decoration methods Graphnix uses (Step 1 spec, question 11, answered 7 Nov 2026).

Each method gets its one station. Admins add more methods later from the admin or the
task form.
"""

from django.db import migrations

METHODS = [
    ("screen-print", "Screen print"),
    ("embroidery", "Embroidery"),
    ("heat-press", "Heat press"),
    ("dtf", "Direct to film (DTF)"),
    ("laser", "Laser"),
    ("vinyl-stickers", "Vinyl stickers"),
    ("vinyl-decals", "Vinyl decals"),
    ("business-cards", "Business cards"),
    ("banners", "Banners"),
    ("promo", "General promotional"),
]


def seed(apps, schema_editor):
    DecorationMethod = apps.get_model("production", "DecorationMethod")
    Station = apps.get_model("production", "Station")
    for code, name in METHODS:
        method, _ = DecorationMethod.objects.get_or_create(code=code, defaults={"name": name})
        if not Station.objects.filter(decoration_method=method).exists():
            Station.objects.create(name=method.name, decoration_method=method)


class Migration(migrations.Migration):
    dependencies = [("production", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
