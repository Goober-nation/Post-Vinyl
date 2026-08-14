// setup.js — first-run onboarding wizard + post-wizard tutorial overlay.
//
// Gated on GET /api/setup/status at load: if the wizard isn't marked
// complete, show it; once it is, show the one-time tutorial overlay unless
// already dismissed. Config's "Re-run setup" button calls startWizard()
// directly after POST /api/setup/rerun resets both flags.

import * as api from "./api.js";
import { esc, showToast, confirmAction } from "./components.js";

const STEP_WELCOME = 0;
const STEP_NAVIDROME = 1;
const STEP_SLSKD = 2;
const STEP_LISTENBRAINZ = 3;
const STEP_FINISH = 4;
const STEP_COUNT = 5;

let step = STEP_WELCOME;
let navidromeJustSaved = false;
let slskdJustSaved = false;
let slskdSavedUsername = "";

function overlay(id) {
  return document.getElementById(id);
}

function showWizardOverlay() {
  const el = overlay("setup-wizard-overlay");
  if (el) el.hidden = false;
}

function hideWizardOverlay() {
  const el = overlay("setup-wizard-overlay");
  if (el) el.hidden = true;
}

function setStatus(text, isError) {
  const el = document.getElementById("setup-wizard-status");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", Boolean(isError));
}

function renderStepDots() {
  const el = document.getElementById("setup-wizard-steps");
  if (!el) return;
  let dots = "";
  for (let i = 0; i < STEP_COUNT; i++) {
    dots += `<span class="wizard-dot${i === step ? " active" : ""}${i < step ? " done" : ""}"></span>`;
  }
  el.innerHTML = dots;
}

function actionsRow(buttons) {
  const el = document.getElementById("setup-wizard-actions");
  if (!el) return;
  el.innerHTML = "";
  for (const btn of buttons) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = btn.className || "btn btn-ghost";
    b.textContent = btn.label;
    b.addEventListener("click", btn.onClick);
    el.appendChild(b);
  }
}

function goToStep(n) {
  step = n;
  setStatus("");
  renderCurrentStep();
}

function renderCurrentStep() {
  renderStepDots();
  const title = document.getElementById("setup-wizard-title");
  const body = document.getElementById("setup-wizard-body");
  if (!title || !body) return;

  if (step === STEP_WELCOME) {
    title.textContent = "Welcome to Post-Vinyl";
    body.innerHTML = `
      <p>Let's connect the services Post-Vinyl runs on top of: Navidrome (your music library),
      Soulseek (via slskd, for downloads), and optionally ListenBrainz (for recommendations).</p>
      <p class="hint">Each step can be skipped and finished later from Config → Re-run setup.</p>
    `;
    actionsRow([{ label: "Get started", className: "btn btn-primary", onClick: () => goToStep(STEP_NAVIDROME) }]);
  } else if (step === STEP_NAVIDROME) {
    renderNavidromeStep(title, body);
  } else if (step === STEP_SLSKD) {
    renderSlskdStep(title, body);
  } else if (step === STEP_LISTENBRAINZ) {
    renderListenBrainzStep(title, body);
  } else if (step === STEP_FINISH) {
    title.textContent = "All set";
    body.innerHTML = `<p>Setup is complete. You can revisit any of these steps later from Config → Re-run setup.</p>`;
    actionsRow([
      {
        label: "Finish",
        className: "btn btn-primary",
        onClick: async () => {
          try {
            await api.completeSetup();
          } catch (err) {
            showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
          }
          hideWizardOverlay();
          maybeShowTutorial();
        },
      },
    ]);
  }
}

// Restarts the app to pick up newly-saved Navidrome credentials without
// navigating the browser away from the wizard — a full page reload would
// re-fetch GET /api/setup/status and reopen the wizard at step 1, making it
// look like the just-saved credentials were lost. Polls for the app coming
// back, same as Config's restartAppNow(), but resumes the wizard in place
// instead of calling window.location.reload().
async function restartAppInPlace(btn) {
  if (
    !confirmAction(
      "Restart the app to apply the Navidrome credentials? The wizard stays open and continues once it's back."
    )
  ) {
    return;
  }
  if (btn) btn.disabled = true;
  setStatus("Restarting the app…");
  try {
    await api.restartApp();
  } catch (err) {
    setStatus(`${err.code || "ERROR"}: ${err.message}`, true);
    if (btn) btn.disabled = false;
    return;
  }
  await new Promise((resolve) => setTimeout(resolve, 1000));
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    try {
      await api.getSystemPing();
      showToast("App restarted — Navidrome credentials are now active");
      navidromeJustSaved = false;
      goToStep(STEP_SLSKD);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  setStatus("The app didn't come back after restart — check the container logs.", true);
  if (btn) btn.disabled = false;
}

function renderNavidromeStep(title, body) {
  title.textContent = "Navidrome account";
  body.innerHTML = `
    <p>Enter a username and password. If Navidrome has no admin yet, this creates one; otherwise it
    verifies these credentials against the existing admin.</p>
    <label>Username<input type="text" id="setup-navidrome-username" autocomplete="off"></label>
    <label>Password<input type="password" id="setup-navidrome-password" autocomplete="off"></label>
    <div id="setup-navidrome-restart-hint" class="hint" hidden>
      Saved — Navidrome will show as "disabled" in service health until the app restarts and picks
      up these credentials.
    </div>
  `;
  const buttons = [
    { label: "Skip", className: "btn btn-ghost", onClick: () => goToStep(STEP_SLSKD) },
    {
      label: "Save",
      className: "btn btn-primary",
      onClick: async (e) => {
        const username = document.getElementById("setup-navidrome-username").value.trim();
        const password = document.getElementById("setup-navidrome-password").value;
        if (!username || !password) {
          setStatus("Username and password are required", true);
          return;
        }
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          const result = await api.setupNavidrome(username, password);
          showToast(result.created ? "Navidrome admin account created" : "Navidrome credentials verified and saved");
          navidromeJustSaved = true;
          setStatus("Saved — restart the app to pick up these credentials, or continue and restart later.");
          renderNavidromeStep(title, body);
          return;
        } catch (err) {
          setStatus(`${err.code || "ERROR"}: ${err.message}`, true);
        } finally {
          btn.disabled = false;
        }
      },
    },
  ];
  if (navidromeJustSaved) {
    buttons.push({
      label: "Restart app now",
      className: "btn btn-danger",
      onClick: (e) => restartAppInPlace(e.currentTarget),
    });
  }
  buttons.push({ label: "Continue", className: "btn btn-ghost", onClick: () => goToStep(STEP_SLSKD) });
  actionsRow(buttons);
  if (navidromeJustSaved) {
    const hint = document.getElementById("setup-navidrome-restart-hint");
    if (hint) hint.hidden = false;
  }
}

function renderSlskdStep(title, body) {
  title.textContent = "Soulseek login";
  body.innerHTML = `
    <p>Pick a Soulseek username and password. Logging in for the first time registers it — if it's
    already taken by someone else, you'll be asked to pick a different one after slskd picks up the
    new login.</p>
    <label>Username<input type="text" id="setup-slskd-username" autocomplete="off" value="${esc(slskdSavedUsername)}"></label>
    <label>Password<input type="password" id="setup-slskd-password" autocomplete="off"></label>
    <div id="setup-slskd-restart-hint" class="hint" hidden>
      Saved for <strong>${esc(slskdSavedUsername)}</strong>. Run this on the host, then click
      "Check connection" — <code>restart</code> won't work here, it keeps slskd's old environment
      instead of re-reading .env, so the new login is silently ignored:
      <pre>docker compose up -d slskd</pre>
    </div>
  `;
  const buttons = [
    { label: "Skip", className: "btn btn-ghost", onClick: () => goToStep(STEP_LISTENBRAINZ) },
    {
      label: "Save",
      className: "btn btn-primary",
      onClick: async (e) => {
        const username = document.getElementById("setup-slskd-username").value.trim();
        const password = document.getElementById("setup-slskd-password").value;
        if (!username || !password) {
          setStatus("Username and password are required", true);
          return;
        }
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          await api.setupSlskd(username, password);
          slskdJustSaved = true;
          slskdSavedUsername = username;
          setStatus("Saved — run `docker compose up -d slskd`, then check the connection.");
          renderSlskdStep(title, body);
          return;
        } catch (err) {
          setStatus(`${err.code || "ERROR"}: ${err.message}`, true);
        } finally {
          btn.disabled = false;
        }
      },
    },
  ];
  if (slskdJustSaved) {
    buttons.push({
      label: "Check connection",
      className: "btn",
      onClick: async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        setStatus("Checking…");
        try {
          const result = await api.checkSetupSlskd();
          if (result.connected) {
            setStatus("Connected!");
            showToast("Soulseek connected");
            goToStep(STEP_LISTENBRAINZ);
          } else {
            setStatus(result.error ? `Not connected: ${result.error}` : "Not connected yet — did you run `docker compose up -d slskd`?", true);
          }
        } catch (err) {
          setStatus(`${err.code || "ERROR"}: ${err.message}`, true);
        } finally {
          btn.disabled = false;
        }
      },
    });
  }
  buttons.push({ label: "Continue", className: "btn btn-ghost", onClick: () => goToStep(STEP_LISTENBRAINZ) });
  actionsRow(buttons);
  if (slskdJustSaved) {
    const hint = document.getElementById("setup-slskd-restart-hint");
    if (hint) hint.hidden = false;
  }
}

function renderListenBrainzStep(title, body) {
  title.textContent = "ListenBrainz (optional)";
  body.innerHTML = `
    <p>ListenBrainz powers recommendations. Get a token from your
    <a href="https://listenbrainz.org/profile/" target="_blank" rel="noopener">ListenBrainz profile settings</a>,
    then enter it below along with your username. This step only saves the token — you'll enable
    recommendations separately in Config.</p>
    <label>Username<input type="text" id="setup-lb-username" autocomplete="off"></label>
    <label>Token<input type="password" id="setup-lb-token" autocomplete="off"></label>
  `;
  actionsRow([
    { label: "Skip", className: "btn btn-ghost", onClick: () => goToStep(STEP_FINISH) },
    {
      label: "Save & continue",
      className: "btn btn-primary",
      onClick: async (e) => {
        const username = document.getElementById("setup-lb-username").value.trim();
        const token = document.getElementById("setup-lb-token").value.trim();
        if (!username && !token) {
          goToStep(STEP_FINISH);
          return;
        }
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          const payload = {};
          if (username) payload.listenbrainz_username = username;
          if (token) payload.listenbrainz_token = token;
          await api.updateSecrets(payload);
          showToast("ListenBrainz credentials saved — restart required to take effect");
          goToStep(STEP_FINISH);
        } catch (err) {
          setStatus(`${err.code || "ERROR"}: ${err.message}`, true);
        } finally {
          btn.disabled = false;
        }
      },
    },
  ]);
}

export function startWizard() {
  step = STEP_WELCOME;
  navidromeJustSaved = false;
  slskdJustSaved = false;
  slskdSavedUsername = "";
  showWizardOverlay();
  renderCurrentStep();
}

function maybeShowTutorial() {
  const el = overlay("setup-tutorial-overlay");
  if (!el) return;
  const body = document.getElementById("setup-tutorial-body");
  if (body) {
    body.innerHTML = `
      <ul>
        <li><strong>Search</strong> — find recordings/albums via MusicBrainz and queue downloads.</li>
        <li><strong>Soulseek</strong> — run manual Soulseek searches directly.</li>
        <li><strong>Transfers</strong> — watch active and finished downloads.</li>
        <li><strong>Recs</strong> — Comfort Zone, Fresh Picks, and Deep Cuts recommendation pulls.</li>
        <li><strong>Config</strong> — settings, secrets, service health, and re-run this setup wizard.</li>
      </ul>
    `;
  }
  el.hidden = false;
}

async function dismissTutorial() {
  const el = overlay("setup-tutorial-overlay");
  if (el) el.hidden = true;
  try {
    await api.dismissTutorial();
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  }
}

export async function rerunSetup(btn) {
  if (btn) btn.disabled = true;
  try {
    await api.rerunSetup();
    startWizard();
  } catch (err) {
    showToast(`${err.code || "ERROR"}: ${err.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function initSetup() {
  document.getElementById("btn-tutorial-dismiss")?.addEventListener("click", dismissTutorial);
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("#config-rerun-setup-btn");
    if (btn) rerunSetup(btn);
  });

  try {
    const status = await api.getSetupStatus();
    if (!status.wizard_completed) {
      startWizard();
    } else if (!status.tutorial_dismissed) {
      maybeShowTutorial();
    }
  } catch {
    // Setup status is best-effort — never block the rest of the app on it.
  }
}
