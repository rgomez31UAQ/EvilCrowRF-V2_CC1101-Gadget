/* ============================================================
   EvilCrow RF V2 (v1.1) — Web Flasher Application Logic
   Fetches releases from GitHub, builds ESP Web Tools manifest,
   handles changelog display and APK downloads.

   CORS NOTE: GitHub does NOT send Access-Control-Allow-Origin
   headers on release asset downloads — neither via
   browser_download_url nor the API redirect target
   (release-assets.githubusercontent.com).
   See: https://github.com/esphome/esp-web-tools/issues/521
   Solution: download firmware via a CORS proxy, convert to
   a local Blob URL, and feed that to ESP Web Tools.
   ============================================================ */

const GITHUB_REPO  = 'Senape3000/EvilCrowRF-V2';
const GITHUB_API   = `https://api.github.com/repos/${GITHUB_REPO}/releases`;
const GITHUB_URL   = `https://github.com/${GITHUB_REPO}`;
const DONATE_URL   = 'https://ko-fi.com/senape3000';

// ─── CORS proxy for GitHub release asset downloads ─────────────────
// GitHub release downloads are blocked by CORS in browsers.
// We use CORS proxy services to fetch them as blobs.
//
// RECOMMENDED: Deploy the Cloudflare Worker in cloudflare-worker/cors-proxy.js
// (free, 100K req/day) and set CORS_WORKER_URL below.
// Free public proxies are used as fallback but may be unreliable.

// Set this to your Cloudflare Worker URL after deploying cors-proxy.js
// Example: 'https://evilcrow-cors.yourname.workers.dev'
const CORS_WORKER_URL = 'https://evilcrow-cors.anx-hack.workers.dev';

// Fallback public CORS proxies (tried in order if worker is not set or fails)
const CORS_PROXIES = [
  url => `https://corsproxy.io/?${encodeURIComponent(url)}`,
  url => `https://api.allorigins.win/raw?url=${encodeURIComponent(url)}`,
];

/**
 * Fetch a URL that is blocked by CORS, trying multiple proxy services.
 * Returns { resp, method } on success, or throws on total failure.
 */
async function fetchViaCorsProxy(url) {
  // 1. Try direct fetch (future-proof: if GitHub ever adds CORS)
  try {
    const resp = await fetch(url, { mode: 'cors' });
    if (resp.ok) return { resp, method: 'direct' };
  } catch (_) { /* expected: CORS block */ }

  // 2. Try Cloudflare Worker (most reliable, user-controlled)
  if (CORS_WORKER_URL) {
    try {
      const base = CORS_WORKER_URL.replace(/\/+$/, ''); // strip trailing slashes
      const workerUrl = `${base}/?url=${encodeURIComponent(url)}`;
      const resp = await fetch(workerUrl);
      if (resp.ok) return { resp, method: 'Cloudflare Worker' };
    } catch (_) { /* worker unreachable, try public proxies */ }
  }

  // 3. Try each public CORS proxy in order
  for (let i = 0; i < CORS_PROXIES.length; i++) {
    try {
      const proxyUrl = CORS_PROXIES[i](url);
      const resp = await fetch(proxyUrl);
      if (resp.ok) return { resp, method: `proxy #${i + 1}` };
    } catch (_) { /* try next */ }
  }

  throw new Error('All download methods failed (CORS proxies unreachable)');
}

// Firmware file naming: evilcrow-v2-fw-v{version}-full.bin
const FW_FULL_RE   = /evilcrow-v2-fw-v[\d.]+-full\.bin$/i;
const FW_MD5_RE    = /evilcrow-v2-fw-v[\d.]+-full\.bin\.md5$/i;
// APK naming: EvilCrowRF-v{version}.apk
const APK_RE       = /EvilCrowRF-v[\d.]+\.apk$/i;

// ─── State ─────────────────────────────────────────────────────────
let allReleases  = [];   // raw GitHub release objects
let fwReleases   = [];   // filtered: only those with a -full.bin asset
let appReleases  = [];   // filtered: only those with an .apk asset
let selectedFw   = null; // currently selected firmware release

// ─── DOM refs ──────────────────────────────────────────────────────
const versionSelect   = document.getElementById('version-select');
const statusDot       = document.getElementById('status-dot');
const statusText      = document.getElementById('status-text');
const changelogPanel  = document.getElementById('changelog-content');
const terminalBody    = document.getElementById('terminal-body');
const espInstallBtn   = document.getElementById('esp-install-btn');
const apkBtn          = document.getElementById('apk-download-btn');
const fwInfoTag       = document.getElementById('fw-info-tag');
const fwInfoSize      = document.getElementById('fw-info-size');
const fwInfoDate      = document.getElementById('fw-info-date');

// ─── Terminal logging (macOS style) ────────────────────────────────
function termLog(msg, type = 'default') {
  const line = document.createElement('div');
  line.className = `terminal-line ${type}`;
  line.textContent = msg;
  terminalBody.appendChild(line);
  terminalBody.scrollTop = terminalBody.scrollHeight;
}

function termClear() {
  terminalBody.innerHTML = '';
}

// Typing animation helper
async function termType(msg, type = 'info', speed = 18) {
  const line = document.createElement('div');
  line.className = `terminal-line ${type}`;
  terminalBody.appendChild(line);
  for (let i = 0; i < msg.length; i++) {
    line.textContent += msg[i];
    terminalBody.scrollTop = terminalBody.scrollHeight;
    await new Promise(r => setTimeout(r, speed));
  }
}

// ─── Status helpers ────────────────────────────────────────────────
function setStatus(state, text) {
  statusDot.className = `status-dot ${state}`;
  statusText.textContent = text;
}

// ─── Format bytes ──────────────────────────────────────────────────
function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(2) + ' MB';
}

// ─── Fetch all releases ────────────────────────────────────────────
async function fetchReleases() {
  setStatus('loading', 'Fetching releases...');
  termClear();
  await termType('$ fetching releases from GitHub...', 'info', 12);

  try {
    const resp = await fetch(GITHUB_API, {
      headers: { 'Accept': 'application/vnd.github.v3+json' }
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    allReleases = await resp.json();

    // Filter firmware releases (those with -full.bin)
    fwReleases = allReleases.filter(r =>
      r.assets.some(a => FW_FULL_RE.test(a.name))
    );

    // Filter app releases (those with .apk)
    appReleases = allReleases.filter(r =>
      r.assets.some(a => APK_RE.test(a.name))
    );

    termLog(`  ✓ Found ${allReleases.length} release(s) total`, 'success');
    termLog(`  ✓ ${fwReleases.length} firmware release(s) with full binary`, 'info');
    termLog(`  ✓ ${appReleases.length} app release(s) with APK`, 'info');

    // NOTE: We intentionally do NOT fallback to OTA binaries here.
    // If there are no releases with a "-full.bin" asset we want
    // to show a single explicit state: NO FIRMWARE

    populateVersionSelect();
    populateApkButton();
    setStatus('online', 'Connected to GitHub');

  } catch (err) {
    termLog(`  ✗ Error: ${err.message}`, 'error');
    setStatus('error', 'Failed to fetch releases');
    termLog('  Tip: Check your internet connection or try again later.', 'warn');
  }
}

// ─── Populate version dropdown ─────────────────────────────────────
function populateVersionSelect() {
  versionSelect.innerHTML = '';

  if (fwReleases.length === 0) {
    const opt = document.createElement('option');
    opt.textContent = 'NO FIRMWARE';
    opt.disabled = true;
    versionSelect.appendChild(opt);
    // Disable install button and clear manifest
    try {
      if (espInstallBtn) {
        espInstallBtn.setAttribute('disabled', '');
        espInstallBtn.removeAttribute && espInstallBtn.removeAttribute('manifest');
      }
    } catch (e) { /* ignore */ }
    setStatus('error', 'NO FIRMWARE');
    termLog('  ✗ No full firmware binaries available (NO FIRMWARE)', 'error');
    return;
  }

  fwReleases.forEach((rel, idx) => {
    const opt = document.createElement('option');
    opt.value = idx;
    const tag = rel.tag_name || rel.name;
    const date = rel.published_at ? new Date(rel.published_at).toLocaleDateString() : '';
    opt.textContent = `${tag}${idx === 0 ? ' (latest)' : ''} — ${date}`;
    versionSelect.appendChild(opt);
  });

  // Auto-select latest
  versionSelect.value = '0';
  onVersionChange();
}

// ─── Handle version change ─────────────────────────────────────────
async function onVersionChange() {
  const idx = parseInt(versionSelect.value, 10);
  selectedFw = fwReleases[idx];
  if (!selectedFw) return;

  // Find the full.bin asset (strict: do NOT fallback to OTA)
  const fullAsset = selectedFw.assets.find(a => FW_FULL_RE.test(a.name));

  // Update info
  if (fwInfoTag)  fwInfoTag.textContent  = selectedFw.tag_name || selectedFw.name;
  if (fwInfoSize) fwInfoSize.textContent = fullAsset ? formatBytes(fullAsset.size) : '—';
  if (fwInfoDate) fwInfoDate.textContent = selectedFw.published_at
    ? new Date(selectedFw.published_at).toLocaleDateString()
    : '—';

  // Build dynamic manifest for ESP Web Tools
  if (fullAsset) {
    try { espInstallBtn && espInstallBtn.removeAttribute && espInstallBtn.removeAttribute('disabled'); } catch (e) {}
    termLog(`  Selected: ${fullAsset.name} (${formatBytes(fullAsset.size)})`, 'info');
    await buildManifest(fullAsset);
  } else {
    // This should not normally happen because we filter releases by full.bin,
    // but handle it defensively: show NO FIRMWARE and disable install.
    try {
      if (espInstallBtn) espInstallBtn.setAttribute('disabled', '');
      espInstallBtn && espInstallBtn.removeAttribute && espInstallBtn.removeAttribute('manifest');
    } catch (e) { /* ignore */ }
    termLog('  ✗ Selected release has no -full.bin asset — NO FIRMWARE', 'error');
    setStatus('error', 'NO FIRMWARE');
  }

  // Update changelog
  renderChangelog();
}

versionSelect.addEventListener('change', onVersionChange);

// ─── Build ESP Web Tools manifest dynamically ──────────────────────
// Download firmware binary via CORS proxy → Blob URL → ESP Web Tools.
// The .bin file stays ONLY in the GitHub release; no copies elsewhere.
async function buildManifest(asset) {
  termLog(`  ↓ Downloading firmware (${formatBytes(asset.size)})...`, 'info');
  setStatus('loading', 'Downloading firmware...');

  let fwBlob = null;

  try {
    // Download the firmware binary via CORS proxy
    const { resp, method } = await fetchViaCorsProxy(asset.browser_download_url);
    fwBlob = await resp.blob();
    termLog(`  ✓ Firmware downloaded ${method} (${formatBytes(fwBlob.size)})`, 'success');
  } catch (err) {
    termLog(`  ✗ Firmware download failed: ${err.message}`, 'error');
    termLog(`  → GitHub blocks CORS on release asset downloads.`, 'warn');
    termLog(`  → Try refreshing or use a different browser.`, 'warn');
    setStatus('error', 'Download failed');
    try { espInstallBtn && espInstallBtn.setAttribute('disabled', ''); } catch (_) {}
    return;
  }

  // Validate: firmware blob should be > 100KB to be plausible
  if (fwBlob.size < 102400) {
    termLog(`  ⚠ Warning: firmware is only ${formatBytes(fwBlob.size)} — may be corrupt`, 'warn');
  }

  // Create local Blob URL for ESP Web Tools (same-origin, no CORS issues)
  const fwBlobUrl = URL.createObjectURL(fwBlob);

  const manifest = {
    name: 'EvilCrow RF V2',
    version: selectedFw.tag_name || selectedFw.name || 'unknown',
    funding_url: DONATE_URL,
    new_install_prompt_erase: true,
    builds: [
      {
        chipFamily: 'ESP32',
        improv: false,
        parts: [
          { path: fwBlobUrl, offset: 0 }
        ]
      }
    ]
  };

  const json = JSON.stringify(manifest);
  const manifestBlob = new Blob([json], { type: 'application/json' });
  const manifestUrl  = URL.createObjectURL(manifestBlob);

  // Set manifest on the esp-web-install-button element
  espInstallBtn.setAttribute('manifest', manifestUrl);
  espInstallBtn.manifest = manifestUrl;

  setStatus('online', 'Ready to flash');
  termLog(`  ✓ Manifest ready — click INSTALL to flash`, 'success');
}

// ─── APK download button ──────────────────────────────────────────
function populateApkButton() {
  if (!apkBtn) return;

  // Find latest release with APK
  let apkAsset = null;
  let apkRelease = null;

  for (const rel of allReleases) {
    const found = rel.assets.find(a => APK_RE.test(a.name));
    if (found) {
      apkAsset = found;
      apkRelease = rel;
      break;
    }
  }

  if (apkAsset) {
    apkBtn.href = apkAsset.browser_download_url;
    apkBtn.classList.remove('hidden');
    const verSpan = apkBtn.querySelector('.apk-version');
    if (verSpan) verSpan.textContent = apkRelease.tag_name || '';
    termLog(`  ✓ Android APK available: ${apkAsset.name}`, 'info');
  } else {
    apkBtn.classList.add('hidden');
    termLog('  — No Android APK found in releases', 'default');
  }
}

// ─── Render Changelog ──────────────────────────────────────────────
async function renderChangelog() {
  if (!changelogPanel) return;

  let changelogData = null;

  // Try to fetch changelog.json from release assets (via CORS proxy)
  const releasesToCheck = selectedFw ? [selectedFw, ...allReleases] : allReleases;
  for (const rel of releasesToCheck) {
    const clAsset = rel.assets.find(a => a.name === 'changelog.json');
    if (!clAsset) continue;
    try {
      const { resp } = await fetchViaCorsProxy(clAsset.browser_download_url);
      changelogData = await resp.json();
      break;
    } catch (_) { /* CORS proxy failed, fall through to release body */ }
  }

  // Fallback: use release body text (already available from API, no CORS issue)
  if (!changelogData) {
    changelogPanel.innerHTML = '';
    fwReleases.forEach(rel => {
      const div = document.createElement('div');
      div.className = 'changelog-version';
      div.innerHTML = `
        <div class="changelog-version-header">
          <span class="changelog-version-tag">${rel.tag_name || rel.name}</span>
          <span class="changelog-version-date">${rel.published_at ? new Date(rel.published_at).toLocaleDateString() : ''}</span>
        </div>
        <div class="changelog-item">
          <span>${rel.body ? rel.body.replace(/\n/g, '<br>') : 'No changelog available.'}</span>
        </div>
      `;
      changelogPanel.appendChild(div);
    });
    return;
  }

  // Render structured changelog
  changelogPanel.innerHTML = '';
  const fwChanges = changelogData.firmware || [];

  fwChanges.forEach(entry => {
    const div = document.createElement('div');
    div.className = 'changelog-version';

    let changesHtml = '';
    (entry.changes || []).forEach(c => {
      const badgeClass = c.type || 'fix';
      changesHtml += `
        <div class="changelog-item">
          <span class="changelog-badge ${badgeClass}">${c.type}</span>
          <span>${c.text}</span>
        </div>
      `;
    });

    div.innerHTML = `
      <div class="changelog-version-header">
        <span class="changelog-version-tag">v${entry.version}</span>
        <span class="changelog-version-date">${entry.date || ''}</span>
      </div>
      ${changesHtml}
    `;
    changelogPanel.appendChild(div);
  });
}

// ─── Startup sequence ──────────────────────────────────────────────
async function init() {
  await termType('╔══════════════════════════════════════════╗', 'default', 4);
  await termType('║   EvilCrow RF V2 — Web Flasher           ║', 'info', 4);
  await termType('╚══════════════════════════════════════════╝', 'default', 4);
  termLog('', 'default');
  await termType('$ initializing...', 'info', 15);
  termLog(`  Platform: ${navigator.platform}`, 'default');
  termLog(`  Web Serial: ${'serial' in navigator ? 'supported ✓' : 'NOT supported ✗'}`, 'serial' in navigator ? 'success' : 'error');
  termLog('', 'default');

  await fetchReleases();

  termLog('', 'default');
  await termType('$ ready — select a version and flash!', 'success', 15);

  // Add blinking cursor
  const cursor = document.createElement('span');
  cursor.className = 'terminal-cursor';
  terminalBody.appendChild(cursor);
}

// ─── Smooth scroll to flash section ────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const flashLink = document.getElementById('go-flash');
  if (flashLink) {
    flashLink.addEventListener('click', (e) => {
      e.preventDefault();
      const target = document.getElementById('flash-section');
      if (target) target.scrollIntoView({ behavior: 'smooth' });
    });
  }

  // Init
  init();
});
