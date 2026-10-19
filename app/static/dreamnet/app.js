/* Dreamnet - private dark romance studio.
   One-file single-page app: hash routes, a small fetch helper and views. */
(function () {
  "use strict";

  var view = document.getElementById("view");
  var state = { me: null, models: [] };
  var cleanup = null; // called when leaving a view

  // ---------- helpers ----------

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Role-play text: *actions* in italics, everything else escaped.
  function rp(s) {
    return esc(s).replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  }

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.status === 401) { showLogin(); throw new Error("Please sign in"); }
        if (!r.ok) throw new Error(data.error || data.detail || "Something went wrong (" + r.status + ")");
        return data;
      });
    });
  }

  // POST and read a server-sent event stream; onDelta gets the text so far.
  function stream(path, body, onDelta, signal) {
    return fetch(path, {
      method: "POST", credentials: "same-origin", signal: signal,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(function (r) {
      var type = r.headers.get("content-type") || "";
      if (type.indexOf("text/event-stream") === -1) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (!r.ok) throw new Error(data.error || data.detail || "Request failed (" + r.status + ")");
          if (data.content) onDelta(data.content);
          return { text: data.content || "", crisis: !!data.crisis };
        });
      }
      var reader = r.body.getReader();
      var dec = new TextDecoder();
      var buf = "", text = "", error = null;
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            if (error) throw new Error(error);
            return { text: text };
          }
          buf += dec.decode(res.value, { stream: true });
          var lines = buf.split("\n");
          buf = lines.pop();
          lines.forEach(function (line) {
            line = line.trim();
            if (line.indexOf("data: ") !== 0 || line === "data: [DONE]") return;
            try {
              var data = JSON.parse(line.slice(6));
              if (data.error) { error = data.error.message || "The model returned an error"; return; }
              (data.choices || []).forEach(function (ch) {
                var d = (ch.delta && ch.delta.content) || "";
                if (d) { text += d; onDelta(text); }
              });
            } catch (e) { /* partial line */ }
          });
          return pump();
        });
      }
      return pump();
    });
  }

  function avatar(c, cls) {
    var style = c.avatar
      ? "background-image:url('" + esc(c.avatar) + "')"
      : "background:linear-gradient(135deg," + esc(c.color || "#be123c") + ",#1a0f1f)";
    return '<div class="avatar ' + (cls || "") + '" style="' + style + '">' +
      (c.avatar ? "" : esc((c.name || "?").charAt(0))) + "</div>";
  }

  function words(s) {
    var m = (s || "").trim().match(/\S+/g);
    return m ? m.length : 0;
  }

  function toast(msg, bad) {
    var t = document.createElement("div");
    t.className = "toast" + (bad ? " bad" : "");
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 3500);
  }

  function ago(ts) {
    var s = Date.now() / 1000 - ts;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  // Time marks: "9:04 PM", "Oct 18, 2026 9:04 PM", and a day heading for chats.
  function clock(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  function stamp(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) + " " + clock(ts);
  }
  // In-universe clock, stored as "2026-10-18T23:40" (no time zone).
  function hhmm(d) {
    var h = d.getHours() % 12 || 12, m = d.getMinutes();
    return (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m + (d.getHours() < 12 ? " AM" : " PM");
  }
  // Day 1 is the day the scene starts on.
  function storyDay(iso, start) {
    if (!iso || !start) return 1;
    var a = new Date(start.slice(0, 10) + "T00:00"), b = new Date(iso.slice(0, 10) + "T00:00");
    return Math.max(1, Math.round((b - a) / 86400000) + 1);
  }
  function storyClock(iso, start, full) {
    if (!iso) return "";
    var d = new Date(iso);
    return "Day " + storyDay(iso, start) + (full ? " · " + d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + ", " : " · ") + hhmm(d);
  }
  // Time of day and moon phase, the same rules the server gives the model.
  var PHASES = [["New moon", "🌑"], ["Waxing crescent", "🌒"], ["First quarter", "🌓"], ["Waxing gibbous", "🌔"],
    ["Full moon", "🌕"], ["Waning gibbous", "🌖"], ["Last quarter", "🌗"], ["Waning crescent", "🌘"]];
  function sky(iso) {
    var d = new Date(iso), h = d.getHours();
    var age = (((d - new Date(2000, 0, 6, 18, 14)) / 86400000) % 29.530588853 + 29.530588853) % 29.530588853;
    var p = PHASES[Math.floor(age / 29.530588853 * 8 + 0.5) % 8];
    return { part: h >= 21 || h < 5 ? "Night" : h < 7 ? "Dawn" : h < 18 ? "Day" : "Dusk", moon: p[0], icon: p[1] };
  }
  function nowLine(iso, start) {
    return "[Current Time: " + hhmm(new Date(iso)) + " | Day " + storyDay(iso, start) + "]";
  }
  function stripTimeTags(text, typing) {
    text = String(text || "").replace(/[*_]*\[\s*(time passed|elapsed time|current time)\s*:[^\]]*\][*_]*/gi, "");
    return (typing ? text.replace(/\[[^\]]*$/, "") : text).replace(/\s+$/, "");
  }
  function dayLabel(ts) {
    return new Date(ts * 1000).toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  }

  function setNav(name) {
    document.querySelectorAll("[data-nav]").forEach(function (a) {
      a.classList.toggle("active", a.getAttribute("data-nav") === name);
    });
  }

  // ---------- auth ----------

  function showLogin() {
    document.getElementById("topbar").hidden = true;
    view.className = "login-view";
    view.innerHTML =
      '<form class="login-card form" id="loginForm">' +
      '  <div class="brand big"><img class="brand-mark" src="/static/dreamnet/icon.svg" alt=""> Dreamnet</div>' +
      '  <p class="muted">Your private dark romance studio.</p>' +
      '  <label>Username<input name="username" autocomplete="username" value="admin" required></label>' +
      '  <label>Password<input name="password" type="password" autocomplete="current-password" required autofocus></label>' +
      '  <p class="form-error" id="loginErr"></p>' +
      '  <button class="btn primary wide">Unlock</button>' +
      "</form>";
    document.getElementById("loginForm").onsubmit = function (e) {
      e.preventDefault();
      var f = e.target;
      fetch("/api/login", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: f.username.value, password: f.password.value }),
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (!res.ok) { document.getElementById("loginErr").textContent = res.d.error || "Wrong password"; return; }
          boot();
        });
    };
  }

  function renderAccount() {
    var el = document.getElementById("account");
    el.innerHTML =
      '<div class="menu"><button class="btn small ghost" id="menuBtn">' + esc(state.me.username) + " ▾</button>" +
      '<div class="menu-list" id="menuList" hidden>' +
      (state.me.is_admin ? '<a href="/admin">Models &amp; usage</a>' : "") +
      '<button id="logoutBtn">Lock</button></div></div>';
    document.getElementById("menuBtn").onclick = function () {
      var l = document.getElementById("menuList"); l.hidden = !l.hidden;
    };
    document.getElementById("logoutBtn").onclick = function () {
      fetch("/api/logout", { method: "POST", credentials: "same-origin" }).then(showLogin);
    };
  }

  // ---------- router ----------

  var routes = [
    [/^#?\/?$/, viewHome],
    [/^#\/characters$/, viewCharacters],
    [/^#\/character\/new$/, function () { viewCharacterForm(null); }],
    [/^#\/time-rules$/, viewTimeRules],
    [/^#\/character\/(\d+)$/, function (m) { viewCharacterForm(+m[1]); }],
    [/^#\/chats$/, function () { viewChat(null); }],
    [/^#\/chat\/(\d+)$/, function (m) { viewChat(+m[1]); }],
    [/^#\/novel\/new$/, function () { viewBible(null); }],
    [/^#\/novel\/(\d+)\/bible$/, function (m) { viewBible(+m[1]); }],
    [/^#\/novel\/(\d+)(?:\/ch\/(\d+))?$/, function (m) { viewStudio(+m[1], m[2] ? +m[2] : null); }],
  ];

  function route() {
    if (!state.me) return;
    if (cleanup) { try { cleanup(); } catch (e) {} cleanup = null; }
    var h = location.hash || "#/";
    view.className = "";
    window.scrollTo(0, 0);
    for (var i = 0; i < routes.length; i++) {
      var m = h.match(routes[i][0]);
      if (m) { routes[i][1](m); return; }
    }
    viewHome();
  }

  // ---------- home ----------

  function viewHome() {
    setNav("home");
    view.innerHTML = '<div class="loading">Loading…</div>';
    Promise.all([api("/app/api/novels"), api("/app/api/conversations"), api("/app/api/characters")]).then(function (r) {
      var novels = r[0], convs = r[1], chars = r[2];
      view.innerHTML =
        '<section class="hero">' +
        "  <div><h1>Write the love story <span>no one should want.</span></h1>" +
        '  <p>Your private studio for dark romance: plan the story bible, draft chapters with an AI co-author, and step into scenes with your characters.</p>' +
        '  <div class="row"><a class="btn primary" href="#/novel/new">Start a new novel</a><a class="btn" href="#/characters">Meet the characters</a></div></div>' +
        '  <div class="hero-stack">' + chars.slice(0, 3).map(function (c) { return avatar(c, "tall"); }).join("") + "</div>" +
        "</section>" +
        '<div class="section-head"><h2>Your novels</h2><a class="btn small" href="#/novel/new">+ New novel</a></div>' +
        (novels.length ? '<div class="novels">' + novels.map(function (n) {
          return '<a class="novel-card" href="#/novel/' + n.id + '">' +
            "<h3>" + esc(n.title) + "</h3>" +
            '<p class="muted">' + esc(n.premise || "No premise yet.").slice(0, 160) + "</p>" +
            '<div class="tags">' + (n.tropes || "").split(",").filter(Boolean).slice(0, 4).map(function (t) {
              return '<span class="tag">' + esc(t.trim()) + "</span>"; }).join("") + "</div>" +
            '<div class="novel-meta">' + n.chapter_count + " chapters · " + (n.word_count || 0).toLocaleString() + " words · " + ago(n.updated_at) + "</div></a>";
        }).join("") + "</div>"
          : '<div class="empty panel">No novels yet. <a href="#/novel/new">Start your first one</a> and give it a story bible.</div>') +
        '<div class="section-head"><h2>Recent roleplay</h2><a class="btn small" href="#/characters">+ New scene</a></div>' +
        (convs.length ? '<div class="conv-row">' + convs.slice(0, 6).map(convLink).join("") + "</div>"
          : '<div class="empty panel">No roleplay yet. Pick a character to start a scene.</div>');
    }).catch(function (e) { view.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; });
  }

  function convLink(cv, active) {
    return '<a class="conv' + (active ? " active" : "") + '" href="#/chat/' + cv.id + '">' + avatar(cv) +
      '<div class="meta"><b>' + esc(cv.name) + (cv.novel_title ? ' <small class="muted">· ' + esc(cv.novel_title) + "</small>" : "") +
      "</b><span>" + esc((cv.last || "").replace(/\*/g, "")) + "</span></div></a>";
  }

  // ---------- characters ----------

  function viewCharacters() {
    setNav("characters");
    api("/app/api/characters").then(function (chars) {
      view.innerHTML =
        '<div class="section-head"><h2>Characters</h2><a class="btn small primary" href="#/character/new">+ Create character</a></div>' +
        '<p class="muted lead">Click a character to start a roleplay scene. Add them to a novel\'s cast from its story bible.</p>' +
        '<div class="grid">' + chars.map(function (c) {
          return '<div class="card" data-chat="' + c.id + '">' + avatar(c) +
            '<a class="edit-link" href="#/character/' + c.id + '" title="Edit">✎</a>' +
            '<div class="info"><h3>' + esc(c.name) + " <small>" + c.age + "</small></h3><p>" + esc(c.tagline) + "</p>" +
            '<div class="tags">' + c.tags.split(",").filter(Boolean).map(function (t) {
              return '<span class="tag">' + esc(t.trim()) + "</span>"; }).join("") + "</div></div></div>";
        }).join("") +
        '<a class="card new" href="#/character/new"><span class="plus">+</span>Create a character</a></div>';
      view.querySelectorAll("[data-chat]").forEach(function (el) {
        el.onclick = function (e) {
          if (e.target.closest(".edit-link")) return;
          api("/app/api/conversations", { method: "POST", body: { character_id: +el.getAttribute("data-chat") } })
            .then(function (r) { location.hash = "#/chat/" + r.id; });
        };
      });
    });
  }

  var TRAITS = ["possessive", "morally grey", "obsessive", "protective", "cold", "brooding", "dominant", "jealous",
    "charming", "ruthless", "tortured past", "sarcastic", "tender underneath", "dangerous", "forbidden"];

  function viewCharacterForm(id) {
    setNav("characters");
    var load = id ? api("/app/api/characters/" + id) : Promise.resolve({
      name: "", age: 30, gender: "male", tagline: "", appearance: "", persona: "", greeting: "", tags: "", color: "#be123c", avatar: "",
    });
    load.then(function (c) {
      view.innerHTML =
        '<div class="create">' +
        '<div class="preview"><div class="card" id="pv"></div></div>' +
        '<form class="panel form" id="charForm">' +
        "<h2>" + (id ? "Edit " + esc(c.name) : "Create a character") + "</h2>" +
        '<div class="two"><label>Name<input name="name" required maxlength="60" value="' + esc(c.name) + '"></label>' +
        '<label>Age <span class="hint">18 or older</span><input name="age" type="number" min="18" max="9999" required value="' + esc(c.age) + '"></label></div>' +
        '<div class="two"><label>Gender<select name="gender">' + ["male", "female", "non-binary"].map(function (g) {
          return "<option" + (c.gender === g ? " selected" : "") + ">" + g + "</option>"; }).join("") + "</select></label>" +
        '<label>Colour<input name="color" type="color" value="' + esc(c.color) + '"></label></div>' +
        '<label>Tagline<input name="tagline" maxlength="160" placeholder="Mafia heir who never asks twice" value="' + esc(c.tagline) + '"></label>' +
        '<label>Traits <span class="hint">click to add to the personality</span><div class="choices">' +
        TRAITS.map(function (t) { return '<button type="button" class="chip" data-trait="' + t + '">' + t + "</button>"; }).join("") + "</div></label>" +
        '<label>Personality and backstory<textarea name="persona" rows="5" required placeholder="Who they are, what they want, what they hide, how they talk.">' + esc(c.persona) + "</textarea></label>" +
        '<label>Appearance<textarea name="appearance" rows="3" placeholder="Lifelike detail, even for supernatural characters: skin, hair, eyes, build, scars, clothes.">' + esc(c.appearance) + "</textarea></label>" +
        '<label>Opening line <span class="hint">how they greet you in a new scene; use *asterisks* for actions</span><textarea name="greeting" rows="3">' + esc(c.greeting) + "</textarea></label>" +
        '<div class="two"><label>Tags <span class="hint">comma separated</span><input name="tags" value="' + esc(c.tags) + '"></label>' +
        '<label>Portrait URL <span class="hint">optional, a photo-realistic image</span><input name="avatar" value="' + esc(c.avatar) + '"></label></div>' +
        portraitSection(id, c) +
        '<p class="form-error" id="charErr"></p>' +
        '<div class="row"><button class="btn primary">' + (id ? "Save changes" : "Create character") + "</button>" +
        (id ? '<button type="button" class="btn ghost danger" id="delChar">Delete</button>' : "") + "</div>" +
        "</form></div>";
      var form = document.getElementById("charForm");
      function preview() {
        var d = { name: form.name.value || "Name", age: form.age.value, tagline: form.tagline.value, color: form.color.value, avatar: form.avatar.value };
        document.getElementById("pv").innerHTML = avatar(d) + '<div class="info"><h3>' + esc(d.name) + " <small>" + esc(d.age) + "</small></h3><p>" + esc(d.tagline) + "</p></div>";
      }
      form.oninput = preview; preview();
      form.querySelectorAll("[data-trait]").forEach(function (b) {
        b.onclick = function () {
          var t = b.getAttribute("data-trait");
          form.persona.value = (form.persona.value.trim() ? form.persona.value.trim().replace(/\.?$/, ". ") : "") + "Deeply " + t + ".";
          form.tags.value = form.tags.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean).concat([t]).join(", ");
          b.classList.add("active");
        };
      });
      form.onsubmit = function (e) {
        e.preventDefault();
        var body = { id: id };
        ["name", "age", "gender", "color", "tagline", "persona", "appearance", "greeting", "tags", "avatar"].forEach(function (k) { body[k] = form[k].value; });
        api("/app/api/characters", { method: "POST", body: body })
          .then(function () { toast("Saved"); location.hash = "#/characters"; })
          .catch(function (err) { document.getElementById("charErr").textContent = err.message; });
      };
      var genP = document.getElementById("genPortrait");
      if (genP) genP.onclick = function () {
        var status = document.getElementById("portraitStatus");
        var promptEl = document.getElementById("portraitPrompt");
        genP.disabled = true;
        status.textContent = promptEl.value.trim() ? "Generating the portrait…" : "Writing a photo prompt, then generating…";
        // Save the card first so the prompt is written from what's on screen.
        var body = { id: id };
        ["name", "age", "gender", "color", "tagline", "persona", "appearance", "greeting", "tags", "avatar"].forEach(function (k) { body[k] = form[k].value; });
        api("/app/api/characters", { method: "POST", body: body })
          .then(function () { return api("/app/api/characters/" + id + "/portrait", { method: "POST", body: { prompt: promptEl.value } }); })
          .then(function (r) {
            form.avatar.value = r.avatar;
            promptEl.value = r.prompt;
            preview();
            status.textContent = "Done. Generate again for a different take, or edit the prompt first.";
          })
          .catch(function (err) { status.textContent = err.message; })
          .finally(function () { genP.disabled = false; });
      };
      var del = document.getElementById("delChar");
      if (del) del.onclick = function () {
        if (!confirm("Delete " + c.name + " and all their roleplay chats?")) return;
        api("/app/api/characters/" + id, { method: "DELETE" }).then(function () { location.hash = "#/characters"; });
      };
    });
  }

  function portraitSection(id, c) {
    var inner;
    if (!state.me.portraits) {
      inner = '<p class="hint">Lifelike portraits need a ComfyUI server with Z-Image-Turbo (AI Playground includes one). Set COMFYUI_URL in .env to turn this on.</p>';
    } else if (!id) {
      inner = '<p class="hint">Create the character first, then generate a lifelike portrait here.</p>';
    } else {
      inner = '<textarea id="portraitPrompt" rows="3" placeholder="Leave empty and the AI writes a photo prompt from this character.">' + esc(c.portrait_prompt || "") + "</textarea>" +
        '<div class="row"><button type="button" class="btn" id="genPortrait">Generate lifelike portrait</button>' +
        '<span class="hint" id="portraitStatus"></span></div>';
    }
    return '<fieldset class="cast portrait"><legend>Portrait <span class="hint">photorealistic, made with Z-Image-Turbo</span></legend>' + inner + "</fieldset>";
  }

  // ---------- roleplay chat ----------

  function viewChat(convId) {
    setNav("chats");
    view.className = "page-chat";
    var ctrl = null;
    cleanup = function () { if (ctrl) ctrl.abort(); };
    Promise.all([api("/app/api/conversations"), convId ? api("/app/api/conversations/" + convId) : null]).then(function (r) {
      var convs = r[0], conv = r[1];
      if (!conv && convs.length) { location.replace("#/chat/" + convs[0].id); return; }
      var side = '<aside class="sidebar"><h4>Scenes</h4>' + (convs.map(function (cv) { return convLink(cv, cv.id === convId); }).join("") ||
        '<p class="muted pad">No scenes yet.</p>') + '<a class="btn small wide" href="#/characters">+ New scene</a></aside>';
      if (!conv) {
        view.innerHTML = '<div class="chat-layout">' + side + '<section class="chat"><div class="empty">Pick a character to start a scene.<br><br><a class="btn primary" href="#/characters">Choose a character</a></div></section></div>';
        return;
      }
      var c = conv.character;
      view.innerHTML = '<div class="chat-layout">' + side +
        '<section class="chat">' +
        '<div class="chat-head">' + avatar(c) + '<div class="grow"><b>' + esc(c.name) + "</b><p>" +
        (conv.novel ? "Scene for <a href=\"#/novel/" + conv.novel.id + "\">" + esc(conv.novel.title) + "</a>" : esc(c.tagline)) + "</p></div>" +
        '<button class="btn small clock" id="clockBtn" title="In-story time. Set the clock or skip ahead.">🕒 <span id="clockNow"></span></button>' +
        '<button class="btn small" id="memBtn" title="What ' + esc(c.name) + ' remembers">Memory</button>' +
        '<button class="btn small" id="newScene" title="Start over with the opening line">New scene</button>' +
        '<button class="btn small ghost" id="delConv" title="Delete this scene">🗑</button></div>' +
        '<div class="memory-panel" id="memPanel" hidden></div>' +
        '<form class="clock-panel" id="clockPanel" hidden>' +
        '<label>Set the clock to<input type="datetime-local" id="clockSet"></label>' +
        '<label>or skip ahead<span class="row"><input type="number" id="skipH" min="0" max="672" placeholder="0"> h ' +
        '<input type="number" id="skipM" min="0" max="59" placeholder="0"> min</span></label>' +
        '<label class="grow">Label (optional)<input id="clockLabel" maxlength="120" placeholder="The next morning, his penthouse"></label>' +
        '<button class="btn small primary">Mark time</button><a class="btn small ghost" href="#/time-rules">Time rules</a></form>' +
        '<div class="messages" id="msgs"></div>' +
        '<form class="composer" id="composer"><textarea id="input" rows="1" placeholder="Write your reply. Use *asterisks* for actions."></textarea>' +
        '<button class="btn primary" id="sendBtn">Send</button></form>' +
        '<p class="notice">' + esc(c.name) + " is an AI character. Every character is a fictional adult.</p>" +
        "</section></div>";

      var msgs = document.getElementById("msgs");
      var input = document.getElementById("input");
      var sendBtn = document.getElementById("sendBtn");
      var list = conv.messages.slice();

      function currentClock() {
        for (var j = list.length - 1; j >= 0; j--) if (list[j].story_at) return list[j].story_at;
        return conv.clock;
      }
      function render() {
        var lastDay = "";
        var start = conv.start || currentClock();
        var sk = sky(currentClock());
        document.getElementById("clockNow").textContent = storyClock(currentClock(), start, true) + " · " + sk.part + " · " + sk.icon;
        document.getElementById("clockBtn").title = "In-story time. " + sk.part + ", " + sk.moon.toLowerCase() + ". Set the clock or skip ahead.";
        msgs.innerHTML = list.map(function (m, i) {
          var isLast = i === list.length - 1;
          var ts = m.created_at || Date.now() / 1000;
          var day = dayLabel(ts), head = "";
          if (day !== lastDay) { head = '<div class="day-mark"><span>' + esc(day) + "</span></div>"; lastDay = day; }
          if (m.role === "mark") {
            return head + '<div class="story-mark"><span>⏱ ' + esc(m.content) + (m.story_at ? " · " + esc(storyClock(m.story_at, start, true)) : "") +
              '</span><time title="Written ' + esc(stamp(ts)) + '">' + esc(clock(ts)) + '</time><button data-delmark="' + m.id + '" title="Remove this time mark">×</button></div>';
          }
          return head + '<div class="msg ' + m.role + '">' + (m.role === "assistant" ? avatar(c) : "") +
            '<div class="bubble-wrap"><div class="bubble' + (m.typing ? " typing" : "") + '">' + rp(stripTimeTags(m.content, m.typing)) +
            (m.role === "assistant" && m.story_at ? '<div class="now">' + esc(nowLine(m.story_at, start)) + "</div>" : "") + "</div>" +
            (m.id && !m.typing ? '<div class="msg-tools">' + (m.role === "user" && m.story_at ? '<time class="story" title="In-story time">🕒 ' + esc(storyClock(m.story_at, start)) + "</time>" : "") +
              '<time title="Written ' + esc(stamp(ts)) + '">' + esc(clock(ts)) + "</time>" +
              (m.role === "assistant" && isLast && i > 0 ? '<button data-regen="' + m.id + '">↻ Regenerate</button>' : "") +
              (conv.novel && m.role === "assistant" ? '<button data-tochapter="' + i + '">＋ Add to chapter</button>' : "") +
              (i > 0 ? '<button data-rewind="' + m.id + '">Rewind to here</button>' : "") + "</div>" : "") +
            "</div></div>";
        }).join("");
        msgs.scrollTop = msgs.scrollHeight;
      }
      render();

      function send(text) {
        sendBtn.disabled = true;
        if (text) list.push({ role: "user", content: text, created_at: Date.now() / 1000 });
        var pending = { role: "assistant", content: "", typing: true };
        list.push(pending);
        render();
        ctrl = new AbortController();
        stream("/app/api/conversations/" + convId + "/messages", { content: text }, function (t) {
          pending.content = t; render();
        }, ctrl.signal).then(function () {
          return api("/app/api/conversations/" + convId);
        }).then(function (fresh) {
          list = fresh.messages; render();
        }).catch(function (e) {
          if (e.name === "AbortError") return;
          list.pop(); render(); toast(e.message, true);
        }).finally(function () { sendBtn.disabled = false; ctrl = null; });
      }

      document.getElementById("composer").onsubmit = function (e) {
        e.preventDefault();
        var text = input.value.trim();
        if (!text || sendBtn.disabled) return;
        input.value = "";
        send(text);
      };
      input.onkeydown = function (e) {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); document.getElementById("composer").requestSubmit(); }
      };
      msgs.onclick = function (e) {
        var b = e.target.closest("button");
        if (!b) return;
        if (b.dataset.regen) {
          api("/app/api/messages/" + b.dataset.regen, { method: "DELETE" }).then(function () {
            list = list.filter(function (m) { return m.id < +b.dataset.regen; }); render(); send("");
          });
        } else if (b.dataset.rewind) {
          if (!confirm("Delete this message and everything after it?")) return;
          api("/app/api/messages/" + b.dataset.rewind, { method: "DELETE" }).then(function () {
            list = list.filter(function (m) { return m.id < +b.dataset.rewind; }); render();
          });
        } else if (b.dataset.delmark) {
          api("/app/api/marks/" + b.dataset.delmark, { method: "DELETE" }).then(function () {
            list = list.filter(function (m) { return m.id !== +b.dataset.delmark; }); render();
          });
        } else if (b.dataset.tochapter) {
          addToChapter(conv.novel.id, list[+b.dataset.tochapter].content);
        }
      };
      var clockPanel = document.getElementById("clockPanel");
      document.getElementById("clockBtn").onclick = function () {
        clockPanel.hidden = !clockPanel.hidden;
        if (!clockPanel.hidden) document.getElementById("clockSet").value = currentClock().slice(0, 16);
      };
      clockPanel.onsubmit = function (e) {
        e.preventDefault();
        var set = document.getElementById("clockSet").value;
        var skip = (+document.getElementById("skipH").value || 0) * 60 + (+document.getElementById("skipM").value || 0);
        var body = { story_time: document.getElementById("clockLabel").value.trim(), skip_minutes: skip };
        if (set && set !== currentClock().slice(0, 16)) body.set = set;
        if (!body.set && !skip && !body.story_time) { toast("Set a time, a skip or a label first.", true); return; }
        api("/app/api/conversations/" + convId + "/marks", { method: "POST", body: body })
          .then(function (m) { list.push(m); clockPanel.hidden = true; clockPanel.reset(); render(); })
          .catch(function (err) { toast(err.message, true); });
      };
      var memPanel = document.getElementById("memPanel");
      function loadMemory() {
        Promise.all([api("/app/api/characters/" + c.id + "/memories"), api("/app/api/conversations/" + convId)]).then(function (r) {
          var mems = r[0], fresh = r[1];
          memPanel.innerHTML =
            '<div class="mem-col"><h4>This scene so far' + (fresh.summary_upto ? ' <span class="hint">up to ' + esc(stamp(fresh.summary_upto)) + "</span>" : "") + "</h4>" +
            '<textarea id="sumText" rows="5" placeholder="Filled in automatically once the scene gets long. You can edit it.">' + esc(fresh.summary) + "</textarea>" +
            '<button class="btn small" id="sumSave">Save summary</button></div>' +
            '<div class="mem-col"><h4>' + esc(c.name) + " remembers <span class=\"hint\">across every scene</span></h4>" +
            '<form class="row" id="memAdd"><input id="memText" placeholder="Add something they should never forget"><button class="btn small">Add</button></form>' +
            '<ul class="mem-list">' + (mems.map(function (m) {
              var when = stamp(m.said_at || m.created_at) + (m.story_time ? " · story time: " + m.story_time : "");
              return '<li><div class="grow"><span>' + esc(m.content) + "</span>" +
                (m.quote ? '<q>' + esc(m.quote) + "</q>" : "") +
                '<small>' + esc(when) + " · " + (m.source === "user" ? "added by you" : "from a scene") + "</small></div>" +
                '<button data-delmem="' + m.id + '" title="Forget">×</button></li>';
            }).join("") || '<li class="muted">Nothing yet. Memories are added automatically as you play.</li>') + "</ul></div>";
          document.getElementById("sumSave").onclick = function () {
            api("/app/api/conversations/" + convId + "/summary", { method: "PUT", body: { summary: document.getElementById("sumText").value } })
              .then(function () { toast("Summary saved"); });
          };
          document.getElementById("memAdd").onsubmit = function (e) {
            e.preventDefault();
            var t = document.getElementById("memText").value.trim();
            if (!t) return;
            api("/app/api/characters/" + c.id + "/memories", { method: "POST", body: { content: t } }).then(loadMemory)
              .catch(function (err) { toast(err.message, true); });
          };
          memPanel.querySelectorAll("[data-delmem]").forEach(function (b) {
            b.onclick = function () { api("/app/api/memories/" + b.dataset.delmem, { method: "DELETE" }).then(loadMemory); };
          });
        });
      }
      document.getElementById("memBtn").onclick = function () {
        memPanel.hidden = !memPanel.hidden;
        if (!memPanel.hidden) loadMemory();
      };
      document.getElementById("newScene").onclick = function () {
        api("/app/api/conversations", { method: "POST", body: { character_id: c.id, novel_id: conv.novel ? conv.novel.id : null, new: true } })
          .then(function (r) { location.hash = "#/chat/" + r.id; });
      };
      document.getElementById("delConv").onclick = function () {
        if (!confirm("Delete this scene?")) return;
        api("/app/api/conversations/" + convId, { method: "DELETE" }).then(function () { location.hash = "#/chats"; });
      };
    });
  }

  // Time rules: the action-time table and pace the roleplay clock uses.
  function viewTimeRules() {
    setNav("chats");
    view.className = "";
    api("/app/api/time-rules").then(function (cfg) {
      var rows = cfg.actions.map(function (a) { return Object.assign({}, a); }), pace = cfg.pace;
      function fmt(m) { return m < 1 ? Math.round(m * 60) + " s" : m >= 60 && m % 60 === 0 ? m / 60 + " h" : m + " min"; }
      function draw() {
        view.innerHTML =
          '<form class="panel form time-rules" id="trForm"><h2>Time rules</h2>' +
          '<p class="hint">How much in-story time each kind of action takes. The AI picks a value in the range for what ' +
          "happened, the app adds it to the scene clock and prints <i>[Current Time: 02:45 AM | Day 3]</i> after every reply. " +
          "Times are in minutes; use decimals for seconds (0.5 = 30 seconds).</p>" +
          '<div class="seg" role="group" aria-label="Pace">' + cfg.paces.map(function (p) {
            return '<button type="button" data-pace="' + p + '" class="' + (p === pace ? "on" : "") + '">' + p.toFixed(1) + "x " +
              (p < 1 ? "Slow" : p > 1 ? "Fast" : "Real") + "</button>";
          }).join("") + "</div>" +
          '<div class="tr-rows">' + rows.map(function (a, i) {
            return '<div class="tr-row" data-i="' + i + '">' +
              '<input data-k="label" value="' + esc(a.label) + '" aria-label="Action" class="tr-label">' +
              '<span class="tr-range"><input data-k="min" type="number" min="0" step="any" value="' + a.min + '" aria-label="Minimum minutes"> to ' +
              '<input data-k="max" type="number" min="0" step="any" value="' + a.max + '" aria-label="Maximum minutes"> min</span>' +
              '<input data-k="per" value="' + esc(a.per) + '" placeholder="per…" aria-label="Per" class="tr-per">' +
              '<input data-k="context" value="' + esc(a.context) + '" placeholder="Context" aria-label="Context" class="tr-ctx">' +
              '<small class="tr-show">' + esc(a.min === a.max ? fmt(a.min) : fmt(a.min) + " – " + fmt(a.max)) + (a.per ? " / " + esc(a.per) : "") + "</small>" +
              '<button type="button" class="btn small ghost" data-del="' + i + '" title="Remove">×</button></div>';
          }).join("") + "</div>" +
          '<div class="row"><button type="button" class="btn small" id="trAdd">+ Add action</button>' +
          '<button type="button" class="btn small ghost" id="trReset">Reset to defaults</button>' +
          '<span class="grow"></span><button class="btn primary">Save time rules</button></div>' +
          '<p class="form-error" id="trErr"></p></form>';
        var form = document.getElementById("trForm");
        form.oninput = function (e) {
          var row = e.target.closest(".tr-row");
          if (!row) return;
          var a = rows[+row.dataset.i], k = e.target.dataset.k;
          a[k] = k === "min" || k === "max" ? parseFloat(e.target.value) || 0 : e.target.value;
          row.querySelector(".tr-show").textContent = (a.min === a.max ? fmt(a.min) : fmt(a.min) + " – " + fmt(a.max)) + (a.per ? " / " + a.per : "");
        };
        form.onclick = function (e) {
          var b = e.target.closest("button");
          if (!b) return;
          if (b.dataset.pace) { pace = +b.dataset.pace; draw(); }
          else if (b.dataset.del) { rows.splice(+b.dataset.del, 1); draw(); }
          else if (b.id === "trAdd") { rows.push({ label: "", min: 1, max: 5, per: "", context: "" }); draw(); }
          else if (b.id === "trReset") { rows = cfg.defaults.map(function (a) { return Object.assign({}, a); }); pace = 1; draw(); }
        };
        form.onsubmit = function (e) {
          e.preventDefault();
          api("/app/api/time-rules", { method: "PUT", body: { pace: pace, actions: rows } })
            .then(function () { toast("Time rules saved"); history.back(); })
            .catch(function (err) { document.getElementById("trErr").textContent = err.message; });
        };
      }
      draw();
    });
  }

  // Append roleplay text to the last chapter of a novel, as prose.
  function addToChapter(novelId, text) {
    api("/app/api/novels/" + novelId).then(function (n) {
      var ch = n.chapters[n.chapters.length - 1];
      if (!ch) return toast("That novel has no chapters", true);
      return api("/app/api/chapters/" + ch.id).then(function (full) {
        var content = (full.content.trim() ? full.content.replace(/\s*$/, "\n\n") : "") + text.replace(/\*([^*\n]+)\*/g, "$1");
        return api("/app/api/chapters/" + ch.id, { method: "PUT", body: { content: content } });
      }).then(function () { toast("Added to " + ch.title); });
    }).catch(function (e) { toast(e.message, true); });
  }

  // ---------- story bible ----------

  var TROPES = ["enemies to lovers", "forced proximity", "mafia", "captive", "stalker", "touch her and die",
    "forbidden", "revenge", "arranged marriage", "bodyguard", "age gap", "who did this to you", "morally grey hero",
    "fake dating", "villain gets the girl", "slow burn", "possessive hero", "dark academia (adults)"];

  function viewBible(id) {
    setNav("home");
    var blank = { title: "", premise: "", tropes: "", setting: "", pov: "first person, present tense", tone: "dark, obsessive, slow-burn",
      heat: "explicit", style_notes: "", model_id: "", cast: [] };
    Promise.all([id ? api("/app/api/novels/" + id) : Promise.resolve(blank), api("/app/api/characters")]).then(function (r) {
      var n = r[0], chars = r[1];
      var castMap = {};
      (n.cast || []).forEach(function (c) { castMap[c.character_id] = c.role; });
      view.innerHTML =
        '<form class="panel form bible" id="bibleForm">' +
        "<h2>" + (id ? "Story bible" : "Start a new novel") + "</h2>" +
        '<p class="muted">Everything here is given to the AI each time it writes, so it keeps your voice, cast and rules consistent.</p>' +
        '<label>Title<input name="title" required value="' + esc(n.title) + '" placeholder="Crown of Thorns"></label>' +
        '<label>Premise<textarea name="premise" rows="4" placeholder="The core story in a few sentences: who, what they want, what stands in the way.">' + esc(n.premise) + "</textarea></label>" +
        '<label>Tropes <span class="hint">click to add</span><div class="choices">' +
        TROPES.map(function (t) { return '<button type="button" class="chip" data-trope="' + t + '">' + t + "</button>"; }).join("") +
        '</div><input name="tropes" value="' + esc(n.tropes) + '"></label>' +
        '<label>Setting<textarea name="setting" rows="2" placeholder="Place, era, the world\'s rules.">' + esc(n.setting) + "</textarea></label>" +
        '<div class="two"><label>Point of view and tense<input name="pov" value="' + esc(n.pov) + '"></label>' +
        '<label>Tone<input name="tone" value="' + esc(n.tone) + '"></label></div>' +
        '<div class="two"><label>Heat level<select name="heat">' + ["closed door", "sensual", "steamy", "explicit"].map(function (h) {
          return "<option" + (n.heat === h ? " selected" : "") + ">" + h + "</option>"; }).join("") + "</select></label>" +
        '<label>AI model<select name="model_id"><option value="">Default (' + esc(state.me.default_model) + ")</option>" +
        state.models.map(function (m) { return '<option value="' + esc(m.model_id) + '"' + (n.model_id === m.model_id ? " selected" : "") + ">" + esc(m.display_name) + "</option>"; }).join("") +
        "</select></label></div>" +
        '<label>Style notes <span class="hint">voice, words to avoid, content warnings, anything the AI must follow</span><textarea name="style_notes" rows="3">' + esc(n.style_notes) + "</textarea></label>" +
        '<fieldset class="cast"><legend>Cast <span class="hint">tick characters and give each a role</span></legend>' +
        chars.map(function (c) {
          var on = Object.prototype.hasOwnProperty.call(castMap, c.id);
          return '<div class="cast-row">' + avatar(c) + '<label class="check"><input type="checkbox" data-cast="' + c.id + '"' + (on ? " checked" : "") + "> " + esc(c.name) + "</label>" +
            '<input data-role="' + c.id + '" placeholder="Role, e.g. love interest" value="' + esc(castMap[c.id] || "") + '"></div>';
        }).join("") + '<a class="btn small" href="#/character/new">+ New character</a></fieldset>' +
        '<p class="form-error" id="bibleErr"></p>' +
        '<div class="row"><button class="btn primary">' + (id ? "Save bible" : "Create novel") + "</button>" +
        (id ? '<a class="btn" href="#/novel/' + id + '">Back to writing</a><button type="button" class="btn ghost danger" id="delNovel">Delete novel</button>' : "") +
        "</div></form>";
      var form = document.getElementById("bibleForm");
      form.querySelectorAll("[data-trope]").forEach(function (b) {
        b.onclick = function () {
          var list = form.tropes.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
          var t = b.getAttribute("data-trope");
          if (list.indexOf(t) === -1) list.push(t);
          form.tropes.value = list.join(", ");
        };
      });
      form.onsubmit = function (e) {
        e.preventDefault();
        var body = { id: id, cast: [] };
        ["title", "premise", "tropes", "setting", "pov", "tone", "heat", "style_notes", "model_id"].forEach(function (k) { body[k] = form[k].value; });
        form.querySelectorAll("[data-cast]").forEach(function (cb) {
          if (cb.checked) body.cast.push({ character_id: +cb.dataset.cast, role: form.querySelector('[data-role="' + cb.dataset.cast + '"]').value });
        });
        api("/app/api/novels", { method: "POST", body: body })
          .then(function (r) { location.hash = "#/novel/" + r.id; })
          .catch(function (err) { document.getElementById("bibleErr").textContent = err.message; });
      };
      var del = document.getElementById("delNovel");
      if (del) del.onclick = function () {
        if (!confirm("Delete " + n.title + " and all its chapters? This can't be undone.")) return;
        api("/app/api/novels/" + id, { method: "DELETE" }).then(function () { location.hash = "#/"; });
      };
    });
  }

  // ---------- writing studio ----------

  var MODES = [
    ["continue", "Continue", "Keep writing from where the chapter ends."],
    ["scene", "Write scene", "Describe the beat you want written."],
    ["dialogue", "Dialogue", "Who is talking and what is at stake."],
    ["describe", "Describe", "A place, a person or a moment to bring alive."],
    ["rewrite", "Rewrite", "Select text in the chapter, then say how to change it."],
    ["brainstorm", "Brainstorm", "Ask for twists, scene ideas or lines."],
    ["outline", "Outline", "Plan the next chapters."],
  ];

  function viewStudio(novelId, chapterId) {
    setNav("home");
    view.className = "page-studio";
    api("/app/api/novels/" + novelId).then(function (n) {
      if (!chapterId && n.chapters.length) { location.replace("#/novel/" + novelId + "/ch/" + n.chapters[n.chapters.length - 1].id); return; }
      return (chapterId ? api("/app/api/chapters/" + chapterId) : Promise.resolve(null)).then(function (ch) { renderStudio(n, ch); });
    }).catch(function (e) { view.innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; });
  }

  function renderStudio(n, ch) {
    var mode = "continue", ctrl = null, saveTimer = null, dirty = false, output = "";
    view.innerHTML =
      '<div class="studio">' +
      '<aside class="sidebar chapters">' +
      '<div class="novel-title"><b>' + esc(n.title) + '</b><a class="btn small ghost" href="#/novel/' + n.id + '/bible">Story bible</a></div>' +
      "<h4>Chapters</h4>" +
      n.chapters.map(function (c) {
        return '<div class="chapter-item' + (ch && c.id === ch.id ? " active" : "") + '"><a href="#/novel/' + n.id + "/ch/" + c.id + '">' +
          '<span class="num">' + c.position + "</span>" + esc(c.title) + "</a>" +
          '<span class="ch-tools"><button data-move="up" data-id="' + c.id + '" title="Move up">↑</button><button data-move="down" data-id="' + c.id + '" title="Move down">↓</button></span></div>';
      }).join("") +
      '<button class="btn small wide" id="addCh">+ Add chapter</button>' +
      "<h4>Cast</h4>" +
      (n.cast.length ? n.cast.map(function (c) {
        return '<button class="cast-chip" data-scene="' + c.character_id + '" title="Roleplay a scene with ' + esc(c.name) + '">' + avatar(c) +
          "<span><b>" + esc(c.name) + "</b><small>" + esc(c.role || "cast") + "</small></span></button>";
      }).join("") : '<p class="muted pad">Add characters in the story bible.</p>') +
      '<a class="btn small ghost wide" href="/app/api/novels/' + n.id + '/export">Export manuscript (.md)</a>' +
      "</aside>" +
      (ch ? '<section class="editor">' +
        '<div class="editor-head"><input id="chTitle" class="ch-title" value="' + esc(ch.title) + '">' +
        '<span class="muted small" id="saveState">Saved</span>' +
        '<button class="btn small ghost danger" id="delCh" title="Delete chapter">🗑</button></div>' +
        '<textarea id="chText" class="manuscript" placeholder="Start writing, or ask the co-author to begin the chapter.">' + esc(ch.content) + "</textarea>" +
        '<details class="summary"><summary>Chapter summary <span class="muted">(used for continuity in later chapters)</span></summary>' +
        '<textarea id="chSummary" rows="3">' + esc(ch.summary) + '</textarea><button class="btn small" id="sumBtn">Summarize with AI</button></details>' +
        '<div class="editor-foot muted small"><span id="wc"></span></div></section>'
        : '<section class="editor empty">No chapters yet. <button class="btn primary" id="addCh2">Add the first chapter</button></section>') +
      '<aside class="coauthor">' +
      "<h4>Co-author</h4>" +
      '<div class="modes">' + MODES.map(function (m) { return '<button class="chip' + (m[0] === mode ? " active" : "") + '" data-mode="' + m[0] + '">' + m[1] + "</button>"; }).join("") + "</div>" +
      '<textarea id="instr" rows="4" placeholder="' + MODES[0][2] + '"></textarea>' +
      '<div class="row"><select id="len"><option value="short">Short</option><option value="medium" selected>Medium</option><option value="long">Long</option></select>' +
      '<button class="btn primary grow" id="genBtn">Write</button></div>' +
      '<div class="output" id="out"><p class="muted">Generated text appears here. Nothing changes in your chapter until you insert it.</p></div>' +
      '<div class="row out-tools" id="outTools" hidden><button class="btn small" data-out="append">Append to chapter</button>' +
      '<button class="btn small" data-out="cursor">Insert at cursor</button><button class="btn small" data-out="replace">Replace selection</button>' +
      '<button class="btn small ghost" data-out="discard">Discard</button></div>' +
      "</aside></div>";

    var addCh = function () {
      api("/app/api/novels/" + n.id + "/chapters", { method: "POST", body: {} }).then(function (r) { location.hash = "#/novel/" + n.id + "/ch/" + r.id; });
    };
    ["addCh", "addCh2"].forEach(function (bid) {
      var b = document.getElementById(bid);
      if (b) b.onclick = addCh;
    });

    view.querySelectorAll("[data-move]").forEach(function (b) {
      b.onclick = function () {
        api("/app/api/chapters/" + b.dataset.id + "/move", { method: "POST", body: { direction: b.dataset.move } }).then(route);
      };
    });
    view.querySelectorAll("[data-scene]").forEach(function (b) {
      b.onclick = function () {
        api("/app/api/conversations", { method: "POST", body: { character_id: +b.dataset.scene, novel_id: n.id } })
          .then(function (r) { location.hash = "#/chat/" + r.id; });
      };
    });

    var text = document.getElementById("chText");
    var instr = document.getElementById("instr");
    var out = document.getElementById("out");
    var outTools = document.getElementById("outTools");
    var genBtn = document.getElementById("genBtn");
    var lastSel = { start: 0, end: 0 };

    function save() {
      if (!ch || !dirty) return Promise.resolve();
      dirty = false;
      document.getElementById("saveState").textContent = "Saving…";
      return api("/app/api/chapters/" + ch.id, { method: "PUT", body: {
        title: document.getElementById("chTitle").value, content: text.value, summary: document.getElementById("chSummary").value } })
        .then(function () { document.getElementById("saveState").textContent = "Saved"; })
        .catch(function (e) { dirty = true; document.getElementById("saveState").textContent = "Not saved: " + e.message; });
    }
    function touch() {
      dirty = true;
      document.getElementById("saveState").textContent = "Editing…";
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 1200);
      if (text) document.getElementById("wc").textContent = words(text.value).toLocaleString() + " words";
    }
    window.onbeforeunload = function () { if (dirty) { save(); return "Unsaved changes"; } };
    cleanup = function () { clearTimeout(saveTimer); save(); if (ctrl) ctrl.abort(); window.onbeforeunload = null; };

    if (ch) {
      text.oninput = touch;
      document.getElementById("chTitle").oninput = touch;
      document.getElementById("chSummary").oninput = touch;
      document.getElementById("wc").textContent = words(text.value).toLocaleString() + " words";
      text.onselect = text.onkeyup = text.onmouseup = function () { lastSel = { start: text.selectionStart, end: text.selectionEnd }; };
      document.getElementById("delCh").onclick = function () {
        if (!confirm("Delete " + ch.title + "? This can't be undone.")) return;
        dirty = false;
        api("/app/api/chapters/" + ch.id, { method: "DELETE" }).then(function () { location.hash = "#/novel/" + n.id; });
      };
      document.getElementById("sumBtn").onclick = function (e) {
        e.preventDefault();
        var sumEl = document.getElementById("chSummary");
        save().then(function () {
          sumEl.value = "Summarizing…";
          return stream("/app/api/chapters/" + ch.id + "/summarize", {}, function (t) { sumEl.value = t; });
        }).catch(function (err) { sumEl.value = ch.summary || ""; toast(err.message, true); });
      };
    }

    view.querySelectorAll("[data-mode]").forEach(function (b) {
      b.onclick = function () {
        mode = b.dataset.mode;
        view.querySelectorAll("[data-mode]").forEach(function (x) { x.classList.toggle("active", x === b); });
        instr.placeholder = MODES.filter(function (m) { return m[0] === mode; })[0][2];
      };
    });

    genBtn.onclick = function () {
      if (ctrl) { ctrl.abort(); return; }
      var selection = text ? text.value.slice(lastSel.start, lastSel.end) : "";
      if (mode === "rewrite" && !selection) { toast("Select the passage to rewrite in the chapter first.", true); return; }
      save().then(function () {
        ctrl = new AbortController();
        genBtn.textContent = "Stop";
        out.classList.add("typing-out");
        out.innerHTML = '<div class="gen typing"></div>';
        outTools.hidden = true;
        return stream("/app/api/novels/" + n.id + "/assist", {
          chapter_id: ch ? ch.id : null, mode: mode, instruction: instr.value, selection: selection,
          length: document.getElementById("len").value,
        }, function (t) { output = t; out.firstChild.textContent = t; out.scrollTop = out.scrollHeight; }, ctrl.signal);
      }).then(function (res) {
        if (res) output = res.text;
      }).catch(function (e) {
        if (e.name !== "AbortError") toast(e.message, true);
      }).finally(function () {
        ctrl = null;
        genBtn.textContent = "Write";
        out.classList.remove("typing-out");
        var g = out.querySelector(".gen");
        if (g) g.classList.remove("typing");
        outTools.hidden = !output || !ch;
      });
    };

    outTools.onclick = function (e) {
      var b = e.target.closest("[data-out]");
      if (!b || !ch) return;
      var v = text.value, act = b.dataset.out;
      if (act === "append") {
        text.value = (v.trim() ? v.replace(/\s*$/, "\n\n") : "") + output;
      } else if (act === "cursor") {
        text.value = v.slice(0, lastSel.end) + output + v.slice(lastSel.end);
      } else if (act === "replace") {
        if (lastSel.start === lastSel.end) { toast("Select text in the chapter to replace.", true); return; }
        text.value = v.slice(0, lastSel.start) + output + v.slice(lastSel.end);
      }
      if (act !== "discard") { touch(); toast("Inserted into " + ch.title); }
      output = "";
      out.innerHTML = '<p class="muted">Generated text appears here.</p>';
      outTools.hidden = true;
      if (act === "append") text.scrollTop = text.scrollHeight;
    };
  }

  // ---------- boot ----------

  function boot() {
    api("/app/api/me").then(function (me) {
      if (!me.authenticated) { state.me = null; showLogin(); return; }
      state.me = me;
      document.getElementById("topbar").hidden = false;
      renderAccount();
      return api("/app/api/models").then(function (m) { state.models = m; }).catch(function () {}).then(route);
    });
  }

  window.addEventListener("hashchange", route);
  document.addEventListener("click", function (e) {
    var list = document.getElementById("menuList");
    if (list && !e.target.closest(".menu")) list.hidden = true;
  });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(function () {});
  boot();
})();
