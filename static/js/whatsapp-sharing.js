(() => {
  const shareableMessages = new Set();
  document.querySelectorAll('[data-whatsapp-share]').forEach(button => {
    const text = document.getElementById(button.dataset.whatsappShare);
    const feedback = document.getElementById(button.dataset.whatsappFeedback);
    if (!text || !feedback || !window.isSecureContext || typeof navigator.share !== 'function') return;
    try {
      if (typeof navigator.canShare === 'function' && !navigator.canShare({ text: text.value })) return;
    } catch {
      return;
    }
    button.hidden = false;
    shareableMessages.add(button.dataset.whatsappShare);
    button.addEventListener('click', async () => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        await navigator.share({ text: text.value });
        feedback.textContent = 'Vérifiez le message dans WhatsApp avant de l’envoyer.';
      } catch (error) {
        if (error?.name === 'AbortError') return;
        feedback.textContent = 'Le partage est indisponible. Utilisez “Copier le message”, puis collez-le dans WhatsApp.';
      } finally {
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll('[data-whatsapp-copy]').forEach(button => {
    const text = document.getElementById(button.dataset.whatsappCopy);
    const feedback = document.getElementById(button.dataset.whatsappFeedback);
    if (!text || !feedback) return;
    button.hidden = false;
    if (!shareableMessages.has(button.dataset.whatsappCopy)) {
      button.classList.remove('button-secondary');
      button.classList.add('button-primary');
    }
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
