// Page-lifetime transcript only. No localStorage, HTML rendering, reasoning or tools.
export function createTextChat({ onActivate, onBusy, onEmotion }) {
  const input = document.getElementById("message");
  const form = document.getElementById("compose");
  const log = document.getElementById("messages");
  const status = document.getElementById("chat-status");
  const entries = [];
  const voiceReplies = new Map();
  let voiceUser = null;
  let request = null;
  let busy = false;

  function atBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 64; }
  function resize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(160, input.scrollHeight)}px`;
  }
  function activate() {
    const opened = !document.body.classList.contains("is-typing");
    if (opened) document.body.classList.add("is-typing");
    onActivate();
    if (opened) log.scrollTop = log.scrollHeight;
  }
  function add(role, content, complete = true) {
    const follow = atBottom();
    const node = document.createElement("p");
    node.className = "message";
    node.dataset.role = role;
    node.setAttribute("aria-label", role === "user" ? "Tú" : "Asistente");
    node.textContent = content;
    const entry = { role, content, complete, node };
    entries.push(entry);
    log.append(node);
    if (follow) log.scrollTop = log.scrollHeight;
    return entry;
  }
  function update(entry, text, complete = entry.complete) {
    const follow = atBottom();
    entry.content = text;
    entry.complete = complete;
    entry.node.textContent = text;
    entry.node.hidden = !text;
    if (follow) log.scrollTop = log.scrollHeight;
  }
  function context() {
    // Display everything, but bound model context. Never replay failed/generated
    // partial replies as if they were completed conversation turns.
    let budget = 32000;
    const messages = [];
    for (const entry of entries.slice().reverse()) {
      if (!entry.complete || !entry.content.trim()) continue;
      if (entry.content.length > budget || messages.length >= 16) break;
      budget -= entry.content.length;
      messages.unshift({ role: entry.role, content: entry.content });
    }
    return messages;
  }
  function setBusy(value) {
    busy = value;
    form.setAttribute("aria-busy", String(value));
    status.textContent = value ? "Pensando" : "";
    onBusy(value);
  }
  async function send(event) {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    activate();
    add("user", text);
    const messages = context();
    input.value = "";
    resize();
    log.scrollTop = log.scrollHeight;
    const controller = new AbortController();
    request = controller;
    setBusy(true);
    try {
      const response = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages }), signal: controller.signal,
      });
      if (!response.ok) throw new Error("request failed");
      const data = await response.json();
      if (request !== controller) return;
      const answer = data.choices?.[0]?.message?.content;
      if (typeof answer !== "string" || !answer.trim()) throw new Error("empty reply");
      add("assistant", answer.trim());
      // HTTP emotion is a visual-only effect; reasoning/tool results stay hidden.
      const emotion = data.dot_tools?.find(tool => tool.name === "emotion" && !tool.result?.startsWith("error:"));
      if (emotion) {
        try { onEmotion(JSON.parse(emotion.arguments).name); } catch { /* optional */ }
      }
    } catch (error) {
      if (request !== controller) return;
      if (error.name !== "AbortError") {
        add("assistant", "No he podido responder. Inténtalo de nuevo.", false);
        status.textContent = "No se ha podido enviar el mensaje";
      }
    } finally {
      if (request === controller) {
        request = null;
        setBusy(false);
      }
    }
  }
  function cancel() {
    const controller = request;
    request = null;
    controller?.abort();
    setBusy(false);
  }
  function voiceMessage(message) {
    if (message.type === "final" && message.text?.trim()) {
      voiceUser = message.turn_id ?? "current";
      add("user", message.text.trim());
    } else if (message.type === "assistant_delta" && message.text) {
      const id = message.turn_id ?? voiceUser;
      let entry = voiceReplies.get(id);
      if (!entry) {
        entry = add("assistant", "", false);
        voiceReplies.set(id, entry);
      }
      update(entry, entry.content + message.text);
    } else if (message.type === "assistant_text") {
      const entry = voiceReplies.get(message.turn_id);
      if (entry) update(entry, message.text || "", true);
    }
  }

  form.addEventListener("submit", send);
  input.addEventListener("input", () => { activate(); resize(); });
  input.addEventListener("focus", activate);
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  document.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (event.key === "Escape" && !input.value && !busy) {
      document.body.classList.remove("is-typing");
      input.blur();
      return;
    }
    if (target instanceof HTMLElement && (target.isContentEditable || target.closest("input, textarea, select"))) return;
    if (event.key.length === 1 && event.key.trim()) {
      event.preventDefault();
      activate();
      input.focus();
      input.value = (input.value + event.key).slice(0, input.maxLength);
      resize();
    }
  });
  document.addEventListener("paste", event => {
    if (event.target === input || event.target.closest?.("input, textarea, [contenteditable]")) return;
    const text = event.clipboardData?.getData("text/plain");
    if (!text) return;
    event.preventDefault();
    activate();
    input.focus();
    input.value = (input.value + text).slice(0, input.maxLength);
    resize();
  });
  window.addEventListener("pagehide", cancel);
  return {
    cancel, context, voiceMessage,
    beginVoiceSession() { voiceReplies.clear(); voiceUser = null; },
    interruptVoice() {
      const entry = voiceReplies.get(voiceUser);
      if (entry) entry.complete = false;
    },
    get busy() { return busy; },
  };
}
