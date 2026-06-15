const state = {
  sessionId: null,
  socket: null,
  frameWidth: 1280,
  frameHeight: 720,
  connected: false,
  composing: false,
  lastMoveAt: 0,
  lastRemoteClipboard: "",
  writingClipboard: false,
  pendingClipboard: "",
};
window.websiteAgentState = state;

const launcher = document.getElementById("launcher");
const launchForm = document.getElementById("launch-form");
const launchError = document.getElementById("launch-error");
const targetUrl = document.getElementById("target-url");
const addressForm = document.getElementById("address-form");
const addressInput = document.getElementById("address-input");
const backButton = document.getElementById("back-button");
const forwardButton = document.getElementById("forward-button");
const reloadButton = document.getElementById("reload-button");
const goButton = document.getElementById("go-button");
const statusText = document.getElementById("status-text");
const viewportWrap = document.getElementById("viewport-wrap");
const canvas = document.getElementById("viewport");
const inputProxy = document.getElementById("input-proxy");
const context = canvas.getContext("2d", { alpha: false });

function setStatus(text) {
  statusText.textContent = text;
}

function setControlsEnabled(enabled) {
  addressInput.disabled = !enabled;
  backButton.disabled = !enabled;
  forwardButton.disabled = !enabled;
  reloadButton.disabled = !enabled;
  goButton.disabled = !enabled;
}

function measureViewport() {
  const rect = viewportWrap.getBoundingClientRect();
  return {
    width: Math.max(320, Math.min(1920, Math.floor(rect.width || 1280))),
    height: Math.max(240, Math.min(1600, Math.floor(rect.height || 720))),
  };
}

async function createSession(url) {
  const viewport = measureViewport();
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, width: viewport.width, height: viewport.height }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || "Could not open the target site.");
  }
  return body;
}

function connectSession(sessionId) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/${sessionId}`);
  state.socket = socket;

  socket.addEventListener("open", () => {
    state.connected = true;
    setControlsEnabled(true);
    setStatus("Connected");
    sendResize();
    focusInputProxy();
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    handleServerMessage(message);
  });

  socket.addEventListener("close", () => {
    state.connected = false;
    setControlsEnabled(false);
    setStatus("Disconnected");
  });
}

function handleServerMessage(message) {
  if (message.type === "frame") {
    drawFrame(message);
  } else if (message.type === "status") {
    if (message.url) {
      addressInput.value = message.url;
    }
    setStatus(message.state || "Ready");
  } else if (message.type === "error") {
    setStatus("Error");
    launchError.textContent = message.message || "Request failed.";
  } else if (message.type === "warning") {
    setStatus(message.message || "Warning");
  } else if (message.type === "blocked") {
    setStatus("Blocked");
  } else if (message.type === "download") {
    startDownload(message);
  } else if (message.type === "filechooser") {
    openFileChooser(message);
  } else if (message.type === "dialog") {
    setStatus(message.dialogType || "Dialog");
  } else if (message.type === "clipboard") {
    handleRemoteClipboard(message.text || "");
  }
}

function drawFrame(message) {
  state.frameWidth = message.width;
  state.frameHeight = message.height;
  if (addressInput !== document.activeElement && message.url) {
    addressInput.value = message.url;
  }
  const image = new Image();
  image.onload = () => {
    if (canvas.width !== message.width || canvas.height !== message.height) {
      canvas.width = message.width;
      canvas.height = message.height;
    }
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
  };
  image.src = `data:${message.mime};base64,${message.image}`;
}

function send(message) {
  if (!state.connected || !state.socket || state.socket.readyState !== WebSocket.OPEN) {
    return;
  }
  state.socket.send(JSON.stringify(message));
}

function focusInputProxy(clientX, clientY) {
  if (typeof clientX === "number" && typeof clientY === "number") {
    inputProxy.style.left = `${Math.max(0, Math.floor(clientX))}px`;
    inputProxy.style.top = `${Math.max(0, Math.floor(clientY))}px`;
  }
  inputProxy.focus({ preventScroll: true });
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
    send({ type: "text", text });
    clearInputProxy();
  }
}

function sendResize() {
  if (!state.sessionId) {
    return;
  }
  const viewport = measureViewport();
  send({ type: "resize", width: viewport.width, height: viewport.height });
}

function pointerToRemote(event) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: ((event.clientX - rect.left) * state.frameWidth) / Math.max(1, rect.width),
    y: ((event.clientY - rect.top) * state.frameHeight) / Math.max(1, rect.height),
  };
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

launchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  launchError.textContent = "";
  setStatus("Opening");
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
    setStatus("Idle");
  } finally {
    submitButton.disabled = false;
  }
});

addressForm.addEventListener("submit", (event) => {
  event.preventDefault();
  send({ type: "navigate", url: addressInput.value });
  focusInputProxy();
});

backButton.addEventListener("click", () => send({ type: "back" }));
forwardButton.addEventListener("click", () => send({ type: "forward" }));
reloadButton.addEventListener("click", () => send({ type: "reload" }));

canvas.addEventListener("contextmenu", (event) => event.preventDefault());

canvas.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  focusInputProxy(event.clientX, event.clientY);
  canvas.setPointerCapture(event.pointerId);
  const point = pointerToRemote(event);
  send({ type: "mouse_down", ...point, button: pointerButton(event) });
});

canvas.addEventListener("pointerup", (event) => {
  event.preventDefault();
  const point = pointerToRemote(event);
  send({ type: "mouse_up", ...point, button: pointerButton(event) });
  if (canvas.hasPointerCapture(event.pointerId)) {
    canvas.releasePointerCapture(event.pointerId);
  }
});

canvas.addEventListener("pointermove", (event) => {
  const now = performance.now();
  if (now - state.lastMoveAt < 32) {
    return;
  }
  state.lastMoveAt = now;
  const point = pointerToRemote(event);
  send({ type: "mouse_move", ...point });
});

canvas.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    send({ type: "wheel", deltaX: event.deltaX, deltaY: event.deltaY });
  },
  { passive: false }
);

function shouldForwardKeyboard() {
  if (!state.connected) {
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
  window.clearTimeout(window.__agentResizeTimer);
  window.__agentResizeTimer = window.setTimeout(sendResize, 160);
});

window.addEventListener("beforeunload", () => {
  if (state.sessionId) {
    navigator.sendBeacon(`/api/sessions/${state.sessionId}/close`);
  }
});

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
    const form = new FormData();
    for (const file of input.files) {
      form.append("files", file);
    }
    await fetch(`/api/sessions/${state.sessionId}/file-chooser/${message.token}`, {
      method: "POST",
      body: form,
    });
    input.remove();
    focusInputProxy();
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
