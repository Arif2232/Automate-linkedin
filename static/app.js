const $ = (id) => document.getElementById(id);
const state = {
  view: "create",
  config: null,
  posts: [],
  selected: null,
  dirty: false,
  dirtyFields: new Set(),
  busy: false,
  creating: false,
  loaded: false,
  clientIdDirty: false,
  aiDirty: { text: false, image: false },
  aiTesting: { text: false, image: false },
  aiIdentity: { text: "", image: "" },
};
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const labels = {
  queued: ["Creating", "working"],
  generating: ["Creating", "working"],
  image_queued: ["Updating image", "working"],
  image_generating: ["Updating image", "working"],
  needs_review: ["Needs review", "review"],
  approved: ["Scheduled", "scheduled"],
  publishing: ["Publishing", "working"],
  published: ["Published", "published"],
  generation_failed: ["Draft failed", "error"],
  publish_failed: ["Needs attention", "error"],
  publish_uncertain: ["Check LinkedIn", "error"],
};

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (init.method !== "GET") {
    init.headers["X-Post-Studio"] = "1";
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Something went wrong. Check the form and try again.",
    );
  return data;
}

let toastTimer;
function toast(message, error = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 5000);
}
function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
function dateParts(value) {
  const date = new Date(value);
  return [
    new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    }).format(date),
    new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
    }).format(date),
  ];
}
function localInput(value) {
  const date = new Date(value);
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function fromInput(value) {
  if (!value) throw new Error("Choose a date and time.");
  const date = new Date(value);
  if (Number.isNaN(date.getTime()))
    throw new Error("Choose a valid date and time.");
  return date.toISOString();
}
function defaultTime() {
  const date = new Date(Date.now() + 60 * 60 * 1000);
  date.setSeconds(0, 0);
  $("first-time").value = localInput(date);
}
function statusInfo(post) {
  if (
    post.status === "needs_review" &&
    new Date(post.scheduled_at) <= new Date()
  )
    return ["Reschedule needed", "error"];
  return labels[post.status] || [post.status, "working"];
}
function statusChip(post) {
  const [label, kind] = statusInfo(post);
  const chip = document.createElement("span");
  chip.className = `status-chip ${kind}`;
  chip.textContent = label;
  return chip;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function canLeave() {
  return !state.dirty || confirm("You have unsaved changes. Discard them?");
}
function navigate(view, force = false) {
  if (!force && state.view === "review" && view !== "review" && !canLeave())
    return false;
  if (view !== "review") {
    state.dirty = false;
    state.dirtyFields.clear();
    state.selected = null;
  }
  state.view = view;
  for (const el of document.querySelectorAll(".view"))
    el.hidden = el.id !== `view-${view}`;
  for (const el of document.querySelectorAll(".nav-link"))
    el.classList.toggle(
      "active",
      el.dataset.nav === (view === "review" ? "queue" : view),
    );
  $("breadcrumb").textContent = {
    create: "Create a post",
    queue: "Review queue",
    review: "Review draft",
    settings: "Settings",
  }[view];
  window.scrollTo({ top: 0, behavior: "auto" });
  return true;
}

function renderConfig() {
  const config = state.config;
  if (!config) return;
  const aiReady =
    config.ai.text.ready &&
    config.ai.text.tested &&
    config.ai.image.ready &&
    config.ai.image.tested;
  $("setup-banner").hidden = aiReady;
  $("create-button").disabled = !aiReady || state.creating;
  $("create-button").title = aiReady
    ? ""
    : "Test your AI connections in Settings first.";
  $("provider-summary").hidden = !aiReady;
  $("provider-summary-text").textContent =
    `Writing: ${config.ai.text.name} · Images: ${config.ai.image.provider === "none" ? "None" : config.ai.image.name}`;
  $("connection-dot").classList.toggle("connected", config.linkedin_connected);
  $("connection-label").textContent = config.linkedin_connected
    ? config.linkedin_name || "LinkedIn connected"
    : "Connect LinkedIn";
  for (const kind of ["text", "image"]) {
    const select = $(`${kind}-provider`);
    if (!select.options.length) {
      for (const spec of config.ai.providers.filter((item) => item[kind])) {
        const option = document.createElement("option");
        option.value = spec.id;
        option.textContent = spec.name;
        select.append(option);
      }
    }
    const active = config.ai[kind];
    const identity = JSON.stringify([
      active.provider,
      active.model,
      active.base_url,
      active.ready,
      active.tested,
    ]);
    if (
      state.aiIdentity[kind] &&
      state.aiIdentity[kind] !== identity &&
      !state.aiTesting[kind]
    ) {
      $(`${kind}-test-result`).textContent = "";
      $(`${kind}-test-result`).className = "test-result";
    }
    state.aiIdentity[kind] = identity;
    const badge = $(`${kind}-ai-badge`);
    badge.textContent = state.aiDirty[kind]
      ? "Unsaved"
      : active.provider === "none"
        ? "Text only"
        : !active.ready
          ? "Not configured"
          : active.tested
            ? "Ready"
            : "Needs test";
    badge.classList.toggle(
      "connected",
      !state.aiDirty[kind] &&
        active.ready &&
        active.tested &&
        active.provider !== "none",
    );
    if (!state.aiDirty[kind]) {
      select.value = active.selection;
      renderAIFields(kind);
    }
  }
  $("linkedin-badge").textContent = config.linkedin_connected
    ? "Connected"
    : "Not connected";
  $("linkedin-badge").classList.toggle("connected", config.linkedin_connected);
  $("linkedin-disconnect").hidden = !config.linkedin_connected;
  $("linkedin-connect").textContent = config.linkedin_connected
    ? "Reconnect LinkedIn ↗"
    : "Connect LinkedIn ↗";
  $("linkedin-connect").setAttribute(
    "aria-disabled",
    String(!config.linkedin_client_id || !config.linkedin_secret_configured),
  );
  if (!state.clientIdDirty && document.activeElement !== $("linkedin-id"))
    $("linkedin-id").value = config.linkedin_client_id || "";
  $("linkedin-secret").placeholder = config.linkedin_secret_configured
    ? "Saved — enter a new secret to replace"
    : "Your LinkedIn client secret";
  $("linkedin-detail").textContent = config.linkedin_connected
    ? `Connected as ${config.linkedin_name}. Access expires ${formatDate(config.linkedin_expires_at)}.`
    : "Your app credentials and access token are stored unencrypted on this computer, not sent to the browser.";
  renderNotifications();
}
function renderAIFields(kind) {
  const ai = state.config.ai;
  const choice = $(`${kind}-provider`).value;
  const spec = ai.providers.find((provider) => provider.id === choice);
  if (!spec) return;
  const simple = choice === "same" || choice === "none";
  $(`${kind}-model-wrap`).hidden = simple;
  $(`${kind}-key-wrap`).hidden = simple;
  $(`${kind}-url-wrap`).hidden = simple || !spec.needs_url;
  $(`${kind}-model`).value = ai.models[kind][choice] || "";
  $(`${kind}-base-url`).value = ai.urls[kind][choice] || "";
  const hasKey = Boolean(ai.keys[kind][choice]);
  $(`${kind}-api-key`).placeholder = hasKey
    ? "Saved — enter a new key to replace"
    : spec.key_optional
      ? "Optional for local Ollama"
      : "Your provider API key";
  $(`${kind}-remove-key`).hidden = simple || !hasKey;
  if (choice === "same") {
    const writer = ai.providers.find(
      (provider) => provider.id === ai.text.provider,
    );
    $(`${kind}-ai-detail`).textContent =
      !writer?.image || writer.needs_url
        ? "The selected writing provider has no automatic image setup. Posts can be text-only, or choose an image provider here."
        : `Uses ${writer.name} with image model ${ai.models.image[writer.id]}. To change the image model, select the provider above.`;
  } else if (choice === "none") {
    $(`${kind}-ai-detail`).textContent =
      "You will explicitly approve each text-only post before it is scheduled.";
  } else {
    $(`${kind}-ai-detail`).textContent = spec.needs_url
      ? "Use the provider’s API base URL and model ID. A key is optional only for local custom endpoints."
      : spec.key_optional
        ? "Start Ollama and download the selected model on this computer first."
        : "Use a model available to your account. Keys are saved separately for each provider.";
  }
  const writer = ai.providers.find(
    (provider) => provider.id === ai.text.provider,
  );
  const noImage =
    choice === "none" ||
    (choice === "same" && (!writer?.image || writer.needs_url));
  $(`${kind}-test-button`).textContent =
    kind === "image" && noImage ? "Save text-only choice" : "Test & save";
}
function renderNotifications() {
  if (!("Notification" in window)) {
    $("notifications-button").disabled = true;
    $("notifications-detail").textContent =
      "Notifications are not available in this browser.";
    return;
  }
  const permission = Notification.permission;
  $("notifications-button").disabled = permission !== "default";
  $("notifications-button").textContent =
    permission === "granted"
      ? "Notifications enabled"
      : permission === "denied"
        ? "Notifications blocked"
        : "Enable notifications";
  $("notifications-detail").textContent =
    permission === "denied"
      ? "Change this in your browser’s site settings."
      : "";
}
function showNotifications(oldPosts, newPosts) {
  if (!("Notification" in window) || Notification.permission !== "granted")
    return;
  const old = new Map(oldPosts.map((post) => [post.id, post.status]));
  for (const post of newPosts) {
    if (
      post.status === "needs_review" &&
      old.get(post.id) !== "needs_review" &&
      old.get(post.id) !== "approved"
    ) {
      try {
        new Notification("Your draft is ready to review", { body: post.topic });
      } catch (_) {
        /* Notifications may be unavailable in private browsing. */
      }
    }
  }
}
function renderQueue() {
  const posts = state.posts;
  const reviewCount = posts.filter((post) =>
    [
      "needs_review",
      "generation_failed",
      "publish_failed",
      "publish_uncertain",
    ].includes(post.status),
  ).length;
  $("nav-count").textContent = String(reviewCount);
  $("nav-count").hidden = !reviewCount;
  $("count-review").textContent = String(reviewCount);
  $("count-scheduled").textContent = String(
    posts.filter((post) => post.status === "approved").length,
  );
  $("count-published").textContent = String(
    posts.filter((post) => post.status === "published").length,
  );
  const list = $("queue-list");
  list.replaceChildren();
  if (!posts.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    const mark = document.createElement("div");
    mark.className = "empty-icon";
    mark.append(icon("stack"));
    const title = document.createElement("h3");
    title.textContent = "Your queue is a blank canvas.";
    const description = document.createElement("p");
    description.textContent =
      "When you create a post, it will be waiting here for you.";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button-primary";
    button.textContent = "Create your first post";
    button.addEventListener("click", () => navigate("create"));
    empty.append(mark, title, description, button);
    list.append(empty);
  } else {
    for (const post of posts) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "queue-item";
      button.setAttribute(
        "aria-label",
        `${post.topic}, ${statusInfo(post)[0]}. Open post.`,
      );
      const mark = document.createElement("div");
      mark.className = "queue-item-icon";
      mark.append(icon(post.status === "published" ? "check" : "pen"));
      const body = document.createElement("div");
      body.className = "queue-item-body";
      const title = document.createElement("div");
      title.className = "queue-item-title";
      title.textContent = post.topic;
      const preview = document.createElement("div");
      preview.className = "queue-item-preview";
      preview.textContent =
        post.content || post.error || "Getting your draft ready…";
      body.append(title, preview);
      const side = document.createElement("div");
      side.className = "queue-item-side";
      const time = document.createElement("div");
      time.className = "queue-time";
      const [day, hour] = dateParts(post.scheduled_at);
      const dayEl = document.createElement("strong");
      dayEl.textContent = day;
      const hourEl = document.createElement("span");
      hourEl.textContent = hour;
      time.append(dayEl, hourEl);
      const arrow = icon("arrow");
      arrow.classList.add("queue-arrow");
      const openLabel = document.createElement("span");
      openLabel.className = "queue-open";
      openLabel.textContent = "Open";
      side.append(statusChip(post), time, openLabel, arrow);
      button.append(mark, body, side);
      button.addEventListener("click", () => openPost(post.id));
      list.append(button);
    }
  }
  const upcoming = $("upcoming-list");
  upcoming.replaceChildren();
  const next = posts.filter((post) => post.status !== "published").slice(0, 3);
  if (!next.length) {
    const empty = document.createElement("p");
    empty.className = "empty-small";
    empty.textContent =
      "Nothing in the queue yet. Your next idea can start right here.";
    upcoming.append(empty);
  }
  next.forEach((post, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mini-post";
    const number = document.createElement("span");
    number.className = "mini-number";
    number.textContent = String(index + 1).padStart(2, "0");
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = post.topic;
    const sub = document.createElement("small");
    sub.textContent = statusInfo(post)[0];
    text.append(title, sub);
    button.append(number, text);
    button.addEventListener("click", () => openPost(post.id));
    upcoming.append(button);
  });
}

async function refresh() {
  const [config, posts] = await Promise.all([
    api("/api/config"),
    api("/api/posts"),
  ]);
  if (state.loaded) showNotifications(state.posts, posts);
  const changed = JSON.stringify(posts) !== JSON.stringify(state.posts);
  state.config = config;
  state.posts = posts;
  state.loaded = true;
  renderConfig();
  if (changed || !$("queue-list").children.length) renderQueue();
  if (
    state.view === "review" &&
    state.selected &&
    !state.dirty &&
    !state.busy
  ) {
    const current = posts.find((post) => post.id === state.selected.id);
    if (!current) {
      navigate("queue", true);
      toast("This draft is no longer available.");
    } else if (
      current.revision !== state.selected.revision ||
      current.series_active !== state.selected.series_active
    )
      await loadPost(current.id);
  }
}
async function openPost(id) {
  if (state.view === "review" && state.selected?.id !== id && !canLeave())
    return;
  try {
    state.dirty = false;
    state.dirtyFields.clear();
    await loadPost(id);
    navigate("review", true);
  } catch (error) {
    toast(error.message, true);
  }
}
async function loadPost(id) {
  const post = await api(`/api/posts/${id}`);
  if (state.view === "review" && state.selected && state.selected.id !== id)
    return;
  state.selected = post;
  state.dirty = false;
  state.dirtyFields.clear();
  renderReview();
}
function renderReview() {
  const post = state.selected;
  if (!post) return;
  const editable = post.status === "needs_review";
  const [label, kind] = statusInfo(post);
  $("review-status").className = `status-chip ${kind}`;
  $("review-status").textContent = label;
  $("review-subtitle").textContent = post.topic;
  $("preview-name").textContent =
    state.config?.linkedin_name || "Your LinkedIn post";
  $("review-topic").value = post.topic;
  $("review-topic").disabled = !editable;
  $("review-topic-hint").textContent =
    post.repeat_unit === "once"
      ? "Changing the topic won't rewrite the post or image."
      : "Changing the topic only affects this draft, not its content or future posts.";
  $("post-content").value = post.content;
  $("post-content").disabled = !editable;
  $("review-time").value = localInput(post.scheduled_at);
  $("review-time").disabled = !editable;
  const repeating = post.repeat_unit !== "once" && post.series_active;
  $("review-repeat-unit").value = repeating ? post.repeat_unit : "once";
  $("review-repeat-count").value = post.repeat_every;
  $("review-repeat-unit").disabled = !editable;
  $("review-repeat-count").disabled = !editable;
  updateReviewRepeat();
  $("character-count").textContent = `${post.content.length} / 3000`;
  $("post-image").hidden = !post.image_url;
  $("image-placeholder").hidden = Boolean(post.image_url);
  if (post.image_url) {
    $("post-image").src = post.image_url;
    $("post-image").alt = `AI-generated image for ${post.topic}`;
  } else $("post-image").removeAttribute("src");
  $("image-edit-wrap").hidden = !editable;
  $("text-only-wrap").hidden = !editable || Boolean(post.image_url);
  $("publish-without-image").checked = false;
  $("publish-without-image").disabled = !editable;
  $("image-instruction").value = "";
  $("image-instruction").disabled = !editable;
  $("image-button").textContent = post.image_url
    ? "Update image"
    : "Generate image";
  $("image-hint").textContent =
    state.config?.ai.image.provider === "none"
      ? "Choose an image provider in Settings to generate an image."
      : post.image_url
        ? "Describe a change, then review the new image before approving."
        : "Add visual direction or leave this blank to try again.";
  $("image-placeholder").querySelector("span").textContent =
    post.status === "approved" && !post.image_url
      ? "This post will publish without an image"
      : "Your image will appear here";
  $("edit-actions").hidden = !editable;
  $("unapprove-button").hidden = post.status !== "approved";
  $("retry-button").hidden = ![
    "generation_failed",
    "publish_failed",
    "publish_uncertain",
  ].includes(post.status);
  $("retry-button").textContent =
    post.status === "generation_failed" ? "Retry draft" : "Return to review";
  $("stop-repeat-button").hidden = !repeating;
  $("delete-button").hidden = [
    "generating",
    "image_generating",
    "publishing",
    "published",
  ].includes(post.status);
  const urn = post.linkedin_id || "";
  const safeUrn = /^urn:li:(share|ugcPost):[A-Za-z0-9_-]+$/.test(urn);
  $("published-link").hidden = post.status !== "published" || !safeUrn;
  if (safeUrn)
    $("published-link").href = `https://www.linkedin.com/feed/update/${urn}/`;
  $("chat-input").disabled = !editable;
  $("chat-send").disabled = !editable;
  $("chat-input").placeholder = editable
    ? "Ask a question or request a rewrite…"
    : "Available when this post is in review";
  let alert = "";
  if (["queued", "generating"].includes(post.status))
    alert =
      state.config?.ai.image.provider === "none"
        ? "Creating your draft. You can leave this page open or return later."
        : "Creating your draft and image. This can take a minute or two; you can leave this page open or return later.";
  else if (["image_queued", "image_generating"].includes(post.status))
    alert = "Updating your image. Your current draft is safe while this runs.";
  else if (post.status === "approved")
    alert = `Approved for ${formatDate(post.scheduled_at)}. Keep the app running and your computer awake so it can publish.`;
  else if (post.status === "publishing")
    alert = "Sending your approved post to LinkedIn now.";
  else if (post.status === "published")
    alert = post.error || "Published on LinkedIn.";
  else if (post.error) alert = post.error;
  else if (post.status === "needs_review" && post.image_error)
    alert = `Image: ${post.image_error} You can try again below.`;
  else if (
    post.status === "needs_review" &&
    new Date(post.scheduled_at) <= new Date()
  )
    alert =
      "The scheduled time has passed. Choose a new time before approving.";
  $("review-alert").textContent = alert;
  $("review-alert").hidden = !alert;
  const messages = $("chat-messages");
  messages.replaceChildren();
  if (!post.messages.length) {
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble assistant";
    bubble.textContent =
      "Hi! Ask me anything about this draft, or tell me how you’d like to change it. For example: ‘Make the opening more conversational.’";
    messages.append(bubble);
  }
  for (const message of post.messages) {
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${message.role === "user" ? "user" : "assistant"}`;
    const text = document.createElement("span");
    text.textContent = message.content;
    bubble.append(text);
    if (message.role === "assistant" && editable) {
      const use = document.createElement("button");
      use.type = "button";
      use.className = "use-reply";
      use.textContent = "Use this reply as my post";
      use.addEventListener("click", () => {
        $("post-content").value = message.content;
        state.dirty = true;
        state.dirtyFields.add("post-content");
        updateCount();
        toast("Suggestion added. Save it or keep editing.");
        $("post-content").focus();
      });
      bubble.append(use);
    }
    messages.append(bubble);
  }
  messages.scrollTop = messages.scrollHeight;
}
function updateCount() {
  $("character-count").textContent = `${$("post-content").value.length} / 3000`;
}
function updateReviewRepeat() {
  const unit = $("review-repeat-unit").value;
  const count = $("review-repeat-count").value;
  $("review-repeat-count-wrap").hidden = unit === "once";
  $("review-repeat-count-unit").textContent =
    unit === "once" ? "" : count === "1" ? unit.slice(0, -1) : unit;
  const scheduleZone = state.dirtyFields.has("review-time")
    ? zone
    : state.selected.timezone;
  $("review-schedule-detail").textContent =
    unit === "once"
      ? `Time shown in ${zone}. This post will not repeat.`
      : `Time shown in ${zone}. After publication, a new draft is prepared every ${count || "…"} ${count === "1" ? unit.slice(0, -1) : unit} for your approval.${unit === "hours" ? "" : ` Repeats use ${scheduleZone}.`}`;
}
function toggleBusy(value) {
  state.busy = value;
  for (const id of [
    "save-button",
    "approve-button",
    "unapprove-button",
    "retry-button",
    "stop-repeat-button",
    "delete-button",
    "image-button",
    "chat-send",
  ])
    $(id).disabled = value;
  if (state.selected) {
    const locked = value || state.selected.status !== "needs_review";
    for (const id of [
      "review-topic",
      "post-content",
      "review-time",
      "review-repeat-unit",
      "review-repeat-count",
      "image-instruction",
      "chat-input",
      "publish-without-image",
    ])
      $(id).disabled = locked;
  }
  if (!value && state.selected)
    $("chat-send").disabled = state.selected.status !== "needs_review";
}
async function runAction(action) {
  if (state.busy) return;
  toggleBusy(true);
  try {
    await action();
  } catch (error) {
    toast(error.message, true);
  } finally {
    toggleBusy(false);
  }
}
async function saveDraft() {
  if (!state.dirty) return;
  const post = state.selected;
  const changes = { revision: post.revision };
  if (state.dirtyFields.has("review-topic"))
    changes.topic = $("review-topic").value;
  if (state.dirtyFields.has("post-content"))
    changes.content = $("post-content").value;
  if (state.dirtyFields.has("review-time")) {
    changes.scheduled_at = fromInput($("review-time").value);
    changes.timezone = zone;
  }
  if (state.dirtyFields.has("review-repeat")) {
    const unit = $("review-repeat-unit").value;
    const every = unit === "once" ? 1 : Number($("review-repeat-count").value);
    if (!Number.isInteger(every) || every < 1 || every > 365)
      throw new Error("Enter a whole repeat interval between 1 and 365.");
    changes.repeat_unit = unit;
    changes.repeat_every = every;
  }
  const updated = await api(`/api/posts/${post.id}`, {
    method: "PATCH",
    body: changes,
  });
  state.selected = { ...post, ...updated };
  state.dirty = false;
  state.dirtyFields.clear();
}

for (const element of document.querySelectorAll("[data-nav]"))
  element.addEventListener("click", (event) => {
    event.preventDefault();
    navigate(element.dataset.nav);
  });
$("timezone-label").textContent = `Your time zone: ${zone}`;
defaultTime();
$("repeat-unit").addEventListener("change", () => {
  $("repeat-count-wrap").hidden = $("repeat-unit").value === "once";
  $("repeat-count-unit").textContent = $("repeat-unit").value;
});
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("create-button");
  state.creating = true;
  button.disabled = true;
  try {
    const post = await api("/api/posts", {
      method: "POST",
      body: {
        topic: $("topic").value.trim(),
        context: $("context").value.trim(),
        scheduled_at: fromInput($("first-time").value),
        timezone: zone,
        repeat_unit: $("repeat-unit").value,
        repeat_every: Number($("repeat-count").value),
      },
    });
    $("topic").value = "";
    $("context").value = "";
    defaultTime();
    await refresh();
    await openPost(post.id);
    toast("Your draft is being created.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.creating = false;
    const ai = state.config?.ai;
    button.disabled =
      !ai ||
      !ai.text.ready ||
      !ai.text.tested ||
      !ai.image.ready ||
      !ai.image.tested;
  }
});
for (const id of ["review-topic", "post-content", "review-time"])
  $(id).addEventListener("input", () => {
    state.dirty = true;
    state.dirtyFields.add(id);
    if (id === "review-topic") $("review-subtitle").textContent = $(id).value;
    if (id === "review-time") updateReviewRepeat();
    updateCount();
  });
for (const id of ["review-repeat-unit", "review-repeat-count"])
  $(id).addEventListener(
    id === "review-repeat-unit" ? "change" : "input",
    () => {
      state.dirty = true;
      state.dirtyFields.add("review-repeat");
      updateReviewRepeat();
    },
  );
$("save-button").addEventListener("click", () =>
  runAction(async () => {
    await saveDraft();
    await loadPost(state.selected.id);
    toast("Draft saved.");
  }),
);
$("approve-button").addEventListener("click", () =>
  runAction(async () => {
    await saveDraft();
    const post = state.selected;
    if (!state.config?.linkedin_connected)
      throw new Error(
        "Connect your LinkedIn account in Settings before approving.",
      );
    const withoutImage = !post.image_url && $("publish-without-image").checked;
    if (!post.image_url && !withoutImage)
      throw new Error(
        "Generate an image or check the option to approve without one.",
      );
    if (new Date(post.scheduled_at) <= new Date())
      throw new Error("The scheduled time has passed. Choose a future time.");
    const expiryWarning =
      new Date(state.config.linkedin_expires_at) <= new Date(post.scheduled_at)
        ? `\n\nYour LinkedIn access expires ${formatDate(state.config.linkedin_expires_at)}. Reconnect before then so this post can publish.`
        : "";
    if (
      !confirm(
        `Approve this ${withoutImage ? "text-only " : ""}post for ${formatDate(post.scheduled_at)}? It will be public on your connected LinkedIn profile. If the app is offline at that time, it will publish when the app restarts.${expiryWarning}`,
      )
    )
      return;
    await api(`/api/posts/${post.id}/approve`, {
      method: "POST",
      body: { revision: post.revision, publish_without_image: withoutImage },
    });
    await loadPost(post.id);
    await refresh();
    toast("Approved and scheduled.");
  }),
);
$("unapprove-button").addEventListener("click", () =>
  runAction(async () => {
    const id = state.selected.id;
    await api(`/api/posts/${id}/unapprove`, { method: "POST" });
    await loadPost(id);
    await refresh();
    toast("Moved back to review.");
  }),
);
$("retry-button").addEventListener("click", () =>
  runAction(async () => {
    const post = state.selected;
    if (
      ["publish_failed", "publish_uncertain"].includes(post.status) &&
      !confirm(
        "Check your LinkedIn profile first. This post may already have been published. Return it to review?",
      )
    )
      return;
    await api(`/api/posts/${post.id}/retry`, { method: "POST" });
    await loadPost(post.id);
    await refresh();
    toast(
      post.status === "generation_failed"
        ? "Trying the draft again."
        : "Returned to review. Choose a new time before approving.",
    );
  }),
);
$("stop-repeat-button").addEventListener("click", () =>
  runAction(async () => {
    if (
      !confirm(
        "Stop creating future drafts for this schedule? The current draft stays in your queue.",
      )
    )
      return;
    await saveDraft();
    const id = state.selected.id;
    await api(`/api/posts/${id}/stop-repeat`, { method: "POST" });
    await loadPost(id);
    await refresh();
    toast("Repeating schedule stopped.");
  }),
);
$("delete-button").addEventListener("click", () =>
  runAction(async () => {
    if (
      !confirm(
        "Delete this draft? Any repeating schedule for it will also stop.",
      )
    )
      return;
    const id = state.selected.id;
    await api(`/api/posts/${id}`, { method: "DELETE" });
    state.dirty = false;
    navigate("queue", true);
    await refresh();
    toast("Draft deleted.");
  }),
);
$("image-button").addEventListener("click", () =>
  runAction(async () => {
    await saveDraft();
    const post = state.selected;
    await api(`/api/posts/${post.id}/image`, {
      method: "POST",
      body: {
        revision: post.revision,
        instruction: $("image-instruction").value,
      },
    });
    await loadPost(post.id);
    await refresh();
    toast("Updating your image.");
  }),
);
$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runAction(async () => {
    const input = $("chat-input");
    const message = input.value.trim();
    if (!message) throw new Error("Write a question or an editing request.");
    await saveDraft();
    const post = state.selected;
    input.disabled = true;
    input.placeholder = "Thinking…";
    try {
      await api(`/api/posts/${post.id}/chat`, {
        method: "POST",
        body: { revision: post.revision, message },
      });
      input.value = "";
      await loadPost(post.id);
      await refresh();
    } finally {
      input.disabled = state.selected?.status !== "needs_review";
      input.placeholder = input.disabled
        ? "Available when this post is in review"
        : "Ask a question or request a rewrite…";
    }
  });
});
for (const kind of ["text", "image"]) {
  const result = $(`${kind}-test-result`);
  function markChanged() {
    state.aiDirty[kind] = true;
    $(`${kind}-ai-badge`).textContent = "Unsaved";
    $(`${kind}-ai-badge`).classList.remove("connected");
    result.className = "test-result";
    result.textContent = "Settings changed. Test them before continuing.";
  }
  $(`${kind}-provider`).addEventListener("change", () => {
    markChanged();
    $(`${kind}-api-key`).value = "";
    renderAIFields(kind);
  });
  for (const id of [`${kind}-model`, `${kind}-base-url`, `${kind}-api-key`]) {
    $(id).addEventListener("input", markChanged);
  }
  $(`${kind}-ai-form`).addEventListener("submit", async (event) => {
    event.preventDefault();
    state.aiTesting[kind] = true;
    const form = event.currentTarget;
    const provider = $(`${kind}-provider`).value;
    const simple = provider === "same" || provider === "none";
    const controls = Array.from(form.querySelectorAll("input, select, button"));
    const previous = controls.map((control) => control.disabled);
    controls.forEach((control) => {
      control.disabled = true;
    });
    let saved = false;
    result.className = "test-result";
    result.textContent = "Saving settings…";
    try {
      await api("/api/ai-config", {
        method: "POST",
        body: {
          kind,
          provider,
          model: simple ? "" : $(`${kind}-model`).value.trim(),
          base_url: simple ? "" : $(`${kind}-base-url`).value.trim(),
          api_key: simple ? null : $(`${kind}-api-key`).value.trim() || null,
        },
      });
      saved = true;
      $(`${kind}-api-key`).value = "";
      state.aiDirty[kind] = false;
      await refresh();
      result.textContent =
        kind === "image"
          ? "Testing the image model; this can take a few minutes…"
          : "Testing the writing connection…";
      await api(`/api/ai-config/${kind}/test`, { method: "POST" });
      await refresh();
      result.className = "test-result success";
      result.textContent =
        state.config.ai[kind].provider === "none"
          ? "Text-only choice saved. No image test is needed."
          : kind === "image"
            ? "Image connection passed. The sample image was deleted."
            : "Writing connection passed. Settings saved.";
      toast(result.textContent);
    } catch (error) {
      if (saved) await refresh().catch(() => {});
      result.className = "test-result error";
      result.textContent = `Test failed: ${error.message}`;
      toast(error.message, true);
    } finally {
      controls.forEach((control, index) => {
        control.disabled = previous[index];
      });
      state.aiTesting[kind] = false;
    }
  });
  $(`${kind}-remove-key`).addEventListener("click", async () => {
    const provider = $(`${kind}-provider`).value;
    const name =
      state.config.ai.providers.find((item) => item.id === provider)?.name ||
      provider;
    if (
      !confirm(
        `Remove the ${name} API key? Writing or image settings using it may stop working.`,
      )
    )
      return;
    try {
      await api(`/api/ai-config/${kind}/${provider}/key`, { method: "DELETE" });
      state.aiDirty[kind] = false;
      $(`${kind}-api-key`).value = "";
      result.className = "test-result";
      result.textContent = "";
      await refresh();
      toast(`${name} key removed.`);
    } catch (error) {
      toast(error.message, true);
    }
  });
}
$("linkedin-id").addEventListener("input", () => {
  state.clientIdDirty = true;
});
$("linkedin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const clientId = $("linkedin-id").value.trim();
  const secret = $("linkedin-secret").value.trim();
  if (!clientId && !secret) {
    toast("Enter your LinkedIn app details.", true);
    return;
  }
  event.submitter.disabled = true;
  try {
    await api("/api/config", {
      method: "POST",
      body: { linkedin_client_id: clientId, linkedin_client_secret: secret },
    });
    $("linkedin-secret").value = "";
    state.clientIdDirty = false;
    await refresh();
    toast("LinkedIn app details saved. You can connect your account now.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    event.submitter.disabled = false;
  }
});
$("linkedin-connect").addEventListener("click", (event) => {
  if ($("linkedin-connect").getAttribute("aria-disabled") === "true") {
    event.preventDefault();
    toast("Save your LinkedIn client ID and secret first.", true);
  }
});
$("linkedin-disconnect").addEventListener("click", async () => {
  if (
    !confirm("Disconnect LinkedIn? Scheduled posts will move back to review.")
  )
    return;
  try {
    await api("/api/linkedin/disconnect", { method: "POST" });
    await refresh();
    toast("LinkedIn disconnected.");
  } catch (error) {
    toast(error.message, true);
  }
});
$("copy-redirect").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(
      "http://localhost:8000/auth/linkedin/callback",
    );
    toast("Redirect URL copied.");
  } catch (_) {
    toast("Copy the redirect URL shown above.");
  }
});
$("notifications-button").addEventListener("click", async () => {
  await Notification.requestPermission();
  renderNotifications();
});
window.addEventListener("beforeunload", (event) => {
  if (state.dirty) event.preventDefault();
});
const params = new URLSearchParams(window.location.search);
if (params.has("linkedin_connected")) {
  navigate("settings", true);
  toast("LinkedIn connected.");
  history.replaceState(null, "", "/");
} else if (params.has("linkedin_error")) {
  navigate("settings", true);
  toast(
    "LinkedIn connection failed. Check the app credentials, redirect URL, and enabled products, then try again.",
    true,
  );
  history.replaceState(null, "", "/");
}
refresh().catch((error) => toast(error.message, true));
setInterval(() => refresh().catch(() => {}), 4000);
