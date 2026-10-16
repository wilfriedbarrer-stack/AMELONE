(() => {
  document.querySelectorAll('[data-whatsapp-copy]').forEach(button => {
    const text = document.getElementById(button.dataset.whatsappCopy);
    const feedback = document.getElementById(button.dataset.whatsappFeedback);
    if (!text || !feedback) return;
    button.hidden = false;
    const selectText = () => {
      text.focus();
      text.select();
      text.setSelectionRange(0, text.value.length);
      feedback.textContent = 'Texte sélectionné : copiez-le avec Ctrl+C, ⌘C ou le menu Copier de votre appareil.';
    };
    button.addEventListener('click', async () => {
      if (!navigator.clipboard?.writeText) {
        selectText();
        return;
      }
      try {
        await navigator.clipboard.writeText(text.value);
        feedback.textContent = 'Message copié. Collez-le dans le groupe WhatsApp des bénévoles.';
      } catch {
        selectText();
      }
    });
  });
})();
