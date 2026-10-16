"""Actions explicites du suivi privé des annonces et rappels."""

from django import forms


class RetryNotificationFailuresForm(forms.Form):
    action = forms.ChoiceField(
        choices=(("retry_failures", "Réessayer les échecs"),),
        initial="retry_failures", widget=forms.HiddenInput,
    )


class WhatsAppReminderStatusForm(forms.Form):
    action = forms.ChoiceField(
        choices=(("mark_shared", "Déclarer le partage"), ("reset_shared", "Remettre l’alerte")),
        widget=forms.HiddenInput,
    )
    registration_closes_at = forms.DateTimeField(widget=forms.HiddenInput)
