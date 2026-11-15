import re

from django import forms

from apps.production.models import DecorationMethod, ProductionTask

from .editing import DETAIL_FIELDS
from .models import ONE_SIZE, STANDARD_SIZES, Job, JobItem
from .money import no_amounts


class DateInput(forms.DateInput):
    input_type = "date"


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


class ProductionReadsMixin:
    """These fields are shown to Production, so they may not hold prices (NFR-02)."""

    production_reads: tuple[str, ...] = ()

    def clean(self):
        data = super().clean()
        for name in self.production_reads:
            try:
                no_amounts(data.get(name))
            except forms.ValidationError as exc:
                self.add_error(name, exc)
        return data


class JobDetailsForm(ProductionReadsMixin, forms.ModelForm):
    production_reads = ("title",)

    class Meta:
        model = Job
        fields = DETAIL_FIELDS
        widgets = {
            "due_date": DateInput(),
            "quote_sent_at": DateTimeInput(),
            "quote_approved_at": DateTimeInput(),
        }
        help_texts = {
            "shared_drive_path": "Paste the job's folder path on the NAS.",
            "square_number": "The job ID Square generates.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].queryset = self.instance.customer.contacts.all()


_OTHER_SIZE = re.compile(r"^\s*([A-Za-z0-9]{1,10})\s*[=:x×]\s*(\d+)\s*$")


class ItemForm(ProductionReadsMixin, forms.ModelForm):
    """One line. Garments take a quantity per size; anything else takes one quantity."""

    production_reads = ("item_no", "description", "color", "notes")

    quantity = forms.IntegerField(min_value=1, required=False, help_text="For items without sizes.")
    other_sizes = forms.CharField(
        required=False,
        help_text="Sizes not in the grid, e.g. LT=2, 6XL=1",
    )

    class Meta:
        model = JobItem
        fields = ["item_no", "description", "color", "is_sized", "customer_supplied", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}
        help_texts = {"description": "Brand and style, e.g. Gildan 5000 tee"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = {}
        if self.instance.pk:
            current = {s.size: s.quantity for s in self.instance.sizes.all()}
        for size in STANDARD_SIZES:
            self.fields[f"size_{size}"] = forms.IntegerField(
                label=size, min_value=0, required=False, initial=current.get(size)
            )
        if self.instance.pk:
            if not self.instance.is_sized:
                self.fields["quantity"].initial = current.get(ONE_SIZE)
            extra = {k: v for k, v in current.items() if k not in STANDARD_SIZES + [ONE_SIZE]}
            self.fields["other_sizes"].initial = ", ".join(f"{k}={v}" for k, v in extra.items())

    @property
    def size_fields(self):
        return [self[f"size_{size}"] for size in STANDARD_SIZES]

    def clean(self):
        data = super().clean()
        sizes: dict[str, int] = {}
        if data.get("is_sized"):
            for size in STANDARD_SIZES:
                if data.get(f"size_{size}"):
                    sizes[size] = data[f"size_{size}"]
            for part in filter(None, (data.get("other_sizes") or "").split(",")):
                match = _OTHER_SIZE.match(part)
                if not match:
                    self.add_error("other_sizes", f"Couldn't read “{part.strip()}”. Use SIZE=QTY.")
                    continue
                if int(match.group(2)):
                    sizes[match.group(1).upper()] = int(match.group(2))
            if not sizes and not self.errors:
                raise forms.ValidationError("Enter a quantity for at least one size.")
        else:
            if not data.get("quantity"):
                self.add_error("quantity", "Enter the quantity.")
            else:
                sizes[ONE_SIZE] = data["quantity"]
        self.sizes = sizes
        return data


class TaskForm(ProductionReadsMixin, forms.ModelForm):
    production_reads = ("placement", "notes")
    new_method = forms.CharField(
        required=False,
        label="Or add a new method",
        help_text="Adds it to the list for everyone.",
    )

    class Meta:
        model = ProductionTask
        fields = ["job_item", "decoration_method", "placement", "notes", "outsourced", "sequence"]
        labels = {"job_item": "Item", "outsourced": "Done by a supplier"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "notes": "Ink colours, stitch count, transfer size, anything the station needs.",
            "placement": "e.g. front, left chest, sleeve",
        }

    def __init__(self, *args, job, can_add_method=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["job_item"].queryset = job.items.all()
        self.fields["job_item"].required = True
        self.fields["decoration_method"].queryset = DecorationMethod.objects.filter(is_active=True)
        self.fields["decoration_method"].required = False
        self.fields["sequence"].required = False
        self.fields["sequence"].help_text = "Order on this job; blank puts it last."
        if not can_add_method:
            del self.fields["new_method"]

    def clean(self):
        data = super().clean()
        if not data.get("decoration_method") and not data.get("new_method"):
            self.add_error("decoration_method", "Pick a method.")
        return data


class NoteForm(forms.Form):
    body = forms.CharField(
        label="Add a note", widget=forms.Textarea(attrs={"rows": 3}), validators=[no_amounts]
    )
