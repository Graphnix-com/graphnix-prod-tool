from django import forms

from apps.jobs.flow import PATH
from apps.jobs.gates import GATEABLE, LEVELS
from apps.jobs.models import Status

CHOICES = [("", "No Gate"), ("deposit", "Deposit First"), ("paid", "Paid In Full First")]


class PaymentGatesForm(forms.Form):
    """One choice per stage after Invoiced (FR-07)."""

    def __init__(self, *args, current: dict, **kwargs):
        super().__init__(*args, **kwargs)
        for stage in GATEABLE:
            self.fields[stage] = forms.ChoiceField(
                label=Status(stage).label,
                choices=CHOICES,
                required=False,
                initial=current.get(stage, ""),
            )

    def gates(self) -> dict[str, str]:
        chosen = {s: self.cleaned_data[s] for s in GATEABLE if self.cleaned_data.get(s) in LEVELS}
        return dict(sorted(chosen.items(), key=lambda kv: PATH.index(kv[0])))
