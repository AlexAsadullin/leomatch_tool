(() => {
  const els = {
    priorityAlert: document.getElementById("priorityAlert"),
    warning: document.getElementById("warning"),
    warningTitle: document.getElementById("warningTitle"),
    warningText: document.getElementById("warningText"),
    empty: document.getElementById("empty"),
    profile: document.getElementById("profile"),
    media: document.getElementById("media"),
    desc: document.getElementById("desc"),
    descMore: document.getElementById("descMore"),
    seen: document.getElementById("seen"),
    likeBtn: document.getElementById("likeBtn"),
    dislikeBtn: document.getElementById("dislikeBtn"),
    letterBtn: document.getElementById("letterBtn"),
    letterInput: document.getElementById("letterInput"),
    autoDislikeBtn: document.getElementById("autoDislikeBtn"),
    modeBtn: document.getElementById("modeBtn"),
    autoCount: document.getElementById("autoCount"),
    likeCount: document.getElementById("likeCount"),
    dislikeCount: document.getElementById("dislikeCount"),
    totalCount: document.getElementById("totalCount"),
    ageForm: document.getElementById("ageForm"),
    ageMin: document.getElementById("ageMin"),
    ageMax: document.getElementById("ageMax"),
    ageReset: document.getElementById("ageReset"),
    ageError: document.getElementById("ageError"),
    autoLikeBtn: document.getElementById("autoLikeBtn"),
    activePhone: document.getElementById("activePhone"),
    emptyText: document.getElementById("emptyText"),
    switchAccountBtn: document.getElementById("switchAccountBtn"),
    modal: document.getElementById("modal"),
    modalText: document.getElementById("modalText"),
    modalYes: document.getElementById("modalYes"),
    modalNo: document.getElementById("modalNo"),
  };

  let ageInputDirty = false;

  const DESC_LIMIT = 50;
  let renderedProfileId = null;
  let actionInFlight = false;
  let fullDescription = "";
  let descExpanded = false;

  function setHidden(el, hidden) { el.classList.toggle("hidden", hidden); }

  function renderMedia(media) {
    els.media.innerHTML = "";
    for (const m of media) {
      let node;
      if (m.kind === "video") {
        node = document.createElement("video");
        node.controls = true;
        node.src = m.url;
      } else {
        node = document.createElement("img");
        node.src = m.url;
        node.alt = m.name;
      }
      els.media.appendChild(node);
    }
  }

  function applyDescView() {
    const truncatable = fullDescription.length > DESC_LIMIT;
    if (truncatable && !descExpanded) {
      els.desc.textContent = fullDescription.slice(0, DESC_LIMIT) + "…";
      els.descMore.classList.remove("hidden");
      els.descMore.textContent = "показать полный текст";
    } else {
      els.desc.textContent = fullDescription;
      if (truncatable) {
        els.descMore.classList.remove("hidden");
        els.descMore.textContent = "свернуть";
      } else {
        els.descMore.classList.add("hidden");
      }
    }
  }

  function setDescription(text) {
    if (text === fullDescription) return;
    fullDescription = text;
    descExpanded = false;
    applyDescView();
  }

  function render(state) {
    const hasProfile = !!state.profile;
    const isWarning = !!state.warning;
    const buttonsLocked = actionInFlight || !hasProfile || isWarning || state.busy;

    setHidden(els.priorityAlert, !state.priority_alert);
    setHidden(els.warning, !isWarning);
    setHidden(els.profile, !hasProfile);
    setHidden(els.empty, hasProfile || isWarning);

    if (isWarning) {
      if (state.status_message) {
        els.warningTitle.textContent = "Лимит исчерпан";
        els.warningText.textContent = state.status_message;
      } else {
        els.warningTitle.textContent = "Бот прислал не-анкету";
        els.warningText.textContent = "Откройте Telegram и выполните требуемые действия в чате с @leomatchbot. Кнопки заблокированы до получения новой анкеты.";
      }
    }

    if (state.status_message) {
      els.emptyText.textContent = state.status_message;
    } else if (state.auto_like_mode) {
      els.emptyText.textContent = "автоматически лайкаю анкеты, процесс можно посмотреть в @leomatchbot";
    } else if (state.auto_dislike_mode) {
      els.emptyText.textContent = "наполняю базу, все анкеты получат дизлайк, процесс можно посмотреть в @leomatchbot";
    } else {
      els.emptyText.textContent = "Ожидание анкеты от бота…";
    }

    if (hasProfile) {
      const p = state.profile;
      const isNewProfile = renderedProfileId !== p.id;
      if (isNewProfile || els.media.childElementCount !== p.media.length) {
        renderMedia(p.media);
        renderedProfileId = p.id;
      }
      setDescription(p.description || "(без описания)");
      els.seen.textContent = String(p.seen_count);
    } else {
      renderedProfileId = null;
    }

    els.likeBtn.disabled = buttonsLocked;
    els.dislikeBtn.disabled = buttonsLocked;
    els.letterBtn.disabled = buttonsLocked;

    els.autoDislikeBtn.classList.toggle("active", state.auto_dislike_mode);
    els.autoDislikeBtn.textContent = `Авто-дизлайк: ${state.auto_dislike_mode ? "ON" : "OFF"}`;
    els.autoLikeBtn.classList.toggle("active", state.auto_like_mode);
    els.autoLikeBtn.textContent = `Авто-лайк: ${state.auto_like_mode ? "ON" : "OFF"}`;
    els.modeBtn.classList.toggle("active", state.only_new_mode);
    els.modeBtn.textContent = `Только новые: ${state.only_new_mode ? "ON" : "OFF"}`;
    els.autoCount.textContent = String(state.auto_dislike_count);
    els.likeCount.textContent = String(state.like_count ?? 0);
    els.dislikeCount.textContent = String(state.dislike_count ?? 0);
    els.totalCount.textContent = String(state.total_profiles ?? 0);
    els.activePhone.textContent = state.active_phone || "—";

    const filterActive = state.age_min != null && state.age_max != null;
    els.ageForm.classList.toggle("active", filterActive);
    if (!ageInputDirty) {
      els.ageMin.value = state.age_min == null ? "" : state.age_min;
      els.ageMax.value = state.age_max == null ? "" : state.age_max;
    }
  }

  async function fetchState() {
    try {
      const r = await fetch("/api/state");
      if (!r.ok) return;
      render(await r.json());
    } catch (e) {
      console.error("state poll failed", e);
    }
  }

  async function react(path) {
    if (actionInFlight) return;
    actionInFlight = true;
    els.likeBtn.disabled = true;
    els.dislikeBtn.disabled = true;
    els.letterBtn.disabled = true;
    try {
      const r = await fetch(path, { method: "POST" });
      if (!r.ok) console.warn("reaction failed", r.status);
    } catch (e) {
      console.error("reaction error", e);
    } finally {
      actionInFlight = false;
      await fetchState();
    }
  }

  els.letterBtn.addEventListener("click", async () => {
    if (actionInFlight) return;
    actionInFlight = true;
    els.likeBtn.disabled = true;
    els.dislikeBtn.disabled = true;
    els.letterBtn.disabled = true;
    try {
      await fetch("/api/letter", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: els.letterInput.value }),
      });
      els.letterInput.value = "";
    } catch (e) {
      console.error("letter error", e);
    } finally {
      actionInFlight = false;
      await fetchState();
    }
  });
  els.likeBtn.addEventListener("click", () => react("/api/like"));
  els.dislikeBtn.addEventListener("click", () => react("/api/dislike"));
  let modalOnYes = null;

  function showModal(text, yesClass, onYes) {
    els.modalText.textContent = text;
    els.modalYes.className = `modal-btn ${yesClass}`;
    modalOnYes = onYes;
    setHidden(els.modal, false);
  }

  function closeModal() { setHidden(els.modal, true); modalOnYes = null; }

  els.modalYes.addEventListener("click", async () => {
    const cb = modalOnYes;
    closeModal();
    if (cb) await cb();
  });
  els.modalNo.addEventListener("click", closeModal);
  els.modal.addEventListener("click", (e) => { if (e.target === els.modal) closeModal(); });

  els.autoDislikeBtn.addEventListener("click", async () => {
    if (els.autoDislikeBtn.classList.contains("active")) {
      const r = await fetch("/api/auto-dislike/toggle", { method: "POST" });
      if (r.ok) render(await r.json());
    } else {
      showModal("Включить авто-дизлайк?\n\nВсе анкеты будут автоматически дизлайкнуты.", "modal-btn-dislike", async () => {
        const r = await fetch("/api/auto-dislike/toggle", { method: "POST" });
        if (r.ok) render(await r.json());
      });
    }
  });

  els.autoLikeBtn.addEventListener("click", async () => {
    if (els.autoLikeBtn.classList.contains("active")) {
      const r = await fetch("/api/auto-like/toggle", { method: "POST" });
      if (r.ok) render(await r.json());
    } else {
      showModal("Включить авто-лайк?\n\nВсе новые анкеты будут автоматически лайкнуты.", "modal-btn-like", async () => {
        const r = await fetch("/api/auto-like/toggle", { method: "POST" });
        if (r.ok) render(await r.json());
      });
    }
  });
  els.modeBtn.addEventListener("click", async () => {
    const r = await fetch("/api/only-new/toggle", { method: "POST" });
    if (r.ok) render(await r.json());
  });
  els.switchAccountBtn.addEventListener("click", async () => {
    els.switchAccountBtn.disabled = true;
    try {
      await fetch("/api/switch-account", { method: "POST" });
    } finally {
      setTimeout(() => { els.switchAccountBtn.disabled = false; }, 3000);
      await fetchState();
    }
  });

  els.descMore.addEventListener("click", () => {
    descExpanded = !descExpanded;
    applyDescView();
  });

  function showAgeError(msg) {
    if (!msg) {
      els.ageError.classList.add("hidden");
      els.ageError.textContent = "";
    } else {
      els.ageError.classList.remove("hidden");
      els.ageError.textContent = msg;
    }
  }

  function parseAgeInput(el) {
    const raw = el.value.trim();
    if (raw === "") return { empty: true, value: null };
    if (!/^\d+$/.test(raw)) return { empty: false, value: null };
    return { empty: false, value: parseInt(raw, 10) };
  }

  els.ageMin.addEventListener("input", () => { ageInputDirty = true; });
  els.ageMax.addEventListener("input", () => { ageInputDirty = true; });

  els.ageForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const a = parseAgeInput(els.ageMin);
    const b = parseAgeInput(els.ageMax);
    if (a.empty && b.empty) {
      showAgeError(null);
      await sendAgeFilter(null, null);
      return;
    }
    if (a.empty || b.empty) {
      showAgeError("заполните оба поля");
      return;
    }
    if (a.value === null || b.value === null) {
      showAgeError("только целые числа");
      return;
    }
    if (a.value < 0 || b.value < 0) {
      showAgeError("возраст ≥ 0");
      return;
    }
    if (a.value > b.value) {
      showAgeError("мин должен быть ≤ макс");
      return;
    }
    showAgeError(null);
    await sendAgeFilter(a.value, b.value);
  });

  els.ageReset.addEventListener("click", async () => {
    els.ageMin.value = "";
    els.ageMax.value = "";
    showAgeError(null);
    await sendAgeFilter(null, null);
  });

  async function sendAgeFilter(min, max) {
    const r = await fetch("/api/age-filter", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ min, max }),
    });
    if (r.ok) {
      ageInputDirty = false;
      render(await r.json());
    } else {
      const err = await r.json().catch(() => ({}));
      showAgeError(err.detail || "ошибка");
    }
  }

  fetchState();
  setInterval(fetchState, 1000);
})();
