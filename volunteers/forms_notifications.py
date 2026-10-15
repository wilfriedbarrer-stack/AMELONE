"""Actions explicites du suivi privé des annonces et rappels."""

from django import forms


class RetryNotificationFailuresForm(forms.Form):
    action = forms.ChoiceField(
        choices=(("retry_failures", "Réessayer les échecs"),),
        initial="retry_failures", widget=forms.HiddenInput,
    )
