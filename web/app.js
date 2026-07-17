const STORAGE_KEY = "local-build-agent.tasks.v1";

const elements = {
  taskSidebar: document.querySelector("#taskSidebar"),
  taskList: document.querySelector("#taskList"),
  newTask: document.querySelector("#newTaskButton"),
  sidebarToggle: document.querySelector("#sidebarToggle"),
  activityToggle: document.querySelector("#activityToggle"),
  activityPanel: document.querySelector("#activityPanel"),
  closeActivity: document.querySelector("#closeActivity"),
  scrim: document.querySelector("#scrim"),
  welcome: document.querySelector("#welcome"),
  messageList: document.querySelector("#messageList"),
  conversation: document.querySelector("#conversation"),
  form: document.querySelector("#composerForm"),
  input: document.querySelector("#promptInput"),
  send: document.querySelector("#sendButton"),
  thinking: document.querySelector("#thinkingRow"),
  thinkingLabel: document.querySelector("#thinkingLabel"),
  activityList: document.querySelector("#activityList"),
  activityEmpty: document.querySelector("#activityEmpty"),
  workspace: document.querySelector("#workspacePath"),
  sidebarModel: document.querySelector("#sidebarModel"),
  sidebarStatus: document.querySelector("#sidebarStatus"),
  sidebarStatusDot: document.querySelector("#sidebarStatusDot"),
  statusModel: document.querySelector("#statusModel"),
  statusContext: document.querySelector("#statusContext"),
  statusDisk: document.querySelector("#statusDisk"),
};

let state = loadState();
let running = false;

function makeId() {
  return (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/[^a-zA-Z0-9-]/g, "");
}

function newTask() {
  return { id: makeId(), title: "新任务", createdAt: Date.now(), messages: [], events: [] };
}

function loadState() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (parsed?.tasks?.length) return parsed;
  } catch (_) {}
  const task = newTask();
  return { activeId: task.id, tasks: [task] };
}

function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function activeTask() {
  let task = state.tasks.find(item => item.id === state.activeId);
  if (!task) {
    task = newTask();
    state.tasks.unshift(task);
    state.activeId = task.id;
  }
  return task;
}

function shortTime(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(timestamp);
}

function renderTasks() {
  elements.taskList.innerHTML = "";
  state.tasks.forEach(task => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `task-item${task.id === state.activeId ? " active" : ""}`;
    item.dataset.id = task.id;
    item.innerHTML = `<strong>${escapeHtml(task.title)}</strong><small>${shortTime(task.createdAt)}</small><span class="task-delete" data-delete="${task.id}" aria-label="删除任务">×</span>`;
    elements.taskList.append(item);
  });
}

function renderConversation() {
  const task = activeTask();
  elements.welcome.classList.toggle("hidden", task.messages.length > 0);
  elements.messageList.innerHTML = task.messages.map(message => {
    const label = message.role === "assistant" ? '<div class="message-label">Local Build Agent</div>' : "";
    return `<article class="message ${message.role}">${label}<div class="message-body">${renderMarkdown(message.content)}</div></article>`;
  }).join("");
  scrollToBottom(false);
}

function renderActivity() {
  const events = activeTask().events || [];
  elements.activityEmpty.classList.toggle("hidden", events.length > 0);
  elements.activityList.innerHTML = events.slice().reverse().map(event => {
    const status = event.status || "done";
    const icon = status === "error" ? "!" : status === "running" ? "◌" : "✓";
    return `<article class="activity-event ${status}">
      <div class="event-title"><span class="event-icon">${icon}</span>${escapeHtml(event.title)}<span class="event-time">${shortTime(event.time)}</span></div>
      ${event.detail ? `<p>${escapeHtml(event.detail)}</p>` : ""}
    </article>`;
  }).join("");
}

function renderAll() {
  renderTasks();
  renderConversation();
  renderActivity();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[character]));
}

function renderMarkdown(text) {
  const codeBlocks = [];
  let source = String(text ?? "").replace(/```([^\n]*)\n?([\s\S]*?)```/g, (_, language, code) => {
    const index = codeBlocks.length;
    codeBlocks.push(`<pre><code data-language="${escapeHtml(language.trim())}">${escapeHtml(code.trim())}</code></pre>`);
    return `\u0000CODE${index}\u0000`;
  });
  source = escapeHtml(source)
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^[-*] (.+)$/gm, "<li>$1</li>");
  source = source.replace(/(?:<li>.*<\/li>\n?)+/g, list => `<ul>${list}</ul>`);
  source = source.split(/\n{2,}/).map(block => {
    if (/^<(h\d|pre|ul)/.test(block)) return block;
    return `<p>${block.replace(/\n/g, "<br>")}</p>`;
  }).join("");
  return source.replace(/\u0000CODE(\d+)\u0000/g, (_, index) => codeBlocks[Number(index)]);
}

function addMessage(role, content) {
  const task = activeTask();
  task.messages.push({ role, content, time: Date.now() });
  if (role === "user" && task.title === "新任务") {
    task.title = content.replace(/\s+/g, " ").slice(0, 30) || "新任务";
  }
  saveState();
  renderTasks();
  renderConversation();
}

function addEvent(event) {
  const task = activeTask();
  task.events ||= [];
  task.events.push({ ...event, time: Date.now() });
  task.events = task.events.slice(-80);
  saveState();
  renderActivity();
}

function updateLatestEvent(name, patch) {
  const events = activeTask().events || [];
  const target = [...events].reverse().find(event => event.key === name && event.status === "running");
  if (target) Object.assign(target, patch);
  else addEvent({ key: name, title: name, ...patch });
  saveState();
  renderActivity();
}

async function sendMessage(text) {
  if (running || !text.trim()) return;
  const task = activeTask();
  running = true;
  addMessage("user", text.trim());
  elements.input.value = "";
  resizeInput();
  setRunning(true, "Agent 正在思考");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: task.id, message: text.trim() }),
    });
    if (!response.ok || !response.body) throw new Error(`请求失败 (${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) if (line.trim()) handleStreamEvent(JSON.parse(line));
    }
    if (buffer.trim()) handleStreamEvent(JSON.parse(buffer));
  } catch (error) {
    addMessage("assistant", `连接本地 Agent 时出错：${error.message}`);
    addEvent({ title: "请求失败", detail: error.message, status: "error" });
  } finally {
    running = false;
    setRunning(false);
    fetchStatus();
  }
}

function handleStreamEvent(event) {
  if (event.type === "step") {
    elements.thinkingLabel.textContent = `Agent 正在处理 · 第 ${event.step} 步`;
  } else if (event.type === "assistant" && event.content) {
    addMessage("assistant", event.content);
  } else if (event.type === "tool_start") {
    const detail = Object.keys(event.arguments || {}).length ? JSON.stringify(event.arguments, null, 2) : "等待结果…";
    addEvent({ key: event.name, title: event.name, detail, status: "running" });
    elements.thinkingLabel.textContent = `正在运行 ${event.name}`;
  } else if (event.type === "tool_result") {
    updateLatestEvent(event.name, { detail: event.output.slice(0, 1800), status: event.output.startsWith("ERROR:") ? "error" : "done" });
  } else if (event.type === "error") {
    addMessage("assistant", `任务未完成：${event.message}`);
    addEvent({ title: "Agent 错误", detail: event.message, status: "error" });
  }
}

function setRunning(isRunning, label = "Agent 正在思考") {
  elements.send.disabled = isRunning;
  elements.input.disabled = isRunning;
  elements.thinking.classList.toggle("hidden", !isRunning);
  elements.thinkingLabel.textContent = label;
  if (isRunning) scrollToBottom();
}

function scrollToBottom(smooth = true) {
  requestAnimationFrame(() => elements.conversation.scrollTo({ top: elements.conversation.scrollHeight, behavior: smooth ? "smooth" : "auto" }));
}

function resizeInput() {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 170)}px`;
}

async function fetchStatus() {
  try {
    const response = await fetch("/api/status");
    const status = await response.json();
    elements.workspace.textContent = status.workspace;
    elements.statusModel.textContent = status.model.split("/").pop().split(":")[0];
    elements.statusContext.textContent = `${Math.round(status.context_length / 1024)}K`;
    elements.statusDisk.textContent = `${status.disk_free_gb} GB`;
    elements.sidebarModel.textContent = "Qwythos 9B · Q4_K_M";
    elements.sidebarStatus.textContent = status.model_loaded ? "模型已加载" : status.model_installed ? "已安装 · 等待任务" : "模型未安装";
    elements.sidebarStatusDot.classList.toggle("offline", !status.model_installed);
  } catch (_) {
    elements.sidebarStatus.textContent = "Ollama 未连接";
    elements.sidebarStatusDot.classList.add("offline");
  }
}

function closeOverlays() {
  elements.taskSidebar.classList.remove("open");
  elements.activityPanel.classList.remove("open");
  elements.scrim.classList.remove("show");
}

elements.form.addEventListener("submit", event => {
  event.preventDefault();
  sendMessage(elements.input.value);
});
elements.input.addEventListener("input", resizeInput);
elements.input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});
elements.newTask.addEventListener("click", () => {
  if (running) return;
  const task = newTask();
  state.tasks.unshift(task);
  state.activeId = task.id;
  saveState();
  renderAll();
  closeOverlays();
  elements.input.focus();
});
elements.taskList.addEventListener("click", async event => {
  const deleteId = event.target.dataset.delete;
  if (deleteId) {
    event.stopPropagation();
    if (running) return;
    state.tasks = state.tasks.filter(task => task.id !== deleteId);
    try { await fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: deleteId }) }); } catch (_) {}
    if (!state.tasks.length) state.tasks.push(newTask());
    if (state.activeId === deleteId) state.activeId = state.tasks[0].id;
    saveState();
    renderAll();
    return;
  }
  const item = event.target.closest(".task-item");
  if (!item || running) return;
  state.activeId = item.dataset.id;
  saveState();
  renderAll();
  closeOverlays();
});
document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => {
  elements.input.value = button.dataset.prompt;
  resizeInput();
  elements.input.focus();
}));
elements.sidebarToggle.addEventListener("click", () => { elements.taskSidebar.classList.add("open"); elements.scrim.classList.add("show"); });
elements.activityToggle.addEventListener("click", () => { elements.activityPanel.classList.add("open"); elements.scrim.classList.add("show"); });
elements.closeActivity.addEventListener("click", closeOverlays);
elements.scrim.addEventListener("click", closeOverlays);
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
    event.preventDefault();
    elements.newTask.click();
  }
  if (event.key === "Escape") closeOverlays();
});

renderAll();
fetchStatus();
elements.input.focus();


