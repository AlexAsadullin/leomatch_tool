(() => {
  const els = {
    warning: document.getElementById("warning"),
    empty: document.getElementById("empty"),
    profile: document.getElementById("profile"),
    media: document.getElementById("media"),
    desc: document.getElementById("desc"),
    descMore: document.getElementById("descMore"),
    seen: document.getElementById("seen"),
    likeBtn: document.getElementById("likeBtn"),
    dislikeBtn: document.getElementById("dislikeBtn"),
    modeBtn: document.getElementById("modeBtn"),
    autoCount: document.getElementById("autoCount"),
  };

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

    setHidden(els.warning, !isWarning);
    setHidden(els.profile, !hasProfile);
    setHidden(els.empty, hasProfile || isWarning);

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

    els.modeBtn.classList.toggle("active", state.only_new_mode);
    els.modeBtn.textContent = `Только новые: ${state.only_new_mode ? "ON" : "OFF"}`;
    els.autoCount.textContent = String(state.auto_dislike_count);
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

  els.likeBtn.addEventListener("click", () => react("/api/like"));
  els.dislikeBtn.addEventListener("click", () => react("/api/dislike"));
  els.modeBtn.addEventListener("click", async () => {
    const r = await fetch("/api/only-new/toggle", { method: "POST" });
    if (r.ok) render(await r.json());
  });
  els.descMore.addEventListener("click", () => {
    descExpanded = !descExpanded;
    applyDescView();
  });

  fetchState();
  setInterval(fetchState, 1000);
})();
