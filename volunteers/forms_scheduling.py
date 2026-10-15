"""Validation finale des horaires d'un ensemble de créneaux."""

from django.forms.models import BaseInlineFormSet

from .scheduling import validate_station_schedule


class ScheduleValidatedInlineFormSet(BaseInlineFormSet):
    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        # A coordinated edit must compare final rows rather than each old peer.
        form.instance._defer_schedule_validation = True
        return form

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        slots = [
            form.instance for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        validate_station_schedule(slots)
