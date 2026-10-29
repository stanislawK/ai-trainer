// Add to home screen (PRD 0009 F22, ticket #307): shows the chat's install card and the Settings
// entry in the state that fits this browser. The server renders every variant hidden. Loaded on
// every page, so the `beforeinstallprompt` event is kept whichever page it fires on; there is no
// service worker (ADR-0019). A dismissal or install is stored in localStorage["install-card"],
// like the theme, so the card never comes back in this browser.
(function () {
  var STORAGE_KEY = "install-card";
  var ua = navigator.userAgent;
  // Caught by the inline script in base.html's <head> if it fired before this file ran.
  var deferredPrompt = window.__installPrompt || null;
  // The browser offered its dialog on this page; it opens once, so after a cancel Install waits,
  // disabled, until the browser offers it again.
  var offered = deferredPrompt !== null;

  function stored() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
  }

  function store(value) {
    try {
      localStorage.setItem(STORAGE_KEY, value);
    } catch (e) {
      // Storage unavailable (blocked, private mode): the card still hides on this page.
    }
  }

  function standalone() {
    return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  }

  var ios = /iPhone|iPod|iPad/.test(ua);
  var android = /Android/.test(ua);
  var phone = /iPhone|iPod/.test(ua) || (android && /Mobile/.test(ua));
  // An in-app browser (a WebView inside another app) can't add to the home screen.
  var inApp = /; wv\)|FBAN|FBAV|Instagram|Line\/|GSA\//.test(ua);

  // Picks the Settings variant: the mock differs between phones and desktop, and names a browser
  // that can install per platform.
  function platform() {
    return ios ? "ios" : android ? "android" : "desktop";
  }

  // One of "prompt", "ios-safari", "ios-chrome", "installed", "unsupported", or null while
  // the browser may still offer its install dialog (no `beforeinstallprompt` yet), or installs
  // only from its own menu (Firefox on Android, Safari on macOS), which the mock leaves out.
  function state() {
    if (standalone()) {
      return "installed";
    }
    // The browser offering its dialog means the app isn't installed (any more).
    if (offered) {
      return "prompt";
    }
    if (stored() === "installed") {
      return "installed";
    }
    if (inApp) {
      return "unsupported";
    }
    if (ios) {
      if (/CriOS/.test(ua)) {
        return "ios-chrome";
      }
      return /Safari/.test(ua) && !/FxiOS|EdgiOS|OPiOS/.test(ua) ? "ios-safari" : "unsupported";
    }
    if (/Firefox/.test(ua) && !android) {
      return "unsupported";
    }
    return null;
  }

  function fits(element, attribute, value) {
    var on = element.getAttribute("data-install-on");
    return element.getAttribute(attribute) === value &&
      (on === null || on.split(" ").indexOf(platform()) !== -1);
  }

  function render() {
    var current = state();
    var stepsOrPrompt = ["prompt", "ios-safari", "ios-chrome"].indexOf(current) !== -1;
    document.querySelectorAll("[data-install-card]").forEach(function (card) {
      // Stored either way, the card is gone for good in this browser.
      card.hidden = !(phone && stepsOrPrompt && stored() === null);
      card.querySelectorAll("[data-install-variant]").forEach(function (variant) {
        variant.hidden = !fits(variant, "data-install-variant", current);
      });
    });
    document.querySelectorAll("[data-install-settings]").forEach(function (section) {
      section.hidden = current === null;
      section.querySelectorAll("[data-install-state]").forEach(function (variant) {
        variant.hidden = !fits(variant, "data-install-state", current);
      });
    });
    document.querySelectorAll("[data-install-action]").forEach(function (button) {
      button.disabled = deferredPrompt === null;
    });
  }

  function installed() {
    deferredPrompt = null;
    offered = false;
    store("installed");
    render();
  }

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
    offered = true;
    render();
  });

  window.addEventListener("appinstalled", installed);

  // Delegated on `document`: the chat swaps itself through htmx, bringing a fresh card.
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-install-dismiss]")) {
      store("dismissed");
      render();
      return;
    }
    if (event.target.closest("[data-install-action]") && deferredPrompt) {
      var prompt = deferredPrompt;
      // The event's dialog opens only once.
      deferredPrompt = null;
      prompt.prompt();
      prompt.userChoice.then(function (choice) {
        if (choice.outcome === "accepted") {
          installed();
        } else {
          // Cancelling the browser's dialog dismisses the card too.
          store("dismissed");
          render();
        }
      }, render);
    }
  });

  document.addEventListener("htmx:after:swap", render);

  render();
})();
