const state = {
  sessionId: null,
  socket: null,
  audioSocket: null,
  frameWidth: 1280,
  frameHeight: 720,
  connected: false,
  composing: false,
  lastMoveAt: 0,
  lastRemoteClipboard: "",
  writingClipboard: false,
  pendingClipboard: "",
  quitting: false,
  locked: false,
  lockUrl: "",
  waitingForFrame: false,
  loadingAwaitingStatusUrl: false,
  loadingTargetUrl: "",
  loadingTimeout: null,
  mobileClient: false,
  lastPointerType: "",
  remoteTextFocused: false,
  mobileInputViewArmed: false,
  touchGesture: null,
  activeTouches: new Map(),
  pinchGesture: null,
  mobileLayoutViewport: null,
  lastSentViewport: null,
  frameDisplay: { offsetX: 0, offsetY: 0, width: 1280, height: 720 },
  cookieSyncTimer: null,
  cookieSyncInFlight: false,
  cookieSnapshot: "",
  cookieDirty: false,
  cookieServerChanged: false,
  mobileBackGuardInstalled: false,
  reconnectTimer: null,
  reconnectAttempts: 0,
  intentionalSocketClose: false,
  mobileBackGuardPrimed: false,
  remoteCaret: null,
  audioStreams: new Map(),
  audioUnlocked: false,
  audioUserActivated: false,
  audioContext: null,
  lastAudioRms: 0,
  lastAudioSampleAt: 0,
  audioPlaybackErrors: [],
  pendingFrameHeader: null,
  pendingFrameTimer: null,
  frameObjectUrl: "",
  activeDialog: null,
  dialogQueue: [],
  dialogLastFocus: null,
};
window.websiteAgentState = state;

const launcher = document.getElementById("launcher");
const launchForm = document.getElementById("launch-form");
const launchError = document.getElementById("launch-error");
const targetUrl = document.getElementById("target-url");
const toolbar = document.querySelector(".toolbar");
const lockedMessage = document.getElementById("locked-message");
const addressForm = document.getElementById("address-form");
const addressInput = document.getElementById("address-input");
const backButton = document.getElementById("back-button");
const forwardButton = document.getElementById("forward-button");
const reloadButton = document.getElementById("reload-button");
const cookieButton = document.getElementById("cookie-button");
const quitButton = document.getElementById("quit-button");
const goButton = document.getElementById("go-button");
const statusText = document.getElementById("status-text");
const viewportWrap = document.getElementById("viewport-wrap");
const canvas = document.getElementById("viewport");
const remoteCaret = document.getElementById("remote-caret");
const inputProxy = document.getElementById("input-proxy");
const loadingOverlay = document.getElementById("loading-overlay");
const loadingText = document.getElementById("loading-text");
const cookieDialog = document.getElementById("cookie-dialog");
const cookieSummary = document.getElementById("cookie-summary");
const cookieError = document.getElementById("cookie-error");
const cookieList = document.getElementById("cookie-list");
const cookieCount = document.getElementById("cookie-count");
const cookieSyncStatus = document.getElementById("cookie-sync-status");
const cookieRefreshButton = document.getElementById("cookie-refresh-button");
const cookieCloseButton = document.getElementById("cookie-close-button");
const cookieCancelButton = document.getElementById("cookie-cancel-button");
const cookieAddButton = document.getElementById("cookie-add-button");
const cookieSaveButton = document.getElementById("cookie-save-button");
const cookieJsonInput = document.getElementById("cookie-json-input");
const cookieJsonRestoreButton = document.getElementById("cookie-json-restore-button");
const browserDialog = document.getElementById("browser-dialog");
const browserDialogTitle = document.getElementById("browser-dialog-title");
const browserDialogSubtitle = document.getElementById("browser-dialog-subtitle");
const browserDialogMessage = document.getElementById("browser-dialog-message");
const browserDialogInputLabel = document.getElementById("browser-dialog-input-label");
const browserDialogInput = document.getElementById("browser-dialog-input");
const browserDialogCancelButton = document.getElementById("browser-dialog-cancel-button");
const browserDialogOkButton = document.getElementById("browser-dialog-ok-button");
const context = canvas.getContext("2d", { alpha: false });
const COOKIE_SYNC_INTERVAL_MS = 2500;
const MOBILE_VIEWPORT_MAX_WIDTH = 430;
const TOUCH_TAP_MAX_MOVE = 8;
const TOUCH_SCROLL_MIN_MOVE = 6;
const TOUCH_WHEEL_SCALE = 1.15;
const TOUCH_HOVER_DELAY_MS = 450;
const PINCH_START_MIN_DISTANCE = 24;
const PINCH_STEP_MIN_RATIO = 0.035;
const PINCH_SCALE_RESPONSE = 0.5;
const DESKTOP_IME_OFFSET_X = 10;
const DESKTOP_IME_OFFSET_Y = 8;
const NAVIGATION_LOADING_MAX_MS = 2500;

function detectMobileClient() {
  const coarsePointer =
    window.matchMedia && window.matchMedia("(hover: none) and (pointer: coarse)").matches;
  const userAgentMobile =
    navigator.userAgentData && typeof navigator.userAgentData.mobile === "boolean"
      ? navigator.userAgentData.mobile
      : /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
  return Boolean(coarsePointer || userAgentMobile);
}

function setStatus(text) {
  statusText.textContent = text;
}

function showLoading(message = "Server loading ...") {
  state.waitingForFrame = true;
  ensureMobileBackGuard();
  loadingText.textContent = message;
  loadingOverlay.hidden = false;
  loadingOverlay.setAttribute("aria-busy", "true");
}

function hideLoading() {
  state.waitingForFrame = false;
  state.loadingAwaitingStatusUrl = false;
  state.loadingTargetUrl = "";
  if (state.loadingTimeout !== null) {
    window.clearTimeout(state.loadingTimeout);
    state.loadingTimeout = null;
  }
  loadingOverlay.hidden = true;
  loadingOverlay.setAttribute("aria-busy", "false");
}

function armNavigationLoadingTimeout() {
  if (state.loadingTimeout !== null) {
    window.clearTimeout(state.loadingTimeout);
  }
  state.loadingTimeout = window.setTimeout(() => {
    state.loadingAwaitingStatusUrl = false;
    state.loadingTargetUrl = "";
    state.loadingTimeout = null;
    hideLoading();
  }, NAVIGATION_LOADING_MAX_MS);
}

function sameUrlWithoutHash(left, right) {
  if (!left || !right) {
    return false;
  }
  return String(left).split("#", 1)[0] === String(right).split("#", 1)[0];
}

function setControlsEnabled(enabled) {
  if (state.locked) {
    addressInput.disabled = true;
    backButton.disabled = true;
    forwardButton.disabled = true;
    reloadButton.disabled = true;
    cookieButton.disabled = true;
    quitButton.disabled = true;
    goButton.disabled = true;
    return;
  }
  addressInput.disabled = !enabled;
  backButton.disabled = !enabled;
  forwardButton.disabled = !enabled;
  reloadButton.disabled = !enabled;
  cookieButton.disabled = !enabled;
  quitButton.disabled = !enabled;
  goButton.disabled = !enabled;
}

function handleBackCommand() {
  if (state.waitingForFrame) {
    setStatus("Loading");
    return;
  }
  if (state.locked) {
    setStatus("Options locked");
    return;
  }
  if (!state.sessionId || !state.connected) {
    return;
  }
  send({ type: "back" });
}

function currentHistoryState() {
  return history.state && typeof history.state === "object" ? history.state : {};
}

function canUseMobileBackGuard() {
  return (
    state.mobileClient &&
    window.history &&
    typeof history.pushState === "function" &&
    typeof history.replaceState === "function"
  );
}

function ensureMobileBackGuard() {
  if (!canUseMobileBackGuard()) {
    return false;
  }
  try {
    const currentState = currentHistoryState();
    if (!currentState.websiteAgentGuard) {
      const rootState = currentState.websiteAgentRoot
        ? currentState
        : { ...currentState, websiteAgentRoot: true };
      history.replaceState(rootState, "", window.location.href);
      history.pushState({ websiteAgentGuard: true }, "", window.location.href);
    }
    if (!state.mobileBackGuardPrimed) {
      history.pushState({ websiteAgentGuard: true }, "", window.location.href);
      state.mobileBackGuardPrimed = true;
    }
    state.mobileBackGuardInstalled = true;
    return true;
  } catch {
    return false;
  }
}

function installMobileBackGuard() {
  ensureMobileBackGuard();
}

function restoreMobileBackGuard() {
  if (!canUseMobileBackGuard()) {
    return;
  }
  try {
    history.pushState({ websiteAgentGuard: true }, "", window.location.href);
    state.mobileBackGuardInstalled = true;
    state.mobileBackGuardPrimed = true;
  } catch {
    // If the browser refuses history manipulation, the normal page lifecycle continues.
  }
}

function handleMobileBrowserBack() {
  restoreMobileBackGuard();
  if (state.waitingForFrame) {
    setStatus("Loading");
    return;
  }
  handleBackCommand();
}

function mobileLayoutViewport() {
  if (!state.mobileLayoutViewport) {
    const width = Math.max(320, Math.min(MOBILE_VIEWPORT_MAX_WIDTH, Math.floor(window.innerWidth || 390)));
    const toolbarHeight = toolbar.getBoundingClientRect().height || 0;
    const height = Math.max(360, Math.min(1200, Math.floor((window.innerHeight || 720) - toolbarHeight)));
    state.mobileLayoutViewport = { width, height };
  }
  return state.mobileLayoutViewport;
}

function isMobileKeyboardResize() {
  if (!state.mobileClient || !state.mobileLayoutViewport || !window.visualViewport) {
    return false;
  }
  return window.visualViewport.height < window.innerHeight * 0.82;
}

function measureViewport() {
  const rect = viewportWrap.getBoundingClientRect();
  if (state.mobileClient) {
    return mobileLayoutViewport();
  }
  return {
    width: Math.max(320, Math.min(1920, Math.floor(rect.width || 1280))),
    height: Math.max(240, Math.min(1600, Math.floor(rect.height || 720))),
  };
}

function decodeLockPathValue(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    throw new Error("Locked URL path is malformed.");
  }
}

function normalizeLockUrl(candidate) {
  const value = candidate.trim();
  if (!value) {
    throw new Error("Locked URL path is empty.");
  }
  return value;
}

function appendCurrentUrlSuffix(candidate) {
  const hashIndex = candidate.indexOf("#");
  const base = hashIndex === -1 ? candidate : candidate.slice(0, hashIndex);
  const embeddedHash = hashIndex === -1 ? "" : candidate.slice(hashIndex);
  let result = base;
  if (window.location.search) {
    result += base.includes("?") ? `&${window.location.search.slice(1)}` : window.location.search;
  }
  return `${result}${embeddedHash || window.location.hash || ""}`;
}

function lockUrlFromLocation() {
  const prefix = "/lock_url/";
  if (!window.location.pathname.startsWith(prefix)) {
    return "";
  }
  const rawPath = window.location.pathname.slice(prefix.length);
  if (!rawPath) {
    throw new Error("Locked URL path is empty.");
  }
  const pathParts = rawPath.split("/");
  const scheme = decodeLockPathValue(pathParts[0] || "").toLowerCase();
  let candidate;
  if ((scheme === "http" || scheme === "https") && pathParts.length > 1) {
    const rest = decodeLockPathValue(pathParts.slice(1).join("/"));
    if (!rest) {
      throw new Error("Locked URL path must include a host name.");
    }
    candidate = `${scheme}://${rest}`;
  } else {
    candidate = decodeLockPathValue(rawPath);
  }
  return normalizeLockUrl(appendCurrentUrlSuffix(candidate));
}

async function loadClientConfig() {
  const response = await fetch("/api/config");
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || "Could not load server configuration.");
  }
  const pathLockUrl = lockUrlFromLocation();
  state.locked = Boolean(body.locked || pathLockUrl);
  state.lockUrl = body.lock_url || pathLockUrl || "";
}

function applyLockedMode() {
  toolbar.classList.toggle("locked", state.locked);
  document.body.classList.toggle("locked", state.locked);
  lockedMessage.hidden = !state.locked;
  if (state.locked) {
    launcher.hidden = true;
    addressInput.value = state.lockUrl;
    setControlsEnabled(false);
  } else if (!state.sessionId) {
    launcher.hidden = false;
    targetUrl.focus();
  }
}

async function createSession(url) {
  const viewport = measureViewport();
  state.lastSentViewport = viewport;
  const payload = {
    url,
    width: viewport.width,
    height: viewport.height,
    is_mobile: state.mobileClient,
    device_scale_factor: Math.max(1, Math.min(4, window.devicePixelRatio || 1)),
  };
  if (state.locked && state.lockUrl) {
    payload.lock_url = state.lockUrl;
  }
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || "Could not open the target site.");
  }
  return body;
}

async function restoreCurrentSession() {
  const response = await fetch("/api/sessions/current");
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || "Could not restore the current session.");
  }
  if (!body.session_id) {
    return false;
  }
  state.sessionId = body.session_id;
  addressInput.value = body.url || "";
  if (body.locked) {
    state.locked = true;
    state.lockUrl = body.url || state.lockUrl;
    applyLockedMode();
  } else {
    launcher.hidden = true;
  }
  connectSession(state.sessionId);
  return true;
}

function connectSession(sessionId) {
  if (state.socket) {
    state.socket.__websiteAgentIntentionalClose = true;
    state.socket.close();
  }
  closeAudioSocket();
  state.intentionalSocketClose = false;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/${sessionId}`);
  socket.binaryType = "arraybuffer";
  state.socket = socket;

  socket.addEventListener("open", () => {
    state.reconnectAttempts = 0;
    state.connected = true;
    setControlsEnabled(true);
    setStatus("Connected");
    if (!state.waitingForFrame) {
      showLoading();
    }
    sendResize();
    if (!state.mobileClient) {
      focusInputProxy();
    }
    connectAudioSession(sessionId);
  });

  socket.addEventListener("message", (event) => {
    if (event.data instanceof ArrayBuffer || event.data instanceof Blob) {
      handleFrameBinary(event.data);
      return;
    }
    const message = JSON.parse(event.data);
    handleServerMessage(message);
  });

  socket.addEventListener("close", () => {
    const intentionalClose = Boolean(socket.__websiteAgentIntentionalClose || state.intentionalSocketClose);
    if (state.socket === socket) {
      state.socket = null;
    }
    state.connected = false;
    setControlsEnabled(false);
    hideLoading();
    closeBrowserDialog(false, true);
    closeAudioSocket();
    cleanupAudioStreams();
    if (!state.quitting && state.sessionId) {
      setStatus("Disconnected");
      if (!intentionalClose) {
        scheduleReconnect();
      }
    }
    if (state.socket === null) {
      state.intentionalSocketClose = false;
    }
  });
}

function connectAudioSession(sessionId) {
  closeAudioSocket();
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/${sessionId}/audio`);
  state.audioSocket = socket;
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "audio") {
      handleAudioMessage(message);
    }
  });
  socket.addEventListener("close", () => {
    if (state.audioSocket === socket) {
      state.audioSocket = null;
    }
  });
}

function closeAudioSocket() {
  if (!state.audioSocket) {
    return;
  }
  state.audioSocket.close();
  state.audioSocket = null;
}

function scheduleReconnect() {
  if (!state.sessionId || state.reconnectTimer !== null || state.quitting) {
    return;
  }
  const delay = Math.min(10000, 700 * 2 ** Math.min(4, state.reconnectAttempts));
  state.reconnectAttempts += 1;
  state.reconnectTimer = window.setTimeout(() => {
    state.reconnectTimer = null;
    reconnectSession();
  }, delay);
}

async function reconnectSession() {
  if (!state.sessionId || state.connected || state.quitting) {
    return;
  }
  setStatus("Reconnecting");
  try {
    const response = await fetch("/api/sessions/current");
    const body = await response.json().catch(() => ({}));
    if (!response.ok || body.session_id !== state.sessionId) {
      state.sessionId = null;
      setStatus("Session expired");
      state.reconnectAttempts = 0;
      applyLockedMode();
      if (state.locked) {
        startLockedSession();
      }
      return;
    }
    if (body.url) {
      addressInput.value = body.url;
    }
    connectSession(state.sessionId);
  } catch {
    scheduleReconnect();
  }
}

function resetViewport() {
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, canvas.width || 1, canvas.height || 1);
  hideRemoteCaret();
}

function clearFrameObjectUrl() {
  if (state.frameObjectUrl) {
    window.URL.revokeObjectURL(state.frameObjectUrl);
    state.frameObjectUrl = "";
  }
}

function clearPendingFrame() {
  state.pendingFrameHeader = null;
  if (state.pendingFrameTimer !== null) {
    window.clearTimeout(state.pendingFrameTimer);
    state.pendingFrameTimer = null;
  }
}

function base64ToArrayBuffer(value) {
  const binary = window.atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}

function cleanupAudioStream(streamId) {
  const stream = state.audioStreams.get(streamId);
  if (!stream) {
    return;
  }
  state.audioStreams.delete(streamId);
  window.clearTimeout(stream.stopTimer);
  try {
    if (stream.sourceBuffer && stream.mediaSource.readyState === "open") {
      stream.mediaSource.endOfStream();
    }
  } catch {
    // The media source may already be closing.
  }
  try {
    if (stream.sourceNode) {
      stream.sourceNode.disconnect();
    }
    if (stream.analyser) {
      stream.analyser.disconnect();
    }
  } catch {
    // Nodes may already be disconnected.
  }
  if (stream.sampleTimer !== null) {
    window.clearInterval(stream.sampleTimer);
  }
  stream.audio.pause();
  stream.audio.remove();
  window.URL.revokeObjectURL(stream.url);
}

function cleanupAudioStreams() {
  for (const streamId of [...state.audioStreams.keys()]) {
    cleanupAudioStream(streamId);
  }
}

function makeAudioStream(streamId, mime) {
  cleanupAudioStream(streamId);
  const mediaSource = new MediaSource();
  const audio = document.createElement("audio");
  audio.autoplay = true;
  audio.playsInline = true;
  audio.hidden = true;
  const url = window.URL.createObjectURL(mediaSource);
  audio.src = url;
  document.body.appendChild(audio);
  const stream = {
    streamId,
    audio,
    url,
    mediaSource,
    sourceBuffer: null,
    queue: [],
    mime,
    stopTimer: null,
    sourceNode: null,
    analyser: null,
    sampleTimer: null,
  };
  state.audioStreams.set(streamId, stream);
  attachAudioAnalyser(stream);
  mediaSource.addEventListener("sourceopen", () => {
    try {
      stream.sourceBuffer = mediaSource.addSourceBuffer(mime);
      stream.sourceBuffer.mode = "sequence";
      stream.sourceBuffer.addEventListener("updateend", () => pumpAudioStream(stream));
      pumpAudioStream(stream);
      unlockAudioPlayback();
    } catch {
      cleanupAudioStream(streamId);
    }
  });
  return stream;
}

function ensureAudioContext() {
  if (!state.audioContext) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
      return null;
    }
    state.audioContext = new AudioContextClass();
  }
  return state.audioContext;
}

function attachAudioAnalyser(stream) {
  const audioContext = ensureAudioContext();
  if (!audioContext) {
    return;
  }
  try {
    stream.sourceNode = audioContext.createMediaElementSource(stream.audio);
    stream.analyser = audioContext.createAnalyser();
    stream.analyser.fftSize = 256;
    stream.sourceNode.connect(stream.analyser);
    stream.analyser.connect(audioContext.destination);
    const samples = new Uint8Array(stream.analyser.fftSize);
    stream.sampleTimer = window.setInterval(() => {
      stream.analyser.getByteTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) {
        const centered = (sample - 128) / 128;
        sum += centered * centered;
      }
      const rms = Math.sqrt(sum / samples.length);
      state.lastAudioRms = Math.max(state.lastAudioRms * 0.85, rms);
      state.lastAudioSampleAt = performance.now();
    }, 100);
  } catch (error) {
    state.audioPlaybackErrors.push(String(error && error.message ? error.message : error));
  }
}

function pumpAudioStream(stream) {
  if (
    !stream.sourceBuffer ||
    stream.sourceBuffer.updating ||
    stream.mediaSource.readyState !== "open" ||
    stream.queue.length === 0
  ) {
    return;
  }
  try {
    stream.sourceBuffer.appendBuffer(stream.queue.shift());
    if (state.audioUserActivated) {
      playAudioStreams();
    }
  } catch {
    cleanupAudioStream(stream.streamId);
  }
}

function playAudioStreams() {
  if (state.audioContext && state.audioContext.state === "suspended") {
    state.audioContext.resume().catch(() => {});
  }
  for (const stream of state.audioStreams.values()) {
    const playPromise = stream.audio.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch((error) => {
        state.audioUnlocked = false;
        state.audioPlaybackErrors.push(String(error && error.message ? error.message : error));
      });
    }
  }
}

function unlockAudioPlayback() {
  state.audioUserActivated = true;
  state.audioUnlocked = true;
  ensureAudioContext();
  playAudioStreams();
}

function handleAudioMessage(message) {
  const streamId = message.streamId || "media";
  if (message.kind === "stop") {
    const stream = state.audioStreams.get(streamId);
    if (stream) {
      window.clearTimeout(stream.stopTimer);
      stream.stopTimer = window.setTimeout(() => cleanupAudioStream(streamId), 1200);
    }
    return;
  }
  if (message.kind === "start") {
    if (message.mime && window.MediaSource && MediaSource.isTypeSupported(message.mime)) {
      makeAudioStream(streamId, message.mime);
    }
    return;
  }
  if (message.kind !== "chunk" || !message.data || !message.mime) {
    return;
  }
  let stream = state.audioStreams.get(streamId);
  if (!stream) {
    if (!window.MediaSource || !MediaSource.isTypeSupported(message.mime)) {
      return;
    }
    stream = makeAudioStream(streamId, message.mime);
  }
  window.clearTimeout(stream.stopTimer);
  stream.stopTimer = null;
  stream.queue.push(base64ToArrayBuffer(message.data));
  pumpAudioStream(stream);
  if (state.audioUserActivated) {
    playAudioStreams();
  }
}

async function quitSession() {
  if (state.locked) {
    return;
  }
  const sessionId = state.sessionId;
  if (!sessionId) {
    return;
  }
  closeCookieDialog(false);
  closeBrowserDialog(false, true);
  setControlsEnabled(false);
  state.quitting = true;
  setStatus("Closing");
  if (state.reconnectTimer !== null) {
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  if (state.socket) {
    state.intentionalSocketClose = true;
    state.socket.__websiteAgentIntentionalClose = true;
    state.socket.close();
    state.socket = null;
  }
  closeAudioSocket();
  state.connected = false;
  state.sessionId = null;
  state.lastSentViewport = null;
  state.mobileLayoutViewport = null;
  try {
    await fetch(`/api/sessions/${sessionId}/close`, { method: "POST" });
  } catch {
    // The browser session may already be gone.
  }
  resetViewport();
  hideLoading();
  cleanupAudioStreams();
  addressInput.value = "";
  launchError.textContent = "";
  launcher.hidden = state.locked;
  state.quitting = false;
  setStatus("Idle");
  if (!state.locked) {
    targetUrl.focus();
  }
}

function handleServerMessage(message) {
  if (message.type === "frame") {
    drawFrame(message);
  } else if (message.type === "status") {
    if (message.url) {
      addressInput.value = message.url;
    }
    if (message.state === "loading") {
      state.mobileInputViewArmed = true;
      state.loadingAwaitingStatusUrl = false;
      state.loadingTargetUrl = message.url || state.loadingTargetUrl;
      showLoading();
      armNavigationLoadingTimeout();
    } else if (message.url) {
      state.loadingAwaitingStatusUrl = false;
      if (state.loadingTargetUrl && !sameUrlWithoutHash(message.url, state.loadingTargetUrl)) {
        state.loadingTargetUrl = message.url;
      }
    }
    setStatus(message.state || "Ready");
  } else if (message.type === "error") {
    hideLoading();
    setStatus("Error");
    launchError.textContent = message.message || "Request failed.";
  } else if (message.type === "warning") {
    setStatus(message.message || "Warning");
  } else if (message.type === "blocked") {
    setStatus("Blocked");
    state.mobileInputViewArmed = true;
  } else if (message.type === "download") {
    startDownload(message);
  } else if (message.type === "filechooser") {
    openFileChooser(message);
  } else if (message.type === "dialog") {
    openBrowserDialog(message);
  } else if (message.type === "clipboard") {
    handleRemoteClipboard(message.text || "");
  } else if (message.type === "editable") {
    handleEditableProbe(message);
  } else if (message.type === "audio") {
    handleAudioMessage(message);
  }
}

function handleEditableProbe(message) {
  if (!state.mobileClient) {
    return;
  }
  if (message.editable) {
    state.mobileInputViewArmed = true;
    focusInputProxy();
  } else {
    state.mobileInputViewArmed = true;
    blurInputProxy();
  }
}

function hideRemoteCaret() {
  state.remoteCaret = null;
  remoteCaret.hidden = true;
}

function remoteCaretClientRect() {
  const caret = state.remoteCaret;
  if (!caret || typeof caret.x !== "number" || typeof caret.y !== "number") {
    return null;
  }
  const display = state.frameDisplay;
  const wrapRect = viewportWrap.getBoundingClientRect();
  const scaleX = display.width / Math.max(1, state.frameWidth);
  const scaleY = display.height / Math.max(1, state.frameHeight);
  const left = wrapRect.left + display.offsetX + caret.x * scaleX;
  const top = wrapRect.top + display.offsetY + caret.y * scaleY;
  const width = Math.max(1, caret.width * scaleX);
  const height = Math.max(8, caret.height * scaleY);
  return { left, top, width, height };
}

function updateDesktopImeAnchor() {
  if (state.mobileClient || !state.remoteTextFocused) {
    return;
  }
  const caretRect = remoteCaretClientRect();
  if (!caretRect) {
    return;
  }
  const proxyWidth = Math.max(2, inputProxy.offsetWidth || 2);
  const proxyHeight = Math.max(2, inputProxy.offsetHeight || 2);
  const x = Math.min(
    Math.max(0, caretRect.left + caretRect.width + DESKTOP_IME_OFFSET_X),
    Math.max(0, window.innerWidth - proxyWidth)
  );
  const y = Math.min(
    Math.max(0, caretRect.top + caretRect.height + DESKTOP_IME_OFFSET_Y),
    Math.max(0, window.innerHeight - proxyHeight)
  );
  inputProxy.style.left = `${Math.floor(x)}px`;
  inputProxy.style.top = `${Math.floor(y)}px`;
}

function updateRemoteCaret() {
  const caret = state.remoteCaret;
  if (!caret || typeof caret.x !== "number" || typeof caret.y !== "number") {
    remoteCaret.hidden = true;
    updateDesktopImeAnchor();
    return;
  }
  const display = state.frameDisplay;
  const scaleX = display.width / Math.max(1, state.frameWidth);
  const scaleY = display.height / Math.max(1, state.frameHeight);
  const left = display.offsetX + caret.x * scaleX;
  const top = display.offsetY + caret.y * scaleY;
  const width = Math.max(1, caret.width * scaleX);
  const height = Math.max(8, caret.height * scaleY);
  remoteCaret.style.left = `${left}px`;
  remoteCaret.style.top = `${top}px`;
  remoteCaret.style.width = `${width}px`;
  remoteCaret.style.height = `${height}px`;
  remoteCaret.hidden = false;
  updateDesktopImeAnchor();
}

function drawFrame(message) {
  state.frameWidth = message.width;
  state.frameHeight = message.height;
  if (!message.image) {
    clearPendingFrame();
    state.pendingFrameHeader = message;
    state.pendingFrameTimer = window.setTimeout(() => {
      clearPendingFrame();
      if (!state.loadingAwaitingStatusUrl && !state.loadingTargetUrl) {
        hideLoading();
      }
    }, 2500);
    return;
  }
  drawFrameImage(message, `data:${message.mime};base64,${message.image}`);
}

function handleFrameBinary(data) {
  const message = state.pendingFrameHeader;
  clearPendingFrame();
  if (!message) {
    return;
  }
  const blob = data instanceof Blob ? data : new Blob([data], { type: message.mime || "image/jpeg" });
  clearFrameObjectUrl();
  state.frameObjectUrl = window.URL.createObjectURL(blob);
  drawFrameImage(message, state.frameObjectUrl);
}

function drawFrameImage(message, imageSource) {
  state.frameWidth = message.width;
  state.frameHeight = message.height;
  if (
    state.loadingTargetUrl &&
    (!message.url || sameUrlWithoutHash(message.url, state.loadingTargetUrl))
  ) {
    state.loadingTargetUrl = "";
  }
  state.remoteCaret =
    (!state.mobileClient || state.remoteTextFocused) && message.caret ? message.caret : null;
  if (addressInput !== document.activeElement && message.url) {
    addressInput.value = message.url;
  }
  const image = new Image();
  image.onload = () => {
    if (canvas.width !== message.width || canvas.height !== message.height) {
      canvas.width = message.width;
      canvas.height = message.height;
    }
    context.fillStyle = "#111827";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    updateFrameDisplay();
    if (!state.loadingAwaitingStatusUrl && !state.loadingTargetUrl) {
      hideLoading();
    }
  };
  image.onerror = () => {
    if (!state.loadingAwaitingStatusUrl && !state.loadingTargetUrl) {
      hideLoading();
    }
  };
  image.src = imageSource;
}

function send(message) {
  unlockAudioPlayback();
  if (
    state.locked &&
    (message.type === "navigate" || message.type === "back" || message.type === "forward")
  ) {
    return;
  }
  if (!state.connected || !state.socket || state.socket.readyState !== WebSocket.OPEN) {
    return;
  }
  if (["navigate", "reload", "back", "forward"].includes(message.type)) {
    state.mobileInputViewArmed = true;
    showLoading();
    state.loadingAwaitingStatusUrl = true;
    state.loadingTargetUrl = "";
    armNavigationLoadingTimeout();
  }
  state.socket.send(JSON.stringify(message));
}

function dialogTitle(dialogType) {
  if (dialogType === "confirm") {
    return "Confirm";
  }
  if (dialogType === "prompt") {
    return "Prompt";
  }
  if (dialogType === "beforeunload") {
    return "Leave Page?";
  }
  return "Alert";
}

function dialogSubtitle(dialogType) {
  if (dialogType === "beforeunload") {
    return "The remote page is asking before navigation.";
  }
  return "The remote page opened a browser dialog.";
}

function openBrowserDialog(message) {
  if (!message || !message.token) {
    return;
  }
  if (state.activeDialog) {
    state.dialogQueue.push(message);
    return;
  }
  state.dialogLastFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  state.activeDialog = message;
  const dialogType = message.dialogType || "alert";
  browserDialogTitle.textContent = dialogTitle(dialogType);
  browserDialogSubtitle.textContent = dialogSubtitle(dialogType);
  browserDialogMessage.textContent = message.message || "";
  browserDialogInput.value = message.defaultValue || "";
  const needsInput = dialogType === "prompt";
  browserDialogInputLabel.hidden = !needsInput;
  const hasCancel = dialogType === "confirm" || dialogType === "prompt" || dialogType === "beforeunload";
  browserDialogCancelButton.hidden = !hasCancel;
  browserDialogCancelButton.textContent = dialogType === "beforeunload" ? "Stay" : "Cancel";
  browserDialogOkButton.textContent = dialogType === "beforeunload" ? "Leave" : "OK";
  browserDialog.hidden = false;
  setStatus(dialogTitle(dialogType));
  window.setTimeout(() => {
    if (needsInput) {
      browserDialogInput.focus();
      browserDialogInput.select();
    } else {
      browserDialogOkButton.focus();
    }
  }, 0);
}

function closeBrowserDialog(restoreFocus = true, clearQueue = false) {
  if (clearQueue) {
    state.dialogQueue = [];
  }
  browserDialog.hidden = true;
  state.activeDialog = null;
  if (restoreFocus && state.dialogLastFocus && document.contains(state.dialogLastFocus)) {
    state.dialogLastFocus.focus({ preventScroll: true });
  } else if (!state.mobileClient || state.remoteTextFocused) {
    focusInputProxy();
  }
  state.dialogLastFocus = null;
  const nextDialog = clearQueue ? null : state.dialogQueue.shift();
  if (nextDialog) {
    window.setTimeout(() => openBrowserDialog(nextDialog), 0);
  }
}

function respondToBrowserDialog(accepted) {
  const dialog = state.activeDialog;
  if (!dialog) {
    return;
  }
  send({
    type: "dialog_response",
    token: dialog.token,
    accepted,
    value: browserDialogInput.value,
  });
  closeBrowserDialog();
  setStatus(accepted ? "Dialog accepted" : "Dialog dismissed");
}

function focusInputProxy(clientX, clientY) {
  state.remoteTextFocused = true;
  if (typeof clientX === "number" && typeof clientY === "number") {
    inputProxy.style.left = `${Math.max(0, Math.floor(clientX))}px`;
    inputProxy.style.top = `${Math.max(0, Math.floor(clientY))}px`;
  }
  updateDesktopImeAnchor();
  inputProxy.focus({ preventScroll: true });
}

function blurInputProxy() {
  state.remoteTextFocused = false;
  state.mobileInputViewArmed = true;
  hideRemoteCaret();
  if (document.activeElement === inputProxy) {
    inputProxy.blur();
  }
}

function clearInputProxy() {
  inputProxy.value = "";
  inputProxy.selectionStart = 0;
  inputProxy.selectionEnd = 0;
}

function flushInputProxy() {
  if (state.composing) {
    return;
  }
  const text = inputProxy.value;
  if (text) {
    const shouldFocusView =
      state.mobileClient && state.remoteTextFocused && state.mobileInputViewArmed;
    send({ type: "text", text, focus_view: shouldFocusView });
    if (shouldFocusView) {
      state.mobileInputViewArmed = false;
    }
    clearInputProxy();
  }
}

function sendResize() {
  if (!state.sessionId) {
    return;
  }
  if (isMobileKeyboardResize()) {
    return;
  }
  const viewport = measureViewport();
  if (
    state.lastSentViewport &&
    state.lastSentViewport.width === viewport.width &&
    state.lastSentViewport.height === viewport.height
  ) {
    return;
  }
  state.lastSentViewport = viewport;
  send({ type: "resize", width: viewport.width, height: viewport.height });
}

function pointerToRemote(event) {
  const rect = canvas.getBoundingClientRect();
  const display = state.frameDisplay;
  const relativeX = Math.max(
    0,
    Math.min(display.width, event.clientX - rect.left - display.offsetX)
  );
  const relativeY = Math.max(
    0,
    Math.min(display.height, event.clientY - rect.top - display.offsetY)
  );
  return {
    x: Math.max(
      0,
      Math.min(state.frameWidth, (relativeX * state.frameWidth) / Math.max(1, display.width))
    ),
    y: Math.max(
      0,
      Math.min(state.frameHeight, (relativeY * state.frameHeight) / Math.max(1, display.height))
    ),
  };
}

function updateFrameDisplay() {
  const rect = canvas.getBoundingClientRect();
  const frameRatio = state.frameWidth / Math.max(1, state.frameHeight);
  const rectRatio = rect.width / Math.max(1, rect.height);
  let width = rect.width;
  let height = rect.height;
  if (rectRatio > frameRatio) {
    width = rect.height * frameRatio;
  } else {
    height = rect.width / frameRatio;
  }
  state.frameDisplay = {
    offsetX: Math.max(0, (rect.width - width) / 2),
    offsetY: Math.max(0, (rect.height - height) / 2),
    width: Math.max(1, width),
    height: Math.max(1, height),
  };
  updateRemoteCaret();
}

function pointerButton(event) {
  if (event.button === 1) {
    return "middle";
  }
  if (event.button === 2) {
    return "right";
  }
  return "left";
}

function setTouchPoint(id, point) {
  state.activeTouches.set(id, {
    clientX: point.clientX,
    clientY: point.clientY,
  });
}

function deleteTouchPointId(id) {
  state.activeTouches.delete(id);
  if (state.activeTouches.size < 2) {
    state.pinchGesture = null;
  }
}

function captureCanvasPointer(event) {
  try {
    canvas.setPointerCapture(event.pointerId);
  } catch {
    // Synthetic or interrupted touch events may not have an active pointer capture target.
  }
}

function releaseCanvasPointer(event) {
  try {
    if (canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
  } catch {
    // The pointer may already have been released by the browser.
  }
}

function updateTouchPoint(event) {
  setTouchPoint(event.pointerId, event);
}

function deleteTouchPoint(event) {
  deleteTouchPointId(event.pointerId);
}

function twoTouchMetrics() {
  const touches = [...state.activeTouches.values()];
  if (touches.length < 2) {
    return null;
  }
  const first = touches[0];
  const second = touches[1];
  const centerClientX = (first.clientX + second.clientX) / 2;
  const centerClientY = (first.clientY + second.clientY) / 2;
  const distance = Math.hypot(first.clientX - second.clientX, first.clientY - second.clientY);
  return {
    centerClientX,
    centerClientY,
    distance,
    centerPoint: pointerToRemote({ clientX: centerClientX, clientY: centerClientY }),
  };
}

function beginPinchGesture() {
  const metrics = twoTouchMetrics();
  if (!metrics || metrics.distance < PINCH_START_MIN_DISTANCE) {
    state.pinchGesture = null;
    return;
  }
  clearTouchHoverTimer();
  state.touchGesture = null;
  blurInputProxy();
  state.pinchGesture = {
    lastSentDistance: metrics.distance,
    centerPoint: metrics.centerPoint,
  };
}

function updatePinchGesture() {
  const gesture = state.pinchGesture;
  const metrics = twoTouchMetrics();
  if (!gesture || !metrics || metrics.distance < PINCH_START_MIN_DISTANCE) {
    return;
  }
  const rawScale = metrics.distance / Math.max(1, gesture.lastSentDistance);
  const scale = 1 + (rawScale - 1) * PINCH_SCALE_RESPONSE;
  gesture.centerPoint = metrics.centerPoint;
  if (Math.abs(scale - 1) < PINCH_STEP_MIN_RATIO) {
    return;
  }
  gesture.lastSentDistance = metrics.distance;
  send({
    type: "pinch",
    x: metrics.centerPoint.x,
    y: metrics.centerPoint.y,
    scale,
  });
}

function changedTouchById(event, id) {
  for (const touch of event.changedTouches) {
    if (touch.identifier === id) {
      return touch;
    }
  }
  return null;
}

function clearTouchHoverTimer() {
  const gesture = state.touchGesture;
  if (gesture && gesture.hoverTimer !== null) {
    window.clearTimeout(gesture.hoverTimer);
    gesture.hoverTimer = null;
  }
}

function scheduleTouchHover(gesture) {
  clearTouchHoverTimer();
  gesture.hoverTimer = window.setTimeout(() => {
    if (state.touchGesture !== gesture || gesture.scrolling || gesture.moved) {
      return;
    }
    gesture.hovered = true;
    send({ type: "mouse_move", ...gesture.lastPoint });
  }, TOUCH_HOVER_DELAY_MS);
}

function clearTouchState() {
  clearTouchHoverTimer();
  state.touchGesture = null;
  state.pinchGesture = null;
  state.activeTouches.clear();
}

function handleCanvasTouchStart(event) {
  unlockAudioPlayback();
  if (!state.mobileClient) {
    return;
  }
  event.preventDefault();
  for (const touch of event.changedTouches) {
    setTouchPoint(touch.identifier, touch);
  }
  if (event.touches.length >= 2) {
    beginPinchGesture();
    return;
  }
  const touch = event.changedTouches[0];
  if (!touch || event.touches.length !== 1) {
    return;
  }
  const point = pointerToRemote(touch);
  state.touchGesture = {
    touchId: touch.identifier,
    startClientX: touch.clientX,
    startClientY: touch.clientY,
    lastClientX: touch.clientX,
    lastClientY: touch.clientY,
    startPoint: point,
    lastPoint: point,
    moved: false,
    scrolling: false,
    hovered: false,
    hoverTimer: null,
  };
  scheduleTouchHover(state.touchGesture);
}

function handleCanvasTouchMove(event) {
  if (!state.mobileClient) {
    return;
  }
  event.preventDefault();
  for (const touch of event.changedTouches) {
    setTouchPoint(touch.identifier, touch);
  }
  if (event.touches.length >= 2 || state.pinchGesture) {
    if (!state.pinchGesture) {
      beginPinchGesture();
    }
    updatePinchGesture();
    return;
  }
  const gesture = state.touchGesture;
  if (!gesture) {
    return;
  }
  const touch = changedTouchById(event, gesture.touchId);
  if (!touch) {
    return;
  }
  const totalX = touch.clientX - gesture.startClientX;
  const totalY = touch.clientY - gesture.startClientY;
  const totalDistance = Math.hypot(totalX, totalY);
  if (totalDistance > TOUCH_TAP_MAX_MOVE) {
    gesture.moved = true;
    gesture.scrolling = true;
    clearTouchHoverTimer();
  }
  const deltaX = touch.clientX - gesture.lastClientX;
  const deltaY = touch.clientY - gesture.lastClientY;
  gesture.lastClientX = touch.clientX;
  gesture.lastClientY = touch.clientY;
  gesture.lastPoint = pointerToRemote(touch);
  if (!gesture.scrolling || Math.hypot(deltaX, deltaY) < TOUCH_SCROLL_MIN_MOVE) {
    return;
  }
  clearTouchHoverTimer();
  blurInputProxy();
  send({
    type: "wheel",
    x: gesture.lastPoint.x,
    y: gesture.lastPoint.y,
    deltaX: -deltaX * TOUCH_WHEEL_SCALE,
    deltaY: -deltaY * TOUCH_WHEEL_SCALE,
  });
}

function handleCanvasTouchEnd(event) {
  if (!state.mobileClient) {
    return;
  }
  event.preventDefault();
  const wasPinching = Boolean(state.pinchGesture) || state.activeTouches.size >= 2;
  const gesture = state.touchGesture;
  const touch = gesture ? changedTouchById(event, gesture.touchId) : null;
  clearTouchHoverTimer();
  if (!wasPinching && gesture && touch && !gesture.scrolling) {
    const point = pointerToRemote(touch);
    focusInputProxy(touch.clientX, touch.clientY);
    send({ type: "tap", ...point });
    send({ type: "probe_editable", ...point });
  }
  for (const changed of event.changedTouches) {
    deleteTouchPointId(changed.identifier);
  }
  if (event.touches.length === 0) {
    state.touchGesture = null;
    state.pinchGesture = null;
  } else if (event.touches.length < 2) {
    state.pinchGesture = null;
  }
}

function handleCanvasTouchCancel(event) {
  if (!state.mobileClient) {
    return;
  }
  event.preventDefault();
  clearTouchState();
}

launchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.locked) {
    return;
  }
  launchError.textContent = "";
  setStatus("Opening");
  showLoading();
  const submitButton = launchForm.querySelector("button");
  submitButton.disabled = true;
  try {
    const session = await createSession(targetUrl.value);
    state.sessionId = session.session_id;
    addressInput.value = session.url || targetUrl.value;
    launcher.hidden = true;
    connectSession(state.sessionId);
  } catch (error) {
    launchError.textContent = error.message;
    hideLoading();
    setStatus("Idle");
  } finally {
    submitButton.disabled = false;
  }
});

addressForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.locked) {
    return;
  }
  send({ type: "navigate", url: addressInput.value });
  if (!state.mobileClient) {
    focusInputProxy();
  }
});

backButton.addEventListener("click", () => handleBackCommand());
forwardButton.addEventListener("click", () => send({ type: "forward" }));
reloadButton.addEventListener("click", () => send({ type: "reload" }));
cookieButton.addEventListener("click", () => {
  if (state.locked) {
    return;
  }
  openCookieDialog();
});
quitButton.addEventListener("click", () => {
  quitSession();
});

cookieRefreshButton.addEventListener("click", () => {
  loadCookies({ force: true });
});
cookieCloseButton.addEventListener("click", () => closeCookieDialog());
cookieCancelButton.addEventListener("click", () => closeCookieDialog());
cookieAddButton.addEventListener("click", () => {
  markCookieDirty();
  const host = cookieDefaultDomain();
  addCookieRow({
    name: "",
    value: "",
    domain: host,
    path: "/",
    expires: null,
    httpOnly: false,
    secure: cookieDefaultSecure(),
    sameSite: "Lax",
  });
  updateCookieCount();
  const lastRow = cookieList.querySelector(".cookie-row:last-child");
  const nameInput = lastRow ? lastRow.querySelector("[data-field='name']") : null;
  if (nameInput) {
    nameInput.focus();
  }
});
cookieSaveButton.addEventListener("click", () => {
  saveCookies();
});
cookieJsonRestoreButton.addEventListener("click", () => {
  restoreCookieJsonFromRows();
});
cookieJsonInput.addEventListener("input", () => {
  markCookieDirty();
  syncCookieRowsFromJson();
});
cookieList.addEventListener("input", (event) => {
  if (event.target.matches("input, select")) {
    markCookieDirty();
    restoreCookieJsonFromRows();
  }
});
cookieList.addEventListener("change", (event) => {
  if (event.target.matches("input, select")) {
    markCookieDirty();
    restoreCookieJsonFromRows();
  }
});
cookieDialog.addEventListener("click", (event) => {
  if (event.target === cookieDialog) {
    closeCookieDialog();
  }
});
browserDialogOkButton.addEventListener("click", () => respondToBrowserDialog(true));
browserDialogCancelButton.addEventListener("click", () => respondToBrowserDialog(false));
browserDialog.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !browserDialogCancelButton.hidden) {
    event.preventDefault();
    respondToBrowserDialog(false);
  } else if (event.key === "Enter" && document.activeElement !== browserDialogInput) {
    event.preventDefault();
    respondToBrowserDialog(true);
  }
});
browserDialogInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    respondToBrowserDialog(true);
  }
});

function cookieDefaultDomain() {
  try {
    return new URL(addressInput.value).hostname || "";
  } catch {
    return "";
  }
}

function cookieDefaultSecure() {
  try {
    return new URL(addressInput.value).protocol === "https:";
  } catch {
    return false;
  }
}

function setCookieBusy(busy) {
  cookieAddButton.disabled = busy;
  cookieSaveButton.disabled = busy;
  cookieCancelButton.disabled = busy;
  cookieCloseButton.disabled = busy;
  cookieRefreshButton.disabled = busy;
  cookieJsonInput.disabled = busy;
  cookieJsonRestoreButton.disabled = busy;
  for (const control of cookieList.querySelectorAll("input, select, button")) {
    control.disabled = busy;
  }
}

function normalizeCookiesForSnapshot(cookies) {
  return [...cookies]
    .map((cookie) => ({
      name: String(cookie.name || ""),
      value: String(cookie.value ?? ""),
      domain: String(cookie.domain || ""),
      path: String(cookie.path || "/"),
      expires:
        typeof cookie.expires === "number" && Number.isFinite(cookie.expires)
          ? cookie.expires
          : null,
      httpOnly: Boolean(cookie.httpOnly),
      secure: Boolean(cookie.secure),
      sameSite: cookie.sameSite || null,
      partitionKey: cookie.partitionKey || null,
    }))
    .sort((left, right) =>
      `${left.domain}\n${left.path}\n${left.name}`.localeCompare(
        `${right.domain}\n${right.path}\n${right.name}`
      )
    );
}

function cookieSnapshot(cookies) {
  return JSON.stringify(normalizeCookiesForSnapshot(cookies));
}

function markCookieDirty() {
  if (!cookieDialog.hidden) {
    state.cookieDirty = true;
    updateCookieSyncStatus();
  }
}

function clearCookieSyncState(cookies) {
  state.cookieSnapshot = cookieSnapshot(cookies);
  state.cookieDirty = false;
  state.cookieServerChanged = false;
  updateCookieSyncStatus();
}

function updateCookieSyncStatus() {
  if (cookieDialog.hidden) {
    return;
  }
  if (state.cookieServerChanged && state.cookieDirty) {
    cookieSyncStatus.textContent = "Server cookies changed. Refresh to load them.";
    cookieSyncStatus.hidden = false;
    return;
  }
  if (state.cookieDirty) {
    cookieSyncStatus.textContent = "Unsaved local changes.";
    cookieSyncStatus.hidden = false;
    return;
  }
  cookieSyncStatus.textContent = "";
  cookieSyncStatus.hidden = true;
}

function startCookieSync() {
  stopCookieSync();
  state.cookieSyncTimer = window.setInterval(() => {
    syncCookiesFromServer();
  }, COOKIE_SYNC_INTERVAL_MS);
}

function stopCookieSync() {
  if (state.cookieSyncTimer !== null) {
    window.clearInterval(state.cookieSyncTimer);
    state.cookieSyncTimer = null;
  }
  state.cookieSyncInFlight = false;
}

function openCookieDialog() {
  if (state.locked || !state.sessionId) {
    return;
  }
  cookieDialog.hidden = false;
  cookieSummary.textContent = addressInput.value || "Current page";
  cookieError.textContent = "";
  cookieSyncStatus.hidden = true;
  state.cookieSnapshot = "";
  state.cookieDirty = false;
  state.cookieServerChanged = false;
  cookieList.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "cookie-empty";
  empty.textContent = "Loading cookies...";
  cookieList.append(empty);
  updateCookieCount();
  restoreCookieJsonFromRows();
  loadCookies({ force: true });
  startCookieSync();
}

function closeCookieDialog(restoreFocus = true) {
  if (cookieDialog.hidden) {
    return;
  }
  stopCookieSync();
  cookieDialog.hidden = true;
  cookieError.textContent = "";
  cookieSyncStatus.hidden = true;
  if (restoreFocus && (!state.mobileClient || state.remoteTextFocused)) {
    focusInputProxy();
  }
}

async function fetchCookies() {
  if (!state.sessionId) {
    return [];
  }
  const response = await fetch(`/api/sessions/${state.sessionId}/cookies`);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || "Could not load cookies.");
  }
  return body.cookies || [];
}

function applyCookiesToEditor(cookies) {
  cookieList.replaceChildren();
  for (const cookie of cookies) {
    addCookieRow(cookie, false);
  }
  if (!cookieList.children.length) {
    showEmptyCookies("No cookies for this page.");
  } else {
    updateCookieCount();
    restoreCookieJsonFromRows();
  }
  cookieSummary.textContent = addressInput.value || "Current page";
}

async function loadCookies({ force = false } = {}) {
  if (!state.sessionId) {
    return;
  }
  setCookieBusy(true);
  try {
    const cookies = await fetchCookies();
    const nextSnapshot = cookieSnapshot(cookies);
    if (!force && state.cookieDirty && nextSnapshot !== state.cookieSnapshot) {
      state.cookieServerChanged = true;
      updateCookieSyncStatus();
      return;
    }
    applyCookiesToEditor(cookies);
    clearCookieSyncState(cookies);
  } catch (error) {
    cookieError.textContent = error.message;
    if (force) {
      showEmptyCookies("Cookies are unavailable.");
    }
  } finally {
    setCookieBusy(false);
  }
}

async function syncCookiesFromServer() {
  if (cookieDialog.hidden || !state.sessionId || state.cookieSyncInFlight) {
    return;
  }
  state.cookieSyncInFlight = true;
  try {
    const cookies = await fetchCookies();
    const nextSnapshot = cookieSnapshot(cookies);
    if (nextSnapshot === state.cookieSnapshot) {
      return;
    }
    if (state.cookieDirty) {
      state.cookieServerChanged = true;
      updateCookieSyncStatus();
      return;
    }
    applyCookiesToEditor(cookies);
    clearCookieSyncState(cookies);
  } catch (error) {
    if (!state.cookieDirty) {
      cookieError.textContent = error.message;
    }
  } finally {
    state.cookieSyncInFlight = false;
  }
}

function showEmptyCookies(text) {
  cookieList.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "cookie-empty";
  empty.textContent = text;
  cookieList.append(empty);
  updateCookieCount();
  restoreCookieJsonFromRows();
}

function addCookieRow(cookie, syncJson = true) {
  const empty = cookieList.querySelector(".cookie-empty");
  if (empty) {
    empty.remove();
  }
  const row = document.createElement("div");
  row.className = "cookie-row";
  if (typeof cookie.expires === "number" && cookie.expires > 0) {
    row.dataset.expires = String(cookie.expires);
  }
  if (cookie.partitionKey) {
    row.dataset.partitionKey = cookie.partitionKey;
  }
  row.append(
    createCookieTextField("Name", "name", cookie.name || ""),
    createCookieTextField("Value", "value", cookie.value || ""),
    createCookieTextField("Domain", "domain", cookie.domain || cookieDefaultDomain()),
    createCookieTextField("Path", "path", cookie.path || "/"),
    createSameSiteField(cookie.sameSite || "Lax"),
    createCookieFlags(cookie),
    createCookieRemoveButton()
  );
  cookieList.append(row);
  if (syncJson) {
    restoreCookieJsonFromRows();
  }
}

function createCookieTextField(labelText, field, value) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.type = "text";
  input.dataset.field = field;
  input.value = value;
  input.spellcheck = false;
  label.append(input);
  return label;
}

function createSameSiteField(value) {
  const label = document.createElement("label");
  label.textContent = "SameSite";
  const select = document.createElement("select");
  select.dataset.field = "sameSite";
  for (const optionValue of ["Lax", "Strict", "None"]) {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = optionValue;
    select.append(option);
  }
  select.value = ["Lax", "Strict", "None"].includes(value) ? value : "Lax";
  label.append(select);
  return label;
}

function createCookieFlags(cookie) {
  const flags = document.createElement("div");
  flags.className = "cookie-flags";
  flags.append(
    createCookieCheckbox("Secure", "secure", Boolean(cookie.secure)),
    createCookieCheckbox("HttpOnly", "httpOnly", Boolean(cookie.httpOnly))
  );
  return flags;
}

function createCookieCheckbox(labelText, field, checked) {
  const label = document.createElement("label");
  label.className = "cookie-flag";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.dataset.field = field;
  input.checked = checked;
  label.append(input, document.createTextNode(labelText));
  return label;
}

function createCookieRemoveButton() {
  const button = document.createElement("button");
  button.className = "cookie-remove-button";
  button.type = "button";
  button.title = "Delete";
  button.setAttribute("aria-label", "Delete cookie");
  button.textContent = "x";
  button.addEventListener("click", () => {
    markCookieDirty();
    button.closest(".cookie-row").remove();
    if (!cookieList.querySelector(".cookie-row")) {
      showEmptyCookies("No cookies for this page.");
    } else {
      updateCookieCount();
      restoreCookieJsonFromRows();
    }
  });
  return button;
}

function collectCookies() {
  const cookies = [];
  const seen = new Set();
  for (const row of cookieList.querySelectorAll(".cookie-row")) {
    const name = row.querySelector("[data-field='name']").value.trim();
    const value = row.querySelector("[data-field='value']").value;
    const domain = row.querySelector("[data-field='domain']").value.trim();
    const path = row.querySelector("[data-field='path']").value.trim() || "/";
    const sameSite = row.querySelector("[data-field='sameSite']").value;
    const secure = row.querySelector("[data-field='secure']").checked;
    const httpOnly = row.querySelector("[data-field='httpOnly']").checked;
    if (!name) {
      throw new Error("Cookie name is required.");
    }
    const key = `${name}\n${domain}\n${path}`;
    if (seen.has(key)) {
      throw new Error(`Duplicate cookie: ${name}`);
    }
    seen.add(key);
    const cookie = { name, value, domain, path, sameSite, secure, httpOnly };
    if (row.dataset.expires) {
      cookie.expires = Number(row.dataset.expires);
    }
    if (row.dataset.partitionKey) {
      cookie.partitionKey = row.dataset.partitionKey;
    }
    cookies.push(cookie);
  }
  return cookies;
}

function formatCookiesJson(cookies) {
  return JSON.stringify(cookies, null, 2);
}

function setCookieJsonError(message) {
  cookieError.textContent = message;
  cookieJsonInput.classList.add("invalid");
}

function clearCookieJsonError() {
  if (cookieJsonInput.classList.contains("invalid")) {
    cookieJsonInput.classList.remove("invalid");
  }
  if (cookieError.textContent.startsWith("JSON:")) {
    cookieError.textContent = "";
  }
}

function restoreCookieJsonFromRows() {
  try {
    cookieJsonInput.value = formatCookiesJson(collectCookies());
    clearCookieJsonError();
  } catch (error) {
    setCookieJsonError(`JSON: ${error.message}`);
  }
}

function normalizeCookieFromJson(cookie) {
  if (!cookie || typeof cookie !== "object" || Array.isArray(cookie)) {
    throw new Error("Each cookie must be an object.");
  }
  const name = String(cookie.name || "").trim();
  if (!name) {
    throw new Error("Cookie name is required.");
  }
  const sameSite = cookie.sameSite || "Lax";
  if (!["Lax", "Strict", "None"].includes(sameSite)) {
    throw new Error(`Invalid SameSite for ${name}.`);
  }
  const normalized = {
    name,
    value: String(cookie.value ?? ""),
    domain: String(cookie.domain || cookieDefaultDomain()).trim(),
    path: String(cookie.path || "/").trim() || "/",
    sameSite,
    secure: Boolean(cookie.secure),
    httpOnly: Boolean(cookie.httpOnly),
  };
  if (cookie.expires !== undefined && cookie.expires !== null && cookie.expires !== "") {
    const expires = Number(cookie.expires);
    if (!Number.isFinite(expires) || expires <= 0) {
      throw new Error(`Invalid expires for ${name}.`);
    }
    normalized.expires = expires;
  }
  if (cookie.partitionKey) {
    normalized.partitionKey = String(cookie.partitionKey);
  }
  return normalized;
}

function parseCookiesJson() {
  let parsed;
  try {
    parsed = JSON.parse(cookieJsonInput.value);
  } catch (error) {
    throw new Error(error.message);
  }
  const rawCookies = Array.isArray(parsed) ? parsed : parsed && parsed.cookies;
  if (!Array.isArray(rawCookies)) {
    throw new Error("JSON must be an array or an object with a cookies array.");
  }
  const cookies = rawCookies.map(normalizeCookieFromJson);
  const seen = new Set();
  for (const cookie of cookies) {
    const key = `${cookie.name}\n${cookie.domain}\n${cookie.path}`;
    if (seen.has(key)) {
      throw new Error(`Duplicate cookie: ${cookie.name}`);
    }
    seen.add(key);
  }
  return cookies;
}

function renderCookieRows(cookies) {
  cookieList.replaceChildren();
  for (const cookie of cookies) {
    addCookieRow(cookie, false);
  }
  if (!cookies.length) {
    showEmptyCookies("No cookies for this page.");
  } else {
    updateCookieCount();
    restoreCookieJsonFromRows();
  }
}

function syncCookieRowsFromJson() {
  let cookies;
  try {
    cookies = parseCookiesJson();
  } catch (error) {
    setCookieJsonError(`JSON: ${error.message}`);
    return;
  }
  clearCookieJsonError();
  renderCookieRows(cookies);
}

async function saveCookies() {
  if (!state.sessionId) {
    return;
  }
  if (cookieJsonInput.classList.contains("invalid")) {
    cookieError.textContent = "Fix the JSON editor or use Restore before saving.";
    return;
  }
  let cookies;
  try {
    cookies = collectCookies();
  } catch (error) {
    cookieError.textContent = error.message;
    return;
  }
  cookieError.textContent = "";
  setCookieBusy(true);
  try {
    const response = await fetch(`/api/sessions/${state.sessionId}/cookies`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookies }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.detail || "Could not save cookies.");
    }
    cookieList.replaceChildren();
    const savedCookies = body.cookies || [];
    for (const cookie of savedCookies) {
      addCookieRow(cookie, false);
    }
    if (!cookieList.children.length) {
      showEmptyCookies("No cookies for this page.");
    } else {
      restoreCookieJsonFromRows();
    }
    updateCookieCount();
    clearCookieSyncState(savedCookies);
    setStatus("Cookies saved");
  } catch (error) {
    cookieError.textContent = error.message;
  } finally {
    setCookieBusy(false);
  }
}

function updateCookieCount() {
  const count = cookieList.querySelectorAll(".cookie-row").length;
  cookieCount.textContent = count === 1 ? "1 cookie" : `${count} cookies`;
}

canvas.addEventListener("contextmenu", (event) => event.preventDefault());

canvas.addEventListener("pointerdown", (event) => {
  unlockAudioPlayback();
  state.lastPointerType = event.pointerType || "";
  if (state.mobileClient && event.pointerType === "touch") {
    return;
  }
  event.preventDefault();
  focusInputProxy(event.clientX, event.clientY);
  captureCanvasPointer(event);
  const point = pointerToRemote(event);
  send({ type: "mouse_down", ...point, button: pointerButton(event) });
});

canvas.addEventListener("pointerup", (event) => {
  if (state.mobileClient && event.pointerType === "touch") {
    return;
  }
  event.preventDefault();
  const point = pointerToRemote(event);
  send({ type: "mouse_up", ...point, button: pointerButton(event) });
  releaseCanvasPointer(event);
});

canvas.addEventListener("pointercancel", (event) => {
  if (state.mobileClient && event.pointerType === "touch") {
    return;
  }
  releaseCanvasPointer(event);
});

canvas.addEventListener("pointermove", (event) => {
  if (state.mobileClient && event.pointerType === "touch") {
    return;
  }
  const now = performance.now();
  if (now - state.lastMoveAt < 32) {
    return;
  }
  state.lastMoveAt = now;
  const point = pointerToRemote(event);
  send({ type: "mouse_move", ...point });
});

canvas.addEventListener("touchstart", handleCanvasTouchStart, { passive: false });
canvas.addEventListener("touchmove", handleCanvasTouchMove, { passive: false });
canvas.addEventListener("touchend", handleCanvasTouchEnd, { passive: false });
canvas.addEventListener("touchcancel", handleCanvasTouchCancel, { passive: false });

canvas.addEventListener(
  "wheel",
  (event) => {
    unlockAudioPlayback();
    event.preventDefault();
    send({ type: "wheel", deltaX: event.deltaX, deltaY: event.deltaY });
  },
  { passive: false }
);

function shouldForwardKeyboard() {
  if (!state.connected) {
    return false;
  }
  if (state.mobileClient && !state.remoteTextFocused) {
    return false;
  }
  const active = document.activeElement;
  return active === inputProxy || active === canvas || active === document.body;
}

function isPasteShortcut(event) {
  return (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v";
}

function isCopyShortcut(event) {
  return (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c";
}

function isCutShortcut(event) {
  return (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "x";
}

function isImeKey(event) {
  return (
    event.isComposing ||
    event.key === "Process" ||
    event.key === "Unidentified" ||
    event.key === "Dead" ||
    event.keyCode === 229
  );
}

function requestRemoteClipboard(cut) {
  if (!state.sessionId) {
    return "";
  }
  const request = new XMLHttpRequest();
  request.open("POST", `/api/sessions/${state.sessionId}/clipboard`, false);
  request.setRequestHeader("Content-Type", "application/json");
  try {
    request.send(JSON.stringify({ cut }));
  } catch {
    setStatus(cut ? "Cut failed" : "Copy failed");
    return "";
  }
  if (request.status < 200 || request.status >= 300) {
    setStatus(cut ? "Cut failed" : "Copy failed");
    return "";
  }
  try {
    const response = JSON.parse(request.responseText);
    return typeof response.text === "string" ? response.text : "";
  } catch {
    return "";
  }
}

function writeClipboardFromGesture(text) {
  state.lastRemoteClipboard = text;
  state.pendingClipboard = text;
  state.writingClipboard = true;
  const previousValue = inputProxy.value;
  const previousStart = inputProxy.selectionStart;
  const previousEnd = inputProxy.selectionEnd;
  inputProxy.value = text || " ";
  inputProxy.focus({ preventScroll: true });
  inputProxy.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } finally {
    state.writingClipboard = false;
    inputProxy.value = previousValue;
    inputProxy.selectionStart = Math.min(previousStart, previousValue.length);
    inputProxy.selectionEnd = Math.min(previousEnd, previousValue.length);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => undefined);
  }
  return copied;
}

function copyOrCutRemoteSelection(cut) {
  const text = requestRemoteClipboard(cut);
  writeClipboardFromGesture(text);
  setStatus(cut ? "Cut" : "Copied");
}

inputProxy.addEventListener("compositionstart", () => {
  state.composing = true;
});

inputProxy.addEventListener("compositionend", () => {
  state.composing = false;
  window.setTimeout(flushInputProxy, 0);
});

inputProxy.addEventListener("input", () => {
  flushInputProxy();
});

inputProxy.addEventListener("paste", (event) => {
  if (!shouldForwardKeyboard()) {
    return;
  }
  event.preventDefault();
  const text = event.clipboardData ? event.clipboardData.getData("text/plain") : "";
  if (text) {
    send({ type: "paste", text });
  }
  clearInputProxy();
});

inputProxy.addEventListener("copy", (event) => {
  event.preventDefault();
  if (state.writingClipboard) {
    if (event.clipboardData) {
      event.clipboardData.setData("text/plain", state.pendingClipboard);
    }
    return;
  }
  if (!shouldForwardKeyboard()) {
    return;
  }
  const text = requestRemoteClipboard(false);
  state.lastRemoteClipboard = text;
  if (event.clipboardData) {
    event.clipboardData.setData("text/plain", text);
  }
  setStatus("Copied");
});

inputProxy.addEventListener("cut", (event) => {
  event.preventDefault();
  if (state.writingClipboard) {
    if (event.clipboardData) {
      event.clipboardData.setData("text/plain", state.pendingClipboard);
    }
    return;
  }
  if (!shouldForwardKeyboard()) {
    return;
  }
  const text = requestRemoteClipboard(true);
  state.lastRemoteClipboard = text;
  if (event.clipboardData) {
    event.clipboardData.setData("text/plain", text);
  }
  setStatus("Cut");
});

inputProxy.addEventListener("keydown", (event) => {
  unlockAudioPlayback();
  if (!shouldForwardKeyboard()) {
    return;
  }
  if (isImeKey(event)) {
    return;
  }
  if (isPasteShortcut(event)) {
    return;
  }
  if (isCopyShortcut(event)) {
    event.preventDefault();
    copyOrCutRemoteSelection(false);
    return;
  }
  if (isCutShortcut(event)) {
    event.preventDefault();
    copyOrCutRemoteSelection(true);
    return;
  }
  if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
    return;
  }
  event.preventDefault();
  send({
    type: "key",
    key: event.key,
    ctrlKey: event.ctrlKey,
    altKey: event.altKey,
    shiftKey: event.shiftKey,
    metaKey: event.metaKey,
  });
});

window.addEventListener("resize", () => {
  updateFrameDisplay();
  window.clearTimeout(window.__agentResizeTimer);
  window.__agentResizeTimer = window.setTimeout(sendResize, 160);
});

if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => {
    updateFrameDisplay();
    window.clearTimeout(window.__agentResizeTimer);
    window.__agentResizeTimer = window.setTimeout(sendResize, 160);
  });
}

window.addEventListener("popstate", () => {
  if (!state.mobileClient || !state.mobileBackGuardInstalled) {
    return;
  }
  handleMobileBrowserBack();
});

async function startLockedSession() {
  if (!state.lockUrl) {
    launchError.textContent = "Locked URL is not configured.";
    setStatus("Idle");
    return;
  }
  setStatus("Opening");
  showLoading();
  try {
    const session = await createSession(state.lockUrl);
    state.sessionId = session.session_id;
    addressInput.value = session.url || state.lockUrl;
    connectSession(state.sessionId);
  } catch (error) {
    hideLoading();
    setStatus("Error");
    launchError.textContent = error.message;
  }
}

async function initializeApp() {
  try {
    state.mobileClient = detectMobileClient();
    document.body.classList.toggle("mobile-client", state.mobileClient);
    installMobileBackGuard();
    await loadClientConfig();
    applyLockedMode();
    if (await restoreCurrentSession()) {
      return;
    }
    if (state.locked) {
      await startLockedSession();
    }
  } catch (error) {
    hideLoading();
    launcher.hidden = false;
    launchError.textContent = error.message;
    setStatus("Idle");
  }
}

initializeApp();

function startDownload(message) {
  const link = document.createElement("a");
  link.href = message.url;
  link.download = message.filename || "";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function openFileChooser(message) {
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = Boolean(message.multiple);
  input.style.display = "none";
  document.body.appendChild(input);
  input.addEventListener("change", async () => {
    if (!input.files || input.files.length === 0) {
      input.remove();
      if (!state.mobileClient || state.remoteTextFocused) {
        focusInputProxy();
      }
      return;
    }
    const form = new FormData();
    for (const file of input.files) {
      form.append("files", file);
    }
    try {
      const response = await fetch(`/api/sessions/${state.sessionId}/file-chooser/${message.token}`, {
        method: "POST",
        body: form,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || "File upload failed.");
      }
      setStatus("Files selected");
    } catch (error) {
      setStatus("File upload failed");
      launchError.textContent = error.message || "File upload failed.";
    } finally {
      input.remove();
      if (!state.mobileClient || state.remoteTextFocused) {
        focusInputProxy();
      }
    }
  });
  input.click();
}

async function handleRemoteClipboard(text) {
  state.lastRemoteClipboard = text;
  if (!text) {
    setStatus("Clipboard empty");
    return;
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      setStatus("Copied");
      return;
    }
  } catch {
    // Fall back to execCommand below.
  }

  writeClipboardFromGesture(text);
  setStatus("Copied");
}
