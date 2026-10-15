(() => {
  const button = document.querySelector('.navigation-toggle');
  const navigation = document.getElementById('site-navigation');
  if (!button || !navigation) return;
  const mobile = window.matchMedia('(max-width: 64rem)');
  const setOpen = (open, restoreFocus = false) => {
    button.setAttribute('aria-expanded', String(open));
    navigation.hidden = mobile.matches && !open;
    if (restoreFocus) button.focus();
  };
  button.addEventListener('click', () => {
    const open = button.getAttribute('aria-expanded') !== 'true';
    setOpen(open);
    if (open) navigation.querySelector('a, button, summary')?.focus();
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const account = document.querySelector('.account-menu[open]');
    if (account) {
      account.open = false;
      account.querySelector('summary')?.focus();
      return;
    }
    if (mobile.matches && button.getAttribute('aria-expanded') === 'true') {
      setOpen(false, true);
    }
  });
  const adapt = () => {
    const containedFocus = navigation.contains(document.activeElement);
    button.hidden = !mobile.matches;
    setOpen(false, mobile.matches && containedFocus);
  };
  mobile.addEventListener('change', adapt);
  adapt();
})();
