(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const layout = $("layout");
  const chat = $("chat");
  const messagesEl = $("messages");
  const welcome = $("welcome");
  const form = $("composer");
  const input = $("input");
  const sendBtn = $("send");
  const stopBtn = $("stop");
  const historyEl = $("history");
  const searchEl = $("search");
  const chatTitle = $("chat-title");
  const banner = $("banner");
  const bannerText = $("banner-text");
  const menu = $("menu");
  const dialog = $("confirm");
  const dialogName = $("confirm-name");
  const mobileQuery = window.matchMedia("(max-width: 860px)");

  const state = {
    id: null,         // currently open conversation (null = new chat)
    list: [],         // [{id, title, updated_at}] newest first
    busy: false,      // a reply is being generated
    controller: null, // AbortController of the in-flight request
    view: 0,          // bumped on every chat switch, so stale requests don't touch the DOM
  };

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
  };

  /* ---------- API ---------- */
  /* Session expired (or logged out in another tab): go back to the login page. */
  function redirectToLogin() {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
  }

  async function api(path, options = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    if (res.status === 401) { redirectToLogin(); throw Object.assign(new Error("Please log in."), { status: 401 }); }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw Object.assign(new Error(data.error || `Request failed (${res.status})`), { status: res.status });
    }
    return res.status === 204 ? null : res.json();
  }

  /* ---------- Theme & sidebar ---------- */
  $("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("theme", next);
  });

  function setSidebar(open, persist = true) {
    layout.dataset.sidebar = open ? "open" : "closed";
    $("sidebar-toggle").setAttribute("aria-expanded", String(open));
    if (persist && !mobileQuery.matches) store.set("sidebar", open ? "open" : "closed");
  }
  setSidebar(mobileQuery.matches ? false : store.get("sidebar") !== "closed", false);

  $("sidebar-toggle").addEventListener("click", () => setSidebar(true));
  $("sidebar-close").addEventListener("click", () => setSidebar(false));
  $("scrim").addEventListener("click", () => setSidebar(false));
  mobileQuery.addEventListener("change", (e) => setSidebar(e.matches ? false : store.get("sidebar") !== "closed", false));

  /* ---------- Minimal, safe Markdown renderer ---------- */
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function inline(text) {
    const codes = [];
    text = text.replace(/`([^`\n]+)`/g, (_, c) => `\u0000${codes.push(`<code>${c}</code>`) - 1}\u0000`);
    text = text
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return text.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[+i]);
  }

  function render(src) {
    const blocks = [];
    // Fenced code blocks first (also handles an unterminated fence while streaming).
    let text = src.replace(/```([\w+-]*)\n([\s\S]*?)(?:```|$)/g, (_, lang, code) => {
      const html =
        `<pre><div class="code-head"><span>${esc(lang || "code")}</span>` +
        `<button type="button" class="copy">Copy</button></div>` +
        `<code>${esc(code.replace(/\n$/, ""))}</code></pre>`;
      return `\u0001${blocks.push(html) - 1}\u0001`;
    });
    text = esc(text);

    const out = [];
    let list = null;
    let para = [];
    const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; } };
    const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

    for (const line of text.split("\n")) {
      let m;
      if ((m = line.match(/^\u0001(\d+)\u0001$/))) {
        flushPara(); closeList(); out.push(blocks[+m[1]]);
      } else if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
        flushPara(); closeList();
        out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`);
      } else if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
        flushPara();
        if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
        out.push(`<li>${inline(m[1])}</li>`);
      } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        flushPara();
        if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
        out.push(`<li>${inline(m[1])}</li>`);
      } else if ((m = line.match(/^&gt;\s?(.*)$/))) {
        flushPara(); closeList();
        out.push(`<blockquote>${inline(m[1])}</blockquote>`);
      } else if (!line.trim()) {
        flushPara(); closeList();
      } else {
        closeList(); para.push(line);
      }
    }
    flushPara(); closeList();
    return out.join("");
  }

  /* ---------- Message UI ---------- */
  const scrollDown = () => { chat.scrollTop = chat.scrollHeight; };
  const nearBottom = () => chat.scrollHeight - chat.scrollTop - chat.clientHeight < 140;

  const AVATAR_ROBOT =
    '<svg viewBox="0 0 40 40" width="22" height="22"><line class="r-stroke-w" x1="20" y1="3" x2="20" y2="8" stroke-width="2" stroke-linecap="round"/>' +
    '<circle class="r-antenna" cx="20" cy="3.5" r="2.2"/><rect x="5" y="8" width="30" height="24" rx="8" fill="#fff"/>' +
    '<ellipse class="r-eye-a" cx="14" cy="20" rx="3" ry="3.6"/><ellipse class="r-eye-a" cx="26" cy="20" rx="3" ry="3.6"/></svg>';

  function addMessage(role, content) {
    welcome.hidden = true;
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;

    if (role === "assistant") {
      const avatar = document.createElement("div");
      avatar.className = "avatar";
      avatar.classList.add("robot");
      avatar.innerHTML = AVATAR_ROBOT;
      avatar.setAttribute("aria-hidden", "true");
      wrap.appendChild(avatar);
    }
    const bubble = document.createElement("div");
    bubble.className = "bubble" + (role === "assistant" ? " md" : "");
    if (role === "assistant") bubble.innerHTML = content;
    else bubble.textContent = content;
    wrap.appendChild(bubble);

    messagesEl.appendChild(wrap);
    scrollDown();
    return bubble;
  }

  const TYPING = '<div class="typing" aria-label="Assistant is typing"><span></span><span></span><span></span></div>';

  function showBanner(message) { bannerText.textContent = message; banner.hidden = false; }
  function hideBanner() { banner.hidden = true; }
  $("banner-close").addEventListener("click", hideBanner);

  function setBusy(busy) {
    state.busy = busy;
    stopBtn.hidden = !busy;
    sendBtn.hidden = busy;
    if (!busy) updateSend();
  }
  const updateSend = () => { sendBtn.disabled = !input.value.trim() || quota.remaining <= 0; };

  /* ---------- Free-message quota ---------- */
  const quotaEl = $("quota");
  const quota = { remaining: Number(quotaEl.dataset.remaining), limit: Number(quotaEl.dataset.limit) };
  function setQuota(remaining) {
    quota.remaining = Math.max(0, remaining);
    const left = quota.remaining;
    quotaEl.textContent = left > 0
      ? `${left} of ${quota.limit} free message${left === 1 ? "" : "s"} left`
      : `No free messages left (${quota.limit} of ${quota.limit} used)`;
    quotaEl.classList.toggle("low", left > 0 && left <= 3);
    quotaEl.classList.toggle("out", left === 0);
    input.placeholder = left > 0 ? "Message the assistant…" : "You've used all your free messages";
    updateSend();
  }
  setQuota(quota.remaining);

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }

  function setTitle(title) { chatTitle.textContent = title || "New chat"; document.title = title ? `${title} · Bedrock Chat` : "Bedrock Chat"; }

  /* ---------- Sidebar history ---------- */
  function groupLabel(iso) {
    const d = new Date(iso);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days <= 7) return "Previous 7 days";
    if (days <= 30) return "Previous 30 days";
    return "Older";
  }

  const MORE_ICON =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';

  function renderHistory() {
    const query = searchEl.value.trim().toLowerCase();
    const items = state.list.filter((c) => c.title.toLowerCase().includes(query));
    historyEl.replaceChildren();

    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "history-empty";
      empty.textContent = query ? "No chats match your search." : "Your conversations will appear here.";
      historyEl.appendChild(empty);
      return;
    }

    let lastGroup = null;
    for (const conv of items) {
      const group = groupLabel(conv.updated_at);
      if (group !== lastGroup) {
        const label = document.createElement("h2");
        label.className = "group-label";
        label.textContent = group;
        historyEl.appendChild(label);
        lastGroup = group;
      }

      const row = document.createElement("div");
      row.className = "chat-item" + (conv.id === state.id ? " active" : "");
      row.dataset.id = conv.id;

      const link = document.createElement("a");
      link.className = "chat-link";
      link.href = `/c/${conv.id}`;
      link.textContent = conv.title;
      link.title = conv.title;
      if (conv.id === state.id) link.setAttribute("aria-current", "page");

      const more = document.createElement("button");
      more.type = "button";
      more.className = "more";
      more.setAttribute("aria-label", `Options for ${conv.title}`);
      more.setAttribute("aria-haspopup", "menu");
      more.setAttribute("aria-expanded", "false");
      more.innerHTML = MORE_ICON;

      row.append(link, more);
      historyEl.appendChild(row);
    }
  }

  async function loadList() {
    try {
      state.list = (await api("/api/conversations")).conversations;
    } catch (err) {
      showBanner(err.message);
    }
    renderHistory();
  }

  function touchConversation(id, title) {
    const existing = state.list.find((c) => c.id === id);
    state.list = state.list.filter((c) => c.id !== id);
    state.list.unshift({ id, title: title ?? existing?.title ?? "New chat", updated_at: new Date().toISOString() });
    renderHistory();
  }

  historyEl.addEventListener("click", (e) => {
    const link = e.target.closest(".chat-link");
    if (link) {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // let the browser open a new tab
      e.preventDefault();
      openConversation(link.closest(".chat-item").dataset.id);
      return;
    }
    const more = e.target.closest(".more");
    if (more) {
      if (menu.hidden || menu.dataset.id !== more.closest(".chat-item").dataset.id) openMenu(more);
      else closeMenu();
    }
  });

  searchEl.addEventListener("input", renderHistory);

  /* ---------- Row menu: rename / delete ---------- */
  let menuTrigger = null;

  function openMenu(button) {
    closeMenu();
    menuTrigger = button;
    menu.dataset.id = button.closest(".chat-item").dataset.id;
    button.setAttribute("aria-expanded", "true");
    menu.hidden = false;
    const r = button.getBoundingClientRect();
    menu.style.top = `${Math.max(8, Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 8))}px`;
    menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8))}px`;
    menu.querySelector("button").focus();
  }

  function closeMenu() {
    if (menuTrigger) menuTrigger.setAttribute("aria-expanded", "false");
    menuTrigger = null;
    menu.hidden = true;
  }

  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !e.target.closest(".more")) closeMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.hidden) { const t = menuTrigger; closeMenu(); t && t.focus(); }
  });
  historyEl.addEventListener("scroll", closeMenu);
  window.addEventListener("resize", closeMenu);

  menu.addEventListener("click", (e) => {
    const action = e.target.closest("button")?.dataset.action;
    if (!action) return;
    const id = menu.dataset.id;
    closeMenu();
    if (action === "rename") startRename(id);
    if (action === "delete") deleteConversation(id);
  });

  function startRename(id) {
    const row = historyEl.querySelector(`.chat-item[data-id="${id}"]`);
    const conv = state.list.find((c) => c.id === id);
    if (!row || !conv) return;

    const field = document.createElement("input");
    field.className = "rename";
    field.value = conv.title;
    field.maxLength = 100;
    field.setAttribute("aria-label", "Chat title");
    row.querySelector(".chat-link").replaceWith(field);
    field.focus();
    field.select();

    let finished = false;
    const finish = async (save) => {
      if (finished) return;
      finished = true;
      const title = field.value.trim();
      if (save && title && title !== conv.title) {
        try {
          await api(`/api/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
          conv.title = title;
          if (state.id === id) setTitle(title);
        } catch (err) {
          showBanner(err.message);
        }
      }
      renderHistory();
    };
    field.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") finish(false);
    });
    field.addEventListener("blur", () => finish(true));
  }

  function confirmDelete(title) {
    dialogName.textContent = title;
    dialog.returnValue = "";
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
      dialog.showModal();
    });
  }

  async function deleteConversation(id) {
    const conv = state.list.find((c) => c.id === id);
    if (!conv || !(await confirmDelete(conv.title))) return;
    try {
      await api(`/api/conversations/${id}`, { method: "DELETE" });
    } catch (err) {
      if (err.status !== 404) { showBanner(err.message); return; }
    }
    state.list = state.list.filter((c) => c.id !== id);
    if (state.id === id) newChat(); else renderHistory();
  }

  /* ---------- Switching chats ---------- */
  function newChat(push = true) {
    if (state.controller) state.controller.abort();
    state.view++;
    state.id = null;
    messagesEl.replaceChildren();
    welcome.hidden = false;
    setTitle(null);
    hideBanner();
    input.value = "";
    autosize();
    updateSend();
    setBusy(false);
    renderHistory();
    if (push && location.pathname !== "/") history.pushState({}, "", "/");
    if (mobileQuery.matches) setSidebar(false);
    input.focus();
  }

  async function openConversation(id, push = true) {
    if (state.controller) state.controller.abort();
    const view = ++state.view;
    hideBanner();
    let data;
    try {
      data = await api(`/api/conversations/${id}`);
    } catch (err) {
      if (view !== state.view) return;
      history.replaceState({}, "", "/");
      newChat(false);
      showBanner(err.status === 404 ? "That conversation no longer exists." : err.message);
      return;
    }
    if (view !== state.view) return;

    state.id = id;
    setBusy(false);
    messagesEl.replaceChildren();
    for (const m of data.messages) addMessage(m.role, m.role === "assistant" ? render(m.content) : m.content);
    welcome.hidden = data.messages.length > 0;
    setTitle(data.title);
    renderHistory();
    if (push && location.pathname !== `/c/${id}`) history.pushState({}, "", `/c/${id}`);
    if (mobileQuery.matches) setSidebar(false);
    scrollDown();
    input.focus();
  }

  function route() {
    const match = location.pathname.match(/^\/c\/([\w-]+)$/);
    if (match) openConversation(match[1], false); else newChat(false);
  }
  window.addEventListener("popstate", route);

  /* ---------- Sending ---------- */
  async function send(text) {
    if (state.busy) return;
    if (quota.remaining <= 0) { showBanner(quotaEl.dataset.exhaustedMessage); return; }
    hideBanner();
    const quotaBefore = quota.remaining;
    let limitHit = false;

    const view = state.view;
    const wasNew = !state.id;
    const current = () => view === state.view; // false once the user switched to another chat

    const userBubble = addMessage("user", text);
    const bubble = addMessage("assistant", TYPING);
    input.value = "";
    autosize();
    updateSend();

    let reply = "";
    let failed = null;
    let convId = null;
    const controller = new AbortController();
    state.controller = controller;
    setBusy(true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: state.id, message: text }),
        signal: controller.signal,
      });
      if (res.status === 401) { redirectToLogin(); return; }
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (res.status === 402) { limitHit = true; setQuota(0); }
        throw new Error(data.error || `Request failed (${res.status})`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      const handle = (line) => {
        if (!line.trim()) return;
        const evt = JSON.parse(line);
        if (evt.quota) {
          setQuota(evt.quota.remaining);
          if (evt.error) limitHit = true; // another tab used the last message
        }
        if (evt.error) throw new Error(evt.error);
        if (evt.conversation) {
          convId = evt.conversation.id;
          touchConversation(evt.conversation.id, evt.conversation.title);
          if (current()) {
            state.id = evt.conversation.id;
            setTitle(evt.conversation.title);
            if (wasNew) history.pushState({}, "", `/c/${state.id}`);
          }
        } else if (evt.delta) {
          reply += evt.delta;
          if (current()) {
            const stick = nearBottom();
            bubble.innerHTML = render(reply);
            if (stick) scrollDown();
          }
        } else if (evt.done) {
          if (current()) setBusy(false);
        } else if (evt.title) {
          const conv = state.list.find((c) => c.id === evt.id);
          if (conv) conv.title = evt.title;
          if (current()) setTitle(evt.title);
          renderHistory();
        }
      };

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        lines.forEach(handle);
      }
      handle(buffer);
    } catch (err) {
      if (err.name !== "AbortError") failed = err.message || "Network error. Please try again.";
    } finally {
      if (state.controller === controller) state.controller = null;
      if (current()) setBusy(false);

      // Nothing was stored server-side when the request failed or was stopped before any text.
      const saved = !failed && reply.length > 0;
      if (!saved) {
        if (!limitHit) setQuota(quotaBefore); // the server gives the reserved message back
        if (wasNew) {
          state.list = state.list.filter((c) => c.id !== convId);
          renderHistory();
        }
        if (current()) {
          userBubble.closest(".msg").remove();
          bubble.closest(".msg").remove();
          if (wasNew) {
            state.id = null;
            setTitle(null);
            history.replaceState({}, "", "/");
            welcome.hidden = messagesEl.children.length > 0;
          }
          input.value = text; // give the text back so it can be retried
          autosize();
          updateSend();
          if (failed) showBanner(failed);
        }
      }
    }
  }

  /* ---------- Events ---------- */
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (text && !state.busy) send(text);
  });

  input.addEventListener("input", () => { autosize(); updateSend(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  stopBtn.addEventListener("click", () => state.controller && state.controller.abort());
  $("new-chat").addEventListener("click", () => newChat());
  $("top-new").addEventListener("click", () => newChat());
  $("brand-link").addEventListener("click", (e) => { e.preventDefault(); newChat(); });

  document.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => send(chip.dataset.prompt))
  );

  messagesEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".copy");
    if (!btn) return;
    navigator.clipboard.writeText(btn.closest("pre").querySelector("code").textContent).then(() => {
      btn.textContent = "Copied!";
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    });
  });

  /* ---------- Boot ---------- */
  loadList().then(route);
})();
