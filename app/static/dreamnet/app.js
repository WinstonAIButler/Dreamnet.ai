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
  // Roleplay text: "spoken words", *actions* and `thoughts` (or ((thoughts))) each get their own font and color.
  var RP_PARTS = /(`[^`\n]+`|\(\([^\n]+?\)\)|\*[^*\n]+\*|"[^"\n]+"|\u201c[^\u201d\n]+\u201d)/g;
  function rp(s) {
    return String(s == null ? "" : s).split(RP_PARTS).map(function (part, i) {
      if (i % 2 === 0) return esc(part);
      var c = part.charAt(0);
      if (c === "`") return '<span class="rp-thought">' + esc(part.slice(1, -1)) + "</span>";
      if (c === "(") return '<span class="rp-thought">' + esc(part.slice(2, -2)) + "</span>";
      if (c === "*") return '<em class="rp-action">' + esc(part.slice(1, -1)) + "</em>";
      return '<span class="rp-speech">' + esc(part) + "</span>";
    }).join("");
  }

  // ---------- read aloud ----------
  // Neural voices come from the server (English voices by country); if it can't
  // reach the online voice service, the device's own English voices are used.
  var voiceInfo = null, player = null, onlineFailed = false, vsWarned = false, voiceHook = null;
  function pref(k, d) { try { var v = localStorage.getItem("dn." + k); return v == null ? d : v; } catch (e) { return d; } }
  function setPref(k, v) { try { localStorage.setItem("dn." + k, v); } catch (e) { /* private mode */ } }
  function loadVoices() {
    if (!voiceInfo) voiceInfo = api("/app/api/voices").catch(function () { return { enabled: false, regions: [], voices: [], tones: [], defaults: {} }; });
    return voiceInfo;
  }
  function deviceVoices() {
    return (window.speechSynthesis ? speechSynthesis.getVoices() : []).filter(function (v) { return /^en[-_]/i.test(v.lang); });
  }
  var TONE_PITCH = { deep: 0.75, low: 0.88, natural: 1, bright: 1.15 };
  function speakableText(text, mode) {
    text = stripTimeTags(text);
    var out = [];
    String(text).split(RP_PARTS).forEach(function (p, i) {
      if (!p.trim()) return;
      var c = p.charAt(0), kind = i % 2 === 0 ? "narration" : c === "`" || c === "(" ? "thought" : c === "*" ? "action" : "speech";
      if (kind === "thought") p = p.replace(/^[`(]+|[`)]+$/g, "");
      if (kind === "action") p = p.replace(/^\*|\*$/g, "");
      if (mode === "words" && kind !== "speech") return;
      if (mode === "words_thoughts" && kind !== "speech" && kind !== "thought") return;
      out.push(p.trim());
    });
    return out.join(" ").replace(/\s+/g, " ").trim();
  }
  // Sound effects for accent presets, applied as the voice plays (online or VoiceStudio audio).
  var actx = null;
  function audioCtx() {
    if (!actx) { var C = window.AudioContext || window.webkitAudioContext; if (C) actx = new C(); }
    return actx;
  }
  function impulse(ctx, secs, decay) {  // a simple room: decaying noise
    var len = Math.floor(ctx.sampleRate * secs), buf = ctx.createBuffer(2, len, ctx.sampleRate);
    for (var ch = 0; ch < 2; ch++) {
      var d = buf.getChannelData(ch);
      for (var i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, decay);
    }
    return buf;
  }
  function filter(ctx, type, freq, gain) {
    var f = ctx.createBiquadFilter(); f.type = type; f.frequency.value = freq; if (gain) f.gain.value = gain; return f;
  }
  function grit(ctx, amount) {
    var ws = ctx.createWaveShaper(), n = 1024, curve = new Float32Array(n);
    for (var i = 0; i < n; i++) { var x = i * 2 / n - 1; curve[i] = (1 + amount) * x / (1 + amount * Math.abs(x)); }
    ws.curve = curve; return ws;
  }
  var SOUND_FX = {  // [filters, grit, reverb seconds, reverb mix]
    innocent: function (ctx) { return [[filter(ctx, "highshelf", 3500, 4)], 0, 0.8, 0.08]; },
    mysterious: function (ctx) { return [[filter(ctx, "lowpass", 6000)], 0, 2.6, 0.35]; },
    villain: function (ctx) { return [[filter(ctx, "lowshelf", 220, 6), ctx.createDynamicsCompressor()], 3, 1.2, 0.15]; },
    possessed: function (ctx) { return [[filter(ctx, "lowshelf", 160, 8), filter(ctx, "lowpass", 5000)], 10, 3.2, 0.45]; }
  };
  function playAudio(blob, sound) {
    var url = URL.createObjectURL(blob), a = new Audio(url), ctx = SOUND_FX[sound] ? audioCtx() : null;
    if (!ctx) return a;
    try {
      if (ctx.state === "suspended") ctx.resume();
      var fx = SOUND_FX[sound](ctx), head = ctx.createMediaElementSource(a);
      var out = ctx.createGain(), dry = ctx.createGain(), wet = ctx.createGain(), verb = ctx.createConvolver();
      out.connect(ctx.destination); dry.connect(out); verb.connect(wet); wet.connect(out);
      fx[0].forEach(function (n) { head.connect(n); head = n; });
      if (fx[1]) { var g = grit(ctx, fx[1]); head.connect(g); head = g; }
      verb.buffer = impulse(ctx, fx[2], 2.5); wet.gain.value = fx[3]; dry.gain.value = 1 - fx[3] / 2;
      head.connect(dry); head.connect(verb);
      if (sound === "possessed") {  // a second, slower and deeper copy of the voice dragging under the first
        var b = new Audio(url);
        b.preservesPitch = b.mozPreservesPitch = b.webkitPreservesPitch = false;
        b.playbackRate = 0.9;
        var g2 = ctx.createGain(); g2.gain.value = 0.55;
        ctx.createMediaElementSource(b).connect(g2); g2.connect(verb); g2.connect(dry);
        a.echo = b;
        a.addEventListener("play", function () { b.play().catch(function () {}); });
      }
    } catch (e) { /* no effects on this browser; the plain voice still plays */ }
    return a;
  }
  function stopSpeaking() {
    if (player) { player.pause(); if (player.echo) player.echo.pause(); player = null; }
    if (window.speechSynthesis) speechSynthesis.cancel();
    if (voiceHook) voiceHook();
  }
  function voiceFor(c) {
    return { voice: c.voice || "", tone: c.voice_tone || "natural", rate: +c.voice_rate || 0, gender: c.gender,
      engine: c.voice_engine || "edge", profile: c.voice_profile || "", age: c.age, name: c.name, character_id: c.id,
      style: parseStyle(c.voice_style) };
  }
  function parseStyle(raw) {
    if (!raw) return null;
    if (typeof raw === "object") return raw.accent ? raw : null;
    try { var st = JSON.parse(raw); return st && st.accent ? st : null; } catch (e) { return null; }
  }
  // Speak text as a character's voice; resolves when it finishes.
  function speak(text, v, onEnd) {
    stopSpeaking();
    var mode = pref("readMode", "all");
    return loadVoices().then(function (info) {
      var id = v.voice || (info.defaults || {})[v.gender] || "en-US-AvaNeural";
      var studio = v.engine === "voicestudio";
      if (!studio && (id.indexOf("device:") === 0 || !info.enabled || onlineFailed)) return speakOnDevice(speakableText(text, mode), id, v, onEnd);
      return fetch("/app/api/tts", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text, voice: id, tone: v.tone, rate: v.rate, mode: mode, engine: v.engine || "edge",
          profile: v.profile || "", gender: v.gender, age: v.age, name: v.name, character_id: v.character_id, style: v.style || null }) })
        .then(function (r) {
          if (!r.ok) return r.json().catch(function () { return {}; }).then(function (d) { throw new Error(d.error || "Voice unavailable"); });
          if (studio && r.headers.get("X-Voice-Fallback") && !vsWarned) {
            vsWarned = true;
            toast(r.headers.get("X-Voice-Fallback") + " Using the online voice instead.", true);
          }
          return r.blob();
        })
        .then(function (blob) {
          var a = playAudio(blob, v.style && v.style.sound);
          player = a;
          a.onended = function () { if (player === a) player = null; if (onEnd) onEnd(); };
          return a.play();
        })
        .catch(function (e) {
          if (e.name === "NotAllowedError") { toast("Tap Listen to hear it; your browser blocked autoplay.", true); if (onEnd) onEnd(); return; }
          if (!studio) onlineFailed = true;
          toast((studio ? "Couldn't reach VoiceStudio or the online voices" : "Couldn't reach the online voices") + ", so this device's voice is reading instead.", true);
          return speakOnDevice(speakableText(text, mode), id, v, onEnd);
        });
    });
  }
  function speakOnDevice(text, id, v, onEnd) {
    if (!window.speechSynthesis || !text) { if (onEnd) onEnd(); return; }
    var list = deviceVoices(), pick = null;
    if (id.indexOf("device:") === 0) pick = list.filter(function (x) { return x.name === id.slice(7); })[0];
    if (!pick) {
      var lang = id.slice(0, 5).toLowerCase();
      var same = list.filter(function (x) { return x.lang.replace("_", "-").toLowerCase().indexOf(lang) === 0; });
      pick = same[0] || list[0];
    }
    var u = new SpeechSynthesisUtterance(text);
    if (pick) { u.voice = pick; u.lang = pick.lang; }
    u.pitch = TONE_PITCH[v.tone] || 1;
    u.rate = Math.max(0.5, Math.min(1.5, 1 + (v.rate || 0) / 100));
    u.onend = function () { if (onEnd) onEnd(); };
    speechSynthesis.speak(u);
  }
  if (window.speechSynthesis) speechSynthesis.onvoiceschanged = function () { /* list fills in later on some browsers */ };

  function voiceSection(c) {
    var vs = c.voice_engine === "voicestudio", usePreset = !!parseStyle(c.voice_style) || (!c.id && !c.name);
    return '<fieldset class="voice-box"><legend>Voice <span class="hint">how ' + esc(c.name || "they") + ' sound when replies are read aloud</span></legend>' +
      '<div class="two"><label>Engine<select name="voice_engine">' +
        '<option value="edge"' + (vs ? "" : " selected") + '>Online voices (Microsoft Edge)</option>' +
        '<option value="voicestudio"' + (vs ? " selected" : "") + '>VoiceStudio on this computer</option></select></label>' +
      '<label class="vs-only"' + (vs ? "" : " hidden") + '>VoiceStudio voice<select name="voice_profile"><option value="">Designed for ' + esc(c.name || "this character") + '</option></select></label></div>' +
      '<p class="hint vs-only" id="vsNote"' + (vs ? "" : " hidden") + '></p>' +
      '<div class="two"><label>Voice type<select name="voice_kind"><option value="preset"' + (usePreset ? " selected" : "") + '>Accent voice (accent, age, sound)</option>' +
        '<option value="country"' + (usePreset ? "" : " selected") + '>Pick by country</option></select></label><span></span></div>' +
      '<div class="preset-box" id="presetBox">' +
        '<div class="two"><label>Accent<select name="vp_accent"></select></label><label>Age<select name="vp_age"></select></label></div>' +
        '<div class="two"><label>Gender<select name="vp_gender"><option value="male">Male</option><option value="female">Female</option></select></label>' +
        '<label>Sound<select name="vp_sound"></select></label></div>' +
        '<p class="hint" id="presetNote"></p></div>' +
      '<div class="two country-only"><label>Country<select name="voice_region"></select></label>' +
      '<label><span id="voiceLbl">' + (vs ? "Backup voice" : "Voice") + '</span><select name="voice"></select></label></div>' +
      '<div class="two"><label class="country-only">Tone<select name="voice_tone">' + ["deep", "low", "natural", "bright"].map(function (t) {
        return '<option value="' + t + '"' + ((c.voice_tone || "natural") === t ? " selected" : "") + ">" + t.charAt(0).toUpperCase() + t.slice(1) + "</option>"; }).join("") + "</select></label>" +
      '<label>Speed <span class="hint" id="rateShow"></span><input type="range" name="voice_rate" min="-30" max="30" step="5" value="' + (+c.voice_rate || 0) + '"></label></div>' +
      '<div class="row"><button type="button" class="btn small" id="voicePreview">▶ Preview voice</button><span class="hint" id="voiceNote"></span></div></fieldset>';
  }
  function formStyle(form) {
    return form.voice_kind && form.voice_kind.value === "preset" && form.vp_accent.value
      ? { accent: form.vp_accent.value, age: form.vp_age.value, gender: form.vp_gender.value, sound: form.vp_sound.value } : null;
  }
  function wireVoiceSection(form, c) {
    loadVoices().then(function (info) {
      var regionSel = form.voice_region, voiceSel = form.voice;
      var current = c.voice || (info.defaults || {})[form.gender.value] || "en-US-AvaNeural";
      var curRegion = current.indexOf("device:") === 0 ? (deviceVoices().filter(function (v) { return v.name === current.slice(7); })[0] || { lang: "en-US" }).lang.replace("_", "-").slice(0, 5) : current.slice(0, 5);
      function regionOpts(list) {
        return list.map(function (r) {
          return '<option value="' + r[0] + '"' + (r[0] === curRegion ? " selected" : "") + ">" + esc(r[1]) + "</option>"; }).join("");
      }
      var eng = info.regions.filter(function (r) { return r[0].indexOf("en-") === 0; });
      var eur = info.regions.filter(function (r) { return r[0].indexOf("en-") !== 0; });
      regionSel.innerHTML = '<optgroup label="English-speaking countries">' + regionOpts(eng) + "</optgroup>" +
        (eur.length ? '<optgroup label="Europe (English with that accent)">' + regionOpts(eur) + "</optgroup>" : "");
      function fill() {
        var reg = regionSel.value, groups = { male: [], female: [] };
        info.voices.forEach(function (v) { if (v.region === reg) groups[v.gender].push(v); });
        var dev = deviceVoices().filter(function (v) { return v.lang.replace("_", "-").toLowerCase().indexOf(reg.toLowerCase()) === 0; });
        voiceSel.innerHTML = ["female", "male"].map(function (g) {
          return groups[g].length ? '<optgroup label="' + (g === "male" ? "Male" : "Female") + '">' + groups[g].map(function (v) {
            return '<option value="' + v.id + '"' + (v.id === current ? " selected" : "") + ">" + esc(v.name) + "</option>"; }).join("") + "</optgroup>" : "";
        }).join("") + (dev.length ? '<optgroup label="On this device (works offline)">' + dev.map(function (v) {
          var val = "device:" + v.name;
          return '<option value="' + esc(val) + '"' + (val === current ? " selected" : "") + ">" + esc(v.name) + "</option>"; }).join("") + "</optgroup>" : "");
        if (voiceSel.value !== current) {  // new country: start on a voice that matches the character's gender
          var g = form.gender.value === "male" ? "male" : "female";
          voiceSel.value = (groups[g][0] || groups.female[0] || groups.male[0] || {}).id || voiceSel.value;
        }
      }
      regionSel.onchange = function () { fill(); current = voiceSel.value; };
      voiceSel.onchange = function () { current = voiceSel.value; };
      fill();
      if (!info.enabled) document.getElementById("voiceNote").textContent = "Online voices aren't installed; device voices will be used.";
      // VoiceStudio: a voice designed from gender, age, tone and country (same voice every time), or one saved in VoiceStudio.
      var vsInfo = info.voicestudio || { online: false, profiles: [], url: "" };
      var profSel = form.voice_profile, have = false;
      vsInfo.profiles.forEach(function (p) {
        if (p.id === c.voice_profile) have = true;
        profSel.insertAdjacentHTML("beforeend", '<option value="' + esc(p.id) + '">' + esc(p.name) + "</option>");
      });
      if (c.voice_profile && !have) profSel.insertAdjacentHTML("beforeend", '<option value="' + esc(c.voice_profile) + '">Saved voice (VoiceStudio is off)</option>');
      profSel.value = c.voice_profile || "";
      function showEngine() {
        var on = form.voice_engine.value === "voicestudio";
        form.querySelectorAll(".vs-only").forEach(function (el) { el.hidden = !on; });
        document.getElementById("voiceLbl").textContent = on ? "Backup voice" : "Voice";
        document.getElementById("vsNote").textContent = !vsInfo.online
          ? "VoiceStudio isn't running at " + vsInfo.url + ". Start it and reload; until then the backup voice reads."
          : profSel.value ? "Uses this voice from VoiceStudio. Country and tone don't change it; speed does."
          : "Designed from gender, age, tone and country, and the same every time. Thoughts are whispered. Accents: American, British, Canadian, Australian, Indian, Russian or Portuguese, the closest to the country. For another European accent, clone or design one in VoiceStudio and pick it above.";
      }
      form.voice_engine.onchange = function () { showEngine(); showKind(); }; profSel.onchange = showEngine; showEngine();
      // Accent voices: accent x age x gender x sound.
      var pre = info.presets || { accents: [], ages: [], sounds: [] }, st = parseStyle(c.voice_style) || {};
      function opts(list, cur) { return list.map(function (x) { return '<option value="' + x[0] + '"' + (x[0] === cur ? " selected" : "") + ">" + esc(x[1]) + "</option>"; }).join(""); }
      form.vp_accent.innerHTML = opts(pre.accents, st.accent || "irish");
      form.vp_age.innerHTML = opts(pre.ages, st.age || (+form.age.value <= 25 ? "young" : +form.age.value <= 35 ? "middle" : +form.age.value <= 60 || +form.age.value > 100 ? "older" : "elder"));
      form.vp_sound.innerHTML = opts(pre.sounds, st.sound || "mysterious");
      form.vp_gender.value = st.gender || (form.gender.value === "male" ? "male" : "female");
      function showKind() {
        var preset = form.voice_kind.value === "preset";
        document.getElementById("presetBox").hidden = !preset;
        form.querySelectorAll(".country-only").forEach(function (el) { el.hidden = preset; });
        var vsOn = form.voice_engine.value === "voicestudio" && !form.voice_profile.value;
        document.getElementById("presetNote").textContent = !preset ? "" :
          vsOn && form.vp_accent.value !== "russian" ? "VoiceStudio has no " + form.vp_accent.selectedOptions[0].textContent + " accent built in, so this preset reads with the online voices. Pick a saved VoiceStudio voice to use your own." :
          "Every accent speaks English. The sound changes the delivery and adds an effect as it plays.";
      }
      [form.voice_kind, form.vp_accent].forEach(function (el) { el.addEventListener("change", showKind); });
      showKind();
      function showRate() { var r = +form.voice_rate.value; document.getElementById("rateShow").textContent = r === 0 ? "normal" : (r > 0 ? "+" : "") + r + "%"; }
      form.voice_rate.oninput = showRate; showRate();
      document.getElementById("voicePreview").onclick = function () {
        var sample = form.greeting.value.trim() || '"' + "You found me. I wondered how long it would take." + '"';
        speak(sample, { voice: voiceSel.value, tone: form.voice_tone.value, rate: +form.voice_rate.value, gender: form.gender.value,
          engine: form.voice_engine.value, profile: form.voice_profile.value, age: form.age.value, name: form.name.value, character_id: c.id,
          style: formStyle(form) });
      };
    });
  }

  // "Where does it start?" Picks a place (and optionally what's happening) for a new scene.
  // create(extra) makes the scene with {location, situation} added and resolves to {id}.
  function pickStart(o) {
    var dlg = document.createElement("dialog");
    dlg.className = "start-dlg";
    dlg.innerHTML = '<form method="dialog" class="start-form form">' +
      '<div class="start-head"><h2><img class="brand-mark" src="/static/dreamnet/brand/mark-dark.svg" alt=""> Where does it start?</h2><button type="button" class="x" data-close title="Close">×</button></div>' +
      (o.who ? '<p class="hint">A new scene with ' + esc(o.who) + ". Pick a place or write your own; " + esc(o.who) + " opens the scene there.</p>" : "") +
      (o.resume ? '<button type="button" class="btn small ghost wide" data-resume>Continue your last scene instead</button>' : "") +
      '<input type="search" class="start-search" placeholder="Search places" aria-label="Search places">' +
      '<div class="start-groups"><p class="hint">Loading places…</p></div>' +
      '<label>Place<input name="location" maxlength="300" placeholder="Pick one above, or write your own: a rooftop in Prague, his study, a burning church…"></label>' +
      '<label>Scenario <span class="hint">optional: the setup, who you are to each other, what just happened, what\'s at stake</span>' +
        '<textarea name="situation" rows="3" maxlength="1500" placeholder="You\'re the accountant who found the second ledger. He knows, and he\'s waiting for you to say it."></textarea></label>' +
      '<div class="start-first"><span class="lbl-strong">Starting point</span><div class="seg" role="radiogroup" aria-label="Who speaks first">' +
        '<label><input type="radio" name="first" value="them" checked> ' + esc(o.who || "They") + ' speaks first</label>' +
        '<label><input type="radio" name="first" value="you"> You speak first</label></div></div>' +
      '<label><span id="openLbl">Their opening line <span class="hint">optional: leave it empty and ' + esc(o.who || "they") + ' writes it</span></span>' +
        '<textarea name="opening" rows="3" maxlength="2000" placeholder="*He sets down the glass without looking up.* &quot;You found the door. Most people don\'t.&quot;"></textarea></label>' +
      '<div class="row start-actions"><button type="button" class="btn small ghost" data-surprise>Surprise me</button>' +
      '<button type="button" class="btn small ghost" data-skip>Use their usual opening</button>' +
      '<button type="submit" class="btn primary" data-go>Start scene</button></div></form>';
    document.body.appendChild(dlg);
    dlg.showModal();
    var form = dlg.querySelector("form"), all = [];
    function close() {
      window.removeEventListener("hashchange", close);
      if (dlg.open) dlg.close();
      dlg.remove();
    }
    window.addEventListener("hashchange", close);  // leaving the page closes it
    api("/app/api/places" + (o.characterId ? "?character_id=" + o.characterId : "")).then(function (r) {
      dlg.querySelector(".start-groups").innerHTML = r.groups.map(function (g) {
        return '<div class="start-group"><h4>' + esc(g.group) + '</h4><div class="start-tiles">' + g.places.map(function (p) {
          all.push(p.place);
          return '<button type="button" class="start-tile" data-place="' + esc(p.place) + '">' + photoSpan("place", p.place) + '<span class="lbl">' + esc(p.place) + "</span></button>";
        }).join("") + "</div></div>";
      }).join("");
      loadPhotos(dlg);
    }).catch(function () { dlg.querySelector(".start-groups").innerHTML = ""; });
    function choose(place) {
      form.location.value = place;
      dlg.querySelectorAll(".start-tile").forEach(function (t) { t.classList.toggle("on", t.dataset.place === place); });
    }
    form.addEventListener("change", function (e) {
      if (e.target.name !== "first") return;
      var you = form.first.value === "you";
      document.getElementById("openLbl").innerHTML = you
        ? 'Your first message <span class="hint">' + esc(o.who || "they") + " replies to it</span>"
        : 'Their opening line <span class="hint">optional: leave it empty and ' + esc(o.who || "they") + " writes it</span>";
      form.opening.placeholder = you ? '"I know about the ledger." *You slide it across his desk.*'
        : '*He sets down the glass without looking up.* "You found the door. Most people don\'t."';
    });
    function go(extra) {
      var btn = dlg.querySelector("[data-go]");
      if (extra.first === "you" && !extra.opening) { toast("Write your first message, or let them speak first.", true); form.opening.focus(); return; }
      btn.disabled = true;
      btn.textContent = (extra.location || extra.situation) && !extra.opening ? "Writing the opening…" : "Starting…";
      o.create(extra).then(function (r) {
        if (r.awaiting_reply) { try { sessionStorage.setItem("dn.autoreply", String(r.id)); } catch (e) { /* reply on demand instead */ } }
        close(); location.hash = "#/chat/" + r.id;
      })
        .catch(function (err) { btn.disabled = false; btn.textContent = "Start scene"; toast(err.message, true); });
    }
    dlg.querySelector(".start-search").oninput = function () {
      var q = this.value.trim().toLowerCase();
      dlg.querySelectorAll(".start-tile").forEach(function (t) { t.hidden = q && t.dataset.place.toLowerCase().indexOf(q) < 0; });
      dlg.querySelectorAll(".start-group").forEach(function (g) { g.hidden = !g.querySelector(".start-tile:not([hidden])"); });
    };
    dlg.onclick = function (e) {
      if (e.target === dlg) return close();
      var b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.place) choose(b.dataset.place);
      else if (b.hasAttribute("data-close")) close();
      else if (b.hasAttribute("data-surprise") && all.length) choose(all[Math.floor(Math.random() * all.length)]);
      else if (b.hasAttribute("data-skip")) go({});
      else if (b.hasAttribute("data-resume")) { close(); location.hash = "#/chat/" + o.resume; }
    };
    dlg.addEventListener("cancel", function (e) { e.preventDefault(); close(); });
    form.onsubmit = function (e) {
      e.preventDefault();
      go({ location: form.location.value.trim(), situation: form.situation.value.trim(),
        opening: form.opening.value.trim(), first: form.first.value });
    };
  }
  function newScene(body, who, extraOpts) {
    pickStart(Object.assign({ who: who, characterId: body.character_id, create: function (extra) {
      return api("/app/api/conversations", { method: "POST", body: Object.assign({ new: true }, body, extra) });
    } }, extraOpts || {}));
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
      : "background:linear-gradient(135deg," + esc(c.color || "#F1916D") + ",#03122F)";
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
  // Line icons (no emoji anywhere in the interface).
  var ICONS = {
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    chatplus: '<path d="M4 5h16v11H9l-5 4z"/><path d="M12 8v5M9.5 10.5h5"/>',
    search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
    panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    panelr: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
    speaker: '<path d="M4 10v4h4l5 4V6L8 10z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/>',
    book: '<path d="M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h10"/>',
    trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    chat: '<path d="M4 5h16v11H9l-5 4z"/>',
    quote: '<path d="M7 7c-2 1-3 3-3 6h4v-4H6M15 7c-2 1-3 3-3 6h4v-4h-2"/>',
    hand: '<path d="M8 13V6a1.5 1.5 0 0 1 3 0v6M11 11V4.5a1.5 1.5 0 0 1 3 0V11M14 11V6a1.5 1.5 0 0 1 3 0v8a6 6 0 0 1-6 6h-1a5 5 0 0 1-4-2l-3-4a1.5 1.5 0 0 1 2.3-2L8 14"/>',
    thought: '<path d="M7 15a4 4 0 0 1-.5-8A5 5 0 0 1 16 6a4 4 0 0 1 1 9z"/><circle cx="8" cy="19" r="1.2"/><circle cx="5" cy="21" r=".6"/>',
    down: '<path d="M7 10l5 5 5-5"/>',
    image: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-9 8"/>',
    wand: '<path d="M4 20L15 9M13 7l4 4"/><path d="M18 3v3M16.5 4.5h3M20 9v2M19 10h2M9 3v2M8 4h2"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4"/>',
    send: '<path d="M4 12l16-8-6 16-3-7z"/>',
    left: '<path d="M15 6l-6 6 6 6"/>',
    right: '<path d="M9 6l6 6-6 6"/>',
    phone: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z"/>',
    more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
    media: '<rect x="3" y="7" width="14" height="13" rx="2"/><path d="M7 3h14v13"/><path d="M17 17l-4-4-7 7"/>',
    pin: '<path d="M12 21s-6-6-6-11a6 6 0 0 1 12 0c0 5-6 11-6 11z"/><circle cx="12" cy="10" r="2"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/>',
    skip: '<path d="M6 5l9 7-9 7z"/><path d="M18 5v14"/>',
    rewind: '<path d="M4 12a8 8 0 1 0 2.3-5.7M4 4v5h5"/>',
    play: '<path d="M8 5l11 7-11 7z" fill="currentColor"/>',
    pause: '<path d="M8 5v14M16 5v14" stroke-width="3"/>',
    dots: '<circle cx="6" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18" cy="12" r="1.2"/>'
  };
  function ic(name) {
    return '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || "") + "</svg>";
  }
  function shortWhen(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000), now = new Date(), y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === now.toDateString()) return clock(ts);
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function fmtSecs(s) {
    if (!s || !isFinite(s)) return "";
    s = Math.round(s); return Math.floor(s / 60) + ":" + ("0" + s % 60).slice(-2);
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
    [/^#\/character\/new$/, function () { viewGuidedCreate(); }],
    [/^#\/character\/new\/full$/, function () { viewCharacterForm(null); }],
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
          var cid = +el.getAttribute("data-chat"), ch = chars.filter(function (x) { return x.id === cid; })[0] || {};
          api("/app/api/conversations").then(function (list) {
            var last = list.filter(function (cv) { return cv.character_id === cid && !cv.novel_id; })[0];
            newScene({ character_id: cid }, ch.name, { resume: last ? last.id : null });
          });
        };
      });
    });
  }

  var TRAITS = ["possessive", "morally grey", "obsessive", "protective", "cold", "brooding", "dominant", "jealous",
    "charming", "ruthless", "tortured past", "sarcastic", "tender underneath", "dangerous", "forbidden"];

  // Guided character creation: one choice per screen, like a character creator. Everything it picks fills the
  // full form, where the AI writes the rest and anything can still be edited. Every character is an adult.
  var SWATCH = {
    skin: [["Porcelain", "#F6E3D4"], ["Fair", "#EBC8A8"], ["Golden", "#D9A577"], ["Tan", "#C08552"], ["Brown", "#8D5534"], ["Deep", "#5A3420"]],
    hair: [["Black", "#151518"], ["Dark brown", "#3f281d"], ["Chestnut", "#7b4a2c"], ["Auburn", "#8f3a20"], ["Copper red", "#c25a2c"],
      ["Blonde", "#e2c27a"], ["Platinum", "#efe6cf"], ["Silver", "#c3c4cc"], ["White", "#f4f4f4"], ["Blue-black", "#1e2a4a"]],
    eyes: [["Brown", "#5b3a22"], ["Hazel", "#8e6b3a"], ["Amber", "#c98a2b"], ["Green", "#4f7a4a"], ["Blue", "#5b8fc7"],
      ["Grey", "#9aa3ad"], ["Violet", "#8a6bb8"], ["Crimson", "#a3262f"], ["Black", "#151518"]]
  };
  var NAMES = {
    male: ["Dante", "Lucien", "Kael", "Nikolai", "Rafael", "Silas", "Damien", "Elias", "Matteo", "Viktor", "Cassian", "Rowan"],
    female: ["Valentina", "Seraphina", "Mara", "Elena", "Isolde", "Noor", "Sienna", "Vesper", "Lilith", "Ines", "Katya", "Aurelia"],
    "non-binary": ["Ash", "Riven", "Sage", "Wren", "Onyx", "Morgan", "Jude", "Ellis", "Nova", "Raven"]
  };
  var SURNAMES = ["Moretti", "Volkov", "Vale", "Draven", "Castellano", "Blackwood", "Ashford", "Kross", "Sinclair", "Delacroix", "Varga", "Thorne"];
  // Realistic photos on choice tiles, made once on the user's ComfyUI. Loaded one at a time so the
  // browser keeps connections free; without ComfyUI the tiles just show their names.
  var tilePhotos = {}, photosOff = false;
  function photoUrl(set, key, g) {
    return "/app/api/option-image?set=" + encodeURIComponent(set) + "&key=" + encodeURIComponent(key) + (g ? "&g=" + encodeURIComponent(g) : "");
  }
  function photoSpan(set, key, g) {
    var url = photoUrl(set, key, g), have = tilePhotos[url];
    if (photosOff && !have) return "";
    return '<span class="ph' + (have ? " ready" : "") + '" data-photo="' + esc(url) + '"' + (have ? ' style="background-image:url(' + have + ')"' : "") + "></span>";
  }
  var photoQueue = Promise.resolve();
  function loadPhotos(root) {
    var spans = [].slice.call(root.querySelectorAll(".ph[data-photo]:not(.ready)"));
    spans.forEach(function (sp) {
      photoQueue = photoQueue.then(function () {
        var url = sp.dataset.photo;
        if (tilePhotos[url]) { sp.style.backgroundImage = "url(" + tilePhotos[url] + ")"; sp.classList.add("ready"); return; }
        if (photosOff || !document.body.contains(sp)) return;
        return fetch(url, { credentials: "same-origin" }).then(function (r) {
          if (r.status === 503) { photosOff = true; document.querySelectorAll(".ph:not(.ready)").forEach(function (x) { x.remove(); }); return; }
          if (!r.ok) return;
          return r.blob().then(function (b) {
            tilePhotos[url] = URL.createObjectURL(b);
            document.querySelectorAll('.ph[data-photo="' + url.replace(/"/g, '\\"') + '"]').forEach(function (x) {
              x.style.backgroundImage = "url(" + tilePhotos[url] + ")"; x.classList.add("ready"); });
          });
        }).catch(function () {});
      });
    });
  }
  function wizardSteps() {
    var t = function (list) { return list.map(function (x) { return { v: x }; }); };
    return [
      { key: "gender", title: "Who are they?", pick: "one", need: true, items: t(["female", "male", "non-binary"]) },
      { key: "kind", title: "What are they?", pick: "one", need: true, note: "Supernatural is welcome; they still look like a real person.",
        items: t(["Human", "Vampire", "Elf", "Demon", "Fae", "Werewolf", "Witch or warlock", "Fallen angel"]),
        extra: { key: "heritage", title: "Heritage", pick: "one", items: t(["East Asian", "Black", "White", "Latino or Latina",
          "Arab", "South Asian", "Mediterranean", "Slavic", "Mixed"]) },
        swatch: { key: "skin", title: "Skin tone" } },
      { key: "hair_style", title: "Hair and eyes", pick: "one", need: true,
        items: t(["Long and straight", "Long and wavy", "Curly", "Braided", "Ponytail", "Bun",
          "Short", "Pixie cut", "Buzzed", "Slicked back", "Messy", "Locs"]),
        swatch: { key: "hair", title: "Hair colour" }, swatch2: { key: "eyes", title: "Eye colour" } },
      { key: "build", title: "Body", pick: "one", need: true,
        items: t(["Slim", "Lean", "Athletic", "Curvy", "Voluptuous", "Muscular", "Broad-shouldered", "Full-figured"]),
        extra: { key: "height", title: "Height", pick: "one", items: t(["Petite", "Average height", "Tall", "Towering"]) } },
      { key: "name", title: "Name, age and voice", form: true },
      { key: "personality", title: "Personality", pick: 3, need: true, note: "Pick up to three.",
        items: t(["Seductive", "Shy", "Sweet", "Playful", "Passionate", "Adventurous", "Confident",
          "Charming", "Witty", "Dominant", "Submissive", "Mischievous", "Caring", "Sassy",
          "Tsundere", "Yandere", "Intense", "Mysterious", "Intellectual", "Possessive",
          "Cold", "Brooding", "Ruthless", "Protective"]) },
      { key: "job", title: "Occupation", pick: "one", custom: true,
        items: t(["Mafia boss", "Bratva enforcer", "Assassin", "Bodyguard", "Billionaire CEO", "Detective",
          "Doctor", "Nurse", "Lawyer", "Artist", "Bartender", "Tattoo artist", "Musician",
          "Model", "Dancer", "Dominatrix", "Professor", "Chef", "Soldier", "Hacker",
          "Thief", "Street racer", "Royal heir", "Priest or priestess", "Surgeon", "Journalist"]) },
      { key: "relationship", title: "Who are they to you?", pick: "one", custom: true,
        items: t(["Stranger", "Lover", "Crush", "Ex", "Rival", "Boss", "Colleague", "Neighbor",
          "Roommate", "Your bodyguard", "Captor", "Arranged marriage", "Fake dating", "Enemy",
          "Secret admirer", "Friend", "Mentor", "Owes you a debt", "You owe them"]) },
      { key: "hobbies", title: "Hobbies", pick: 3, note: "Optional. Pick up to three.", custom: true,
        items: t(["Gaming", "Photography", "Yoga", "Reading", "Singing", "Baking", "Martial arts",
          "Dancing", "Cooking", "Cosplay", "Weightlifting", "Writing", "Gardening", "Painting",
          "Swimming", "Hiking", "Playing guitar", "Running", "Rock climbing", "Chess", "Wine",
          "Motorcycles", "Shooting", "Poker"]) },
      { key: "turn_ons", title: "Turn-ons", pick: 4, note: "Optional. Pick up to four; only between consenting adults.", custom: true,
        items: t(["Lingerie", "Roleplay", "High heels", "Stockings", "Being in control", "Giving up control",
          "Spanking", "Collars", "Latex", "Slow and tender", "Praise", "Teasing", "Jealousy",
          "Restraints", "Being watched", "Muscle worship", "Feet", "Vanilla"]) }
    ];
  }
  function viewGuidedCreate() {
    setNav("characters");
    var steps = wizardSteps(), at = 0, picks = { age: 25, sound: "mysterious", accent: "" };
    function chosen(key) { var v = picks[key]; return Array.isArray(v) ? v : v ? [v] : []; }
    function tiles(st) {
      return '<div class="wiz-tiles">' + st.items.map(function (it) {
        var on = chosen(st.key).indexOf(it.v) >= 0;
        return '<button type="button" class="wiz-tile' + (on ? " on" : "") + '" data-k="' + st.key + '" data-v="' + esc(it.v) + '">' +
          photoSpan(st.key, it.v, st.key === "gender" ? "" : picks.gender) + '<span class="lbl">' + esc(it.v.charAt(0).toUpperCase() + it.v.slice(1)) + "</span></button>";
      }).join("") + "</div>";
    }
    function swatches(sw) {
      return '<h4 class="wiz-sub">' + sw.title + '</h4><div class="wiz-swatches">' + SWATCH[sw.key].map(function (x) {
        return '<button type="button" class="wiz-swatch' + (picks[sw.key] === x[0] ? " on" : "") + '" data-k="' + sw.key + '" data-v="' + x[0] + '" title="' + x[0] + '" style="--sw:' + x[1] + '"><span></span><small>' + x[0] + "</small></button>";
      }).join("") + "</div>";
    }
    function ready(st) {
      if (st.form) return (picks.name || "").trim() && +picks.age >= 18;
      return !st.need || chosen(st.key).length;
    }
    function draw() {
      var st = steps[at];
      var body;
      if (st.form) {
        var acc = [["", "Pick later"], ["irish", "Irish"], ["french", "French"], ["german", "German"], ["nordic", "Nordic"], ["italian", "Italian"], ["russian", "Russian"]];
        var snd = [["innocent", "Innocent"], ["mysterious", "Mysterious"], ["villain", "Villain"], ["possessed", "Possessed"]];
        body = '<div class="wiz-name"><label>Character name<div class="row"><input id="wName" maxlength="60" value="' + esc(picks.name || "") + '" placeholder="Name">' +
          '<button type="button" class="btn small ghost" id="wRandom" title="Random name">Random</button></div></label>' +
          '<label>Age <span class="hint">18 or older</span><div class="wiz-age"><button type="button" class="btn small ghost" data-age="-1">‹</button>' +
          '<input id="wAge" type="number" min="18" max="9999" value="' + picks.age + '"><button type="button" class="btn small ghost" data-age="1">›</button></div></label></div>' +
          '<h4 class="wiz-sub">Voice accent <span class="hint">every accent speaks English</span></h4><div class="choices">' + acc.map(function (a) {
            return '<button type="button" class="chip' + (picks.accent === a[0] ? " on" : "") + '" data-k="accent" data-v="' + a[0] + '">' + a[1] + "</button>"; }).join("") + "</div>" +
          '<h4 class="wiz-sub">Voice sound</h4><div class="choices">' + snd.map(function (a) {
            return '<button type="button" class="chip' + (picks.sound === a[0] ? " on" : "") + '" data-k="sound" data-v="' + a[0] + '">' + a[1] + "</button>"; }).join("") + "</div>";
      } else {
        body = (st.note ? '<p class="hint">' + st.note + "</p>" : "") +
          (st.items.length > 12 ? '<input type="search" class="wiz-search" placeholder="Search" aria-label="Search">' : "") +
          tiles(st) +
          (st.custom ? '<label class="wiz-custom">Custom <span class="hint">write your own</span><input data-custom="' + st.key + '" maxlength="120" value="' + esc(picks[st.key + "_custom"] || "") + '"></label>' : "") +
          (st.extra ? '<h4 class="wiz-sub">' + st.extra.title + "</h4>" + tiles(st.extra) : "") +
          (st.swatch ? swatches(st.swatch) : "") + (st.swatch2 ? swatches(st.swatch2) : "");
      }
      view.innerHTML = '<div class="wizard panel form">' +
        '<div class="wiz-top"><span class="hint"><img class="brand-mark" src="/static/dreamnet/brand/mark-dark.svg" alt=""> Step ' + (at + 1) + " of " + steps.length + '</span><a class="hint" href="#/character/new/full">Skip to the full form</a></div>' +
        '<div class="wiz-dots">' + steps.map(function (s2, i) { return '<span class="' + (i < at ? "done" : i === at ? "now" : "") + '"></span>'; }).join("") + "</div>" +
        "<h2>" + esc(st.title) + "</h2>" + body +
        '<div class="row wiz-nav">' + (at ? '<button type="button" class="btn ghost" id="wBack">← Back</button>' : "") +
        '<button type="button" class="btn primary" id="wNext"' + (ready(st) ? "" : " disabled") + ">" + (at === steps.length - 1 ? "Create character →" : "Next →") + "</button></div></div>";
      wire(st);
      loadPhotos(view);
    }
    function wire(st) {
      var w = view.querySelector(".wizard"), next = document.getElementById("wNext");
      w.onclick = function (e) {
        var b = e.target.closest("button");
        if (!b) return;
        if (b.dataset.k) {
          var k = b.dataset.k, v = b.dataset.v, owner = steps[at].key === k ? steps[at] : (steps[at].extra && steps[at].extra.key === k ? steps[at].extra : null);
          if (owner && typeof owner.pick === "number") {
            var cur = chosen(k), i = cur.indexOf(v);
            if (i >= 0) cur.splice(i, 1); else if (cur.length < owner.pick) cur.push(v); else { toast("Up to " + owner.pick + ". Unpick one first."); return; }
            picks[k] = cur;
          } else { picks[k] = picks[k] === v && !(steps[at].form) ? "" : v; }
          if (st.form) { picks.name = document.getElementById("wName").value; picks.age = +document.getElementById("wAge").value || 18; }
          draw();
          if (owner && owner.pick === "one" && k === st.key && !st.extra && !st.swatch && !st.custom && ready(st)) { at++; draw(); }  // single choice: move on
        } else if (b.id === "wBack") { at--; draw(); }
        else if (b.id === "wNext") {
          if (at < steps.length - 1) { at++; draw(); } else finish();
        } else if (b.id === "wRandom") {
          var g = NAMES[picks.gender] || NAMES.female;
          document.getElementById("wName").value = g[Math.floor(Math.random() * g.length)] + " " + SURNAMES[Math.floor(Math.random() * SURNAMES.length)];
          picks.name = document.getElementById("wName").value; next.disabled = !ready(st);
        } else if (b.dataset.age) {
          var a = document.getElementById("wAge"); a.value = Math.max(18, (+a.value || 18) + +b.dataset.age); picks.age = +a.value;
        }
      };
      w.oninput = function (e) {
        if (e.target.id === "wName") picks.name = e.target.value;
        if (e.target.id === "wAge") picks.age = +e.target.value;
        if (e.target.dataset.custom) picks[e.target.dataset.custom + "_custom"] = e.target.value;
        if (e.target.classList.contains("wiz-search")) {
          var q = e.target.value.trim().toLowerCase();
          w.querySelectorAll(".wiz-tiles:first-of-type .wiz-tile").forEach(function (t) { t.hidden = q && t.dataset.v.toLowerCase().indexOf(q) < 0; });
        }
        next.disabled = !ready(st);
      };
    }
    function list(key) {
      var v = chosen(key).slice(), c = (picks[key + "_custom"] || "").trim();
      if (c) v.push(c);
      return v;
    }
    function finish() {
      if (+picks.age < 18) { toast("Characters must be 18 or older.", true); return; }
      var g = picks.gender || "female", kind = picks.kind && picks.kind !== "Human" ? picks.kind.toLowerCase() : "";
      var look = [picks.height, picks.build ? picks.build.toLowerCase() + " build" : ""].filter(Boolean).join(", ");
      var appearance = [
        (picks.heritage ? picks.heritage + " " : "") + (kind ? kind : g === "male" ? "man" : g === "female" ? "woman" : "person") + (look ? ", " + look.toLowerCase() : "") + ".",
        picks.skin ? picks.skin + " skin." : "",
        picks.hair || picks.hair_style ? (picks.hair ? picks.hair + " hair" : "Hair") + (picks.hair_style ? ", " + picks.hair_style.toLowerCase() : "") + "." : "",
        picks.eyes ? picks.eyes + " eyes." : ""
      ].filter(Boolean).join(" ");
      appearance = appearance.charAt(0).toUpperCase() + appearance.slice(1);
      var pers = list("personality"), job = list("job")[0] || "", rel = list("relationship")[0] || "";
      var P = function (v) { return { value: v, secret: false }; }, profile = {};
      if (look) profile.height_build = P(look);
      if (picks.skin) profile.skin = P(picks.skin + (picks.heritage ? ", " + picks.heritage : ""));
      if (picks.hair || picks.hair_style) profile.hair = P([picks.hair, picks.hair_style].filter(Boolean).join(", ").toLowerCase());
      if (picks.eyes) profile.eyes = P(picks.eyes);
      if (kind) profile.species = P(picks.kind);
      if (pers.length) profile.temperament = P(pers.join(", "));
      if (job) profile.job = P(job);
      if (rel) profile.relationship = P(rel);
      if (list("hobbies").length) profile.likes = P(list("hobbies").join(", "));
      if (list("turn_ons").length) profile.turn_ons = P(list("turn_ons").join(", "));
      var age = +picks.age, ageGroup = age <= 25 ? "young" : age <= 35 ? "middle" : age <= 60 || age > 100 ? "older" : "elder";
      var c = {
        name: picks.name.trim(), age: age, gender: g, tagline: "", appearance: appearance,
        persona: picks.name.trim() + " is " + (pers.length ? pers.join(", ").toLowerCase() : "hard to read") + "." +
          (job ? " Works as: " + job + "." : "") + (rel ? " To you: " + rel + "." : ""),
        greeting: "", tags: pers.concat(job ? [job] : []).join(", ").toLowerCase(), color: "#F1916D", avatar: "", profile: profile,
        voice_style: picks.accent ? JSON.stringify({ accent: picks.accent, age: ageGroup, gender: g === "male" ? "male" : "female", sound: picks.sound }) : ""
      };
      var idea = "A " + (pers.join(", ").toLowerCase() || "") + " " + (kind || (g === "male" ? "man" : g === "female" ? "woman" : "person")) +
        (job ? ", " + job.toLowerCase() : "") + (rel ? ", who is " + rel.toLowerCase() + " to the user" : "") + ".";
      viewCharacterForm(null, { c: c, idea: idea });
    }
    draw();
  }

  function viewCharacterForm(id, guided) {
    setNav("characters");
    var load = Promise.all([id ? api("/app/api/characters/" + id) : Promise.resolve(guided ? guided.c : {
      name: "", age: 30, gender: "male", tagline: "", appearance: "", persona: "", greeting: "", tags: "", color: "#F1916D", avatar: "", profile: {},
    }), api("/app/api/profile-fields")]);
    load.then(function (r) {
      var c = r[0], sections = r[1], prof = c.profile || {};
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
        '<div class="ai-box"><div class="ai-head"><b>✨ Build with AI</b><span class="hint">or fill everything in yourself below; you can mix both</span></div>' +
        '<textarea id="aiIdea" rows="2" placeholder="Your idea, e.g. a possessive vampire art dealer in Prague who collects debts in blood"></textarea>' +
        '<div class="row"><button type="button" class="btn small primary" data-ai="empty">Fill empty fields</button>' +
        '<button type="button" class="btn small" data-ai="all">Write everything from the idea</button><span class="hint" id="aiStatus"></span></div></div>' +
        '<label><span class="lbl">Tagline ' + aiBtn("tagline") + '</span><input name="tagline" maxlength="160" placeholder="Mafia heir who never asks twice" value="' + esc(c.tagline) + '"></label>' +
        '<label>Traits <span class="hint">click to add to the personality</span><div class="choices">' +
        TRAITS.map(function (t) { return '<button type="button" class="chip" data-trait="' + t + '">' + t + "</button>"; }).join("") + "</div></label>" +
        '<label><span class="lbl">Personality and backstory ' + aiBtn("persona") + '</span><textarea name="persona" rows="5" required placeholder="Who they are, what they want, what they hide, how they talk.">' + esc(c.persona) + "</textarea></label>" +
        '<label><span class="lbl">Appearance ' + aiBtn("appearance") + '</span><textarea name="appearance" rows="3" placeholder="Lifelike detail, even for supernatural characters: skin, hair, eyes, build, scars, clothes.">' + esc(c.appearance) + "</textarea></label>" +
        profileSection(sections, prof) +
        '<label><span class="lbl">Opening line ' + aiBtn("greeting") + ' <span class="hint">how they greet you in a new scene: &quot;words&quot;, *actions*, `thoughts`</span></span><textarea name="greeting" rows="3">' + esc(c.greeting) + "</textarea></label>" +
        '<div class="two"><label>Tags <span class="hint">comma separated</span><input name="tags" value="' + esc(c.tags) + '"></label>' +
        '<label>Portrait URL <span class="hint">optional, a photo-realistic image</span><input name="avatar" value="' + esc(c.avatar) + '"></label></div>' +
        portraitSection(id, c) +
        voiceSection(c) +
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
      wireVoiceSection(form, c);
      if (guided) {  // came from the guided creator: let the AI write what the picks didn't cover
        document.getElementById("aiIdea").value = guided.idea;
        setTimeout(function () { var b = form.querySelector('[data-ai="empty"]'); if (b) b.click(); }, 0);
      }
      form.addEventListener("click", function (e) {
        var lock = e.target.closest("[data-secret]");
        if (lock) { setSecret(lock, lock.dataset.secret !== "1"); return; }
        var b = e.target.closest("[data-ai]");
        if (!b) return;
        e.preventDefault();
        var scope = b.dataset.ai, status = document.getElementById("aiStatus");
        if (scope === "all" && !document.getElementById("aiIdea").value.trim() && !confirm("Rewrite every field from scratch? What you've written will be replaced.")) return;
        var body = { character: {}, profile: collectProfile(form), idea: document.getElementById("aiIdea").value, scope: scope };
        ["name", "age", "gender", "tagline", "appearance", "persona", "greeting", "tags"].forEach(function (k) { body.character[k] = form[k].value; });
        if (scope === "all" || scope === "empty") status.textContent = "Writing the character…";
        form.querySelectorAll("[data-ai]").forEach(function (x) { x.disabled = true; });
        b.classList.add("busy");
        api("/app/api/characters/ai-fill", { method: "POST", body: body }).then(function (res) {
          Object.keys(res.character).forEach(function (k) { if (form[k]) form[k].value = res.character[k]; });
          Object.keys(res.profile).forEach(function (k) {
            var row = form.querySelector('[data-prow="' + k + '"]');
            if (!row) return;
            row.querySelector("textarea").value = res.profile[k].value;
            setSecret(row.querySelector("[data-secret]"), res.profile[k].secret);
            var sec = row.closest("details"); if (sec) sec.open = true;
          });
          var n = Object.keys(res.character).length + Object.keys(res.profile).length;
          status.textContent = n ? "Filled " + n + " field" + (n === 1 ? "" : "s") + ". Edit anything, then save." : "Nothing was empty.";
          preview();
        }).catch(function (err) { status.textContent = err.message; toast(err.message, true); })
          .finally(function () { form.querySelectorAll("[data-ai]").forEach(function (x) { x.disabled = false; }); b.classList.remove("busy"); });
      });
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
        ["name", "age", "gender", "color", "tagline", "persona", "appearance", "greeting", "tags", "avatar", "voice", "voice_tone", "voice_rate", "voice_engine", "voice_profile"].forEach(function (k) { body[k] = form[k].value; });
        body.voice_style = formStyle(form) || "";
        body.profile = collectProfile(form);
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
        ["name", "age", "gender", "color", "tagline", "persona", "appearance", "greeting", "tags", "avatar", "voice", "voice_tone", "voice_rate", "voice_engine", "voice_profile"].forEach(function (k) { body[k] = form[k].value; });
        body.voice_style = formStyle(form) || "";
        body.profile = collectProfile(form);
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

  // ---------- character profile ----------
  function aiBtn(key) {
    return '<button type="button" class="ai-one" data-ai="' + key + '" title="Write this with AI">✨</button>';
  }
  function lockBtn(secret) {
    return '<button type="button" class="lock' + (secret ? " on" : "") + '" data-secret="' + (secret ? "1" : "0") + '" title="Secret: true, but they hide it. Public: you know or can see it.">' +
      (secret ? "🔒 Secret" : "👁 Public") + "</button>";
  }
  function setSecret(btn, on) {
    btn.dataset.secret = on ? "1" : "0";
    btn.classList.toggle("on", !!on);
    btn.textContent = on ? "🔒 Secret" : "👁 Public";
  }
  function profileSection(sections, prof) {
    return '<div class="profile-box"><h3>Profile <span class="hint">everything that makes them real. Mark each detail public, or secret: true, but they hide it until the story earns it</span></h3>' +
      sections.map(function (sec, si) {
        var filled = sec.fields.filter(function (f) { return prof[f.key]; }).length;
        return '<details' + (si === 0 || filled ? " open" : "") + '><summary>' + esc(sec.section) +
          ' <span class="hint">' + filled + " of " + sec.fields.length + "</span></summary>" +
          sec.fields.map(function (f) {
            var v = prof[f.key] || { value: "", secret: false };
            return '<div class="prow" data-prow="' + f.key + '"><div class="prow-head"><span>' + esc(f.label) + "</span>" + aiBtn(f.key) + lockBtn(v.secret) + "</div>" +
              '<textarea rows="2" data-pkey="' + f.key + '" placeholder="' + esc(f.hint) + '">' + esc(v.value) + "</textarea></div>";
          }).join("") + "</details>";
      }).join("") + "</div>";
  }
  function collectProfile(form) {
    var out = {};
    form.querySelectorAll("[data-prow]").forEach(function (row) {
      var v = row.querySelector("textarea").value.trim();
      if (v) out[row.dataset.prow] = { value: v, secret: row.querySelector("[data-secret]").dataset.secret === "1" };
    });
    return out;
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
    cleanup = function () { if (ctrl) ctrl.abort(); stopSpeaking(); };
    Promise.all([api("/app/api/conversations"), convId ? api("/app/api/conversations/" + convId) : null,
      api("/app/api/storylines"), api("/app/api/characters")]).then(function (r) {
      var convs = r[0], conv = r[1], stories = r[2], chars = r[3];
      if (!conv && convs.length) { location.replace("#/chat/" + convs[0].id); return; }
      var charOptions = chars.map(function (ch) { return '<option value="' + ch.id + '">' + esc(ch.name) + "</option>"; }).join("");
      // Left: your chats, newest first, with search; storylines on their own tab.
      var stSide = stories.map(function (st) {
        var mine = convs.filter(function (cv) { return cv.storyline_id === st.id; });
        var open = conv && conv.storyline && conv.storyline.id === st.id;
        return '<div class="story' + (open ? " open" : "") + '" data-story="' + st.id + '">' +
          '<div class="story-head"><b title="' + esc(st.premise) + '">' + esc(st.title) + "</b>" +
          '<button type="button" data-editstory="' + st.id + '" title="Edit storyline">' + ic("edit") + "</button>" +
          '<button type="button" data-addscene="' + st.id + '" title="New scene in this storyline">' + ic("plus") + "</button></div>" +
          '<form class="story-form" data-sceneform="' + st.id + '" hidden><select name="character">' + charOptions + "</select>" +
          '<button class="btn small primary">Start scene</button></form>' +
          '<form class="story-form" data-editform="' + st.id + '" hidden><input name="title" maxlength="120" value="' + esc(st.title) + '">' +
          '<textarea name="premise" rows="3" placeholder="Premise">' + esc(st.premise) + "</textarea>" +
          '<div class="row"><button class="btn small primary">Save</button><button type="button" class="btn small ghost danger" data-delstory="' + st.id + '">Delete storyline</button></div></form>' +
          mine.map(function (cv) { return convLink(cv, cv.id === convId); }).join("") + "</div>";
      }).join("") +
        (stories.length ? "" : '<p class="muted pad">No storylines yet.</p>') +
        '<button type="button" class="btn small wide" id="newStoryBtn">+ New storyline</button>' +
        '<form class="story-form" id="newStory" hidden><input name="title" maxlength="120" placeholder="Title, e.g. The Volkov debt" required>' +
        '<textarea name="premise" rows="3" placeholder="Premise (optional): who you are, where it starts, what\'s at stake"></textarea>' +
        '<label>First character<select name="character">' + charOptions + "</select></label>" +
        '<label>Story starts<input type="datetime-local" name="start"></label>' +
        '<button class="btn small primary">Start storyline</button></form>';
      var tab = pref("rpTab", "chats");
      var side = '<aside class="sidebar rp-list">' +
        '<div class="rp-list-head"><button type="button" class="icon-btn side-toggle side-close" title="Close">' + ic("x") + "</button>" +
        '<h3>Chats</h3><a class="btn small light" href="#/characters">' + ic("chatplus") + " New chat</a></div>" +
        '<div class="rp-tabs" role="tablist"><button type="button" data-tab="chats"' + (tab === "chats" ? ' class="on"' : "") + ">Scenes</button>" +
        '<button type="button" data-tab="stories"' + (tab === "stories" ? ' class="on"' : "") + ">Storylines</button></div>" +
        '<div class="rp-chats"' + (tab === "chats" ? "" : " hidden") + '><label class="rp-search">' + ic("search") +
        '<input type="search" id="chatSearch" placeholder="Search" aria-label="Search chats"></label>' +
        (convs.map(function (cv) {
          var snip = stripTimeTags(cv.last || "").replace(/[*`_]/g, "");
          return '<a class="rp-row' + (cv.id === convId ? " active" : "") + '" href="#/chat/' + cv.id + '" data-q="' +
            esc((cv.name + " " + (cv.storyline_title || "") + " " + (cv.novel_title || "") + " " + snip).toLowerCase()) + '">' + avatar(cv) +
            '<div class="meta"><b>' + esc(cv.name) + "</b>" + (cv.storyline_title ? "<small>" + esc(cv.storyline_title) + "</small>" : "") +
            "<span>" + esc(snip) + "</span></div><time>" + esc(shortWhen(cv.updated_at)) + "</time></a>";
        }).join("") || '<p class="muted pad">No scenes yet. Pick a character to start one.</p>') + "</div>" +
        '<div class="rp-stories"' + (tab === "stories" ? "" : " hidden") + ">" + stSide + "</div></aside>";
      function wireSidebar() {
        var sb = view.querySelector(".sidebar");
        var search = document.getElementById("chatSearch");
        if (search) search.oninput = function () {
          var q = search.value.trim().toLowerCase();
          sb.querySelectorAll(".rp-row").forEach(function (a) { a.hidden = q && a.dataset.q.indexOf(q) < 0; });
        };
        sb.onclick = function (e) {
          var b = e.target.closest("button");
          if (!b) return;
          if (b.dataset.tab) {
            setPref("rpTab", b.dataset.tab);
            sb.querySelectorAll("[data-tab]").forEach(function (x) { x.classList.toggle("on", x === b); });
            sb.querySelector(".rp-chats").hidden = b.dataset.tab !== "chats";
            sb.querySelector(".rp-stories").hidden = b.dataset.tab !== "stories";
          }
          else if (b.classList.contains("side-close")) { view.querySelector(".chat-layout").classList.remove("show-side"); }
          else if (b.id === "newStoryBtn") { document.getElementById("newStory").hidden = !document.getElementById("newStory").hidden; }
          else if (b.dataset.addscene) { var f = sb.querySelector('[data-sceneform="' + b.dataset.addscene + '"]'); f.hidden = !f.hidden; }
          else if (b.dataset.editstory) { var g = sb.querySelector('[data-editform="' + b.dataset.editstory + '"]'); g.hidden = !g.hidden; }
          else if (b.dataset.delstory) {
            if (!confirm("Delete this storyline, all its scenes and everything learned in it? This can't be undone.")) return;
            api("/app/api/storylines/" + b.dataset.delstory, { method: "DELETE" }).then(function () { location.hash = "#/chats"; route(); });
          }
        };
        sb.onsubmit = function (e) {
          e.preventDefault();
          var f = e.target;
          if (f.id === "newStory") {
            var made = null;  // the storyline is only created once a start is picked
            pickStart({ who: f.character.selectedOptions[0].textContent, characterId: +f.character.value, create: function (extra) {
              return (made ? Promise.resolve(made) : api("/app/api/storylines", { method: "POST", body: { title: f.title.value, premise: f.premise.value } }))
                .then(function (st) {
                  made = st;
                  return api("/app/api/conversations", { method: "POST", body: Object.assign({ character_id: +f.character.value, storyline_id: st.id, new: true, story_start: f.start.value || null }, extra) });
                });
            } });
          } else if (f.dataset.sceneform) {
            newScene({ character_id: +f.character.value, storyline_id: +f.dataset.sceneform }, f.character.selectedOptions[0].textContent);
          } else if (f.dataset.editform) {
            api("/app/api/storylines/" + f.dataset.editform, { method: "PUT", body: { title: f.title.value, premise: f.premise.value } })
              .then(function () { toast("Storyline saved"); route(); })
              .catch(function (err) { toast(err.message, true); });
          }
        };
      }
      if (!conv) {
        view.innerHTML = '<div class="chat-layout rp">' + side + '<section class="chat rp-chat"><div class="empty"><button type="button" class="btn small ghost side-toggle" onclick="this.closest(\'.chat-layout\').classList.toggle(\'show-side\')">' + ic("panel") + ' Chats</button><br>Start a storyline, or pick a character to begin.<br><br><a class="btn primary" href="#/characters">Choose a character</a></div></section></div>';
        wireSidebar();
        return;
      }
      var c = conv.character;
      var wide = window.matchMedia("(min-width: 1280px)").matches;
      var panelOpen = wide && pref("rpPanel", "1") === "1";
      var modes = { free: ["chat", "As typed"], say: ["quote", "Say it"], act: ["hand", "Do it"], think: ["thought", "Think it"] };
      var mode = modes[pref("rpMode", "free")] ? pref("rpMode", "free") : "free";
      var Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
      view.innerHTML = '<div class="chat-layout rp' + (panelOpen ? " panel-open" : "") + '">' + side +
        '<section class="chat rp-chat"' + (c.avatar ? ' style="--rp-bg:url(\'' + esc(c.avatar) + '\')"' : "") + ">" +
        '<div class="chat-head rp-head"><button type="button" class="icon-btn side-toggle" id="sideBtn" title="Chats and storylines">' + ic("panel") + "</button>" +
        '<div class="rp-who-mini">' + avatar(c) + "<div><b>" + esc(c.name) + "</b><p>" +
        (conv.storyline ? esc(conv.storyline.title) : esc(c.tagline)) +
        (conv.novel ? " · scene for <a href=\"#/novel/" + conv.novel.id + "\">" + esc(conv.novel.title) + "</a>" : "") + "</p></div></div>" +
        '<button class="btn small clock" id="clockBtn" title="In-story time. Set the clock or skip ahead.">' + ic("clock") + ' <span id="clockNow"></span></button>' +
        '<span class="grow"></span>' +
        '<button type="button" class="icon-btn" id="voiceBtn" title="Read aloud settings">' + ic("speaker") + "</button>" +
        '<button type="button" class="icon-btn" id="memBtn" title="What ' + esc(c.name) + ' remembers">' + ic("book") + "</button>" +
        '<button type="button" class="icon-btn" id="delConv" title="Delete this scene">' + ic("trash") + "</button>" +
        '<button type="button" class="icon-btn" id="panelBtn" title="' + esc(c.name) + '\'s panel">' + ic("panelr") + "</button></div>" +
        '<div class="memory-panel" id="memPanel" hidden></div>' +
        '<div class="voice-panel" id="voicePanel" hidden>' +
        '<label class="check-line"><input type="checkbox" id="autoRead"> Read ' + esc(c.name) + "'s replies aloud as they arrive</label>" +
        '<label>Read<select id="readMode"><option value="all">everything (words, actions, thoughts)</option>' +
        '<option value="words_thoughts">spoken words and thoughts</option><option value="words">spoken words only</option></select></label>' +
        '<a class="btn small ghost" href="#/character/' + c.id + '">Change ' + esc(c.name) + "'s voice</a></div>" +
        '<form class="clock-panel" id="clockPanel" hidden>' +
        '<label>Set the clock to<input type="datetime-local" id="clockSet"></label>' +
        '<label>or skip ahead<span class="row"><input type="number" id="skipH" min="0" max="672" placeholder="0"> h ' +
        '<input type="number" id="skipM" min="0" max="59" placeholder="0"> min</span></label>' +
        '<label class="grow">Label (optional)<input id="clockLabel" maxlength="120" placeholder="The next morning, his penthouse"></label>' +
        '<button class="btn small primary">Mark time</button><a class="btn small ghost" href="#/time-rules">Time rules</a>' +
        '<div class="scene-rules"><h4>Time rules for this scene <span class="hint">add to the main table; same name replaces it</span></h4>' +
        '<div id="sceneRules"></div><button type="button" class="btn small" id="srAdd">+ Add rule</button> ' +
        '<button type="button" class="btn small" id="srSave">Save scene rules</button></div></form>' +
        '<div class="messages" id="msgs"></div>' +
        '<form class="composer rp-composer" id="composer"><textarea id="input" rows="1" placeholder="Send a message" aria-label="Your message"></textarea>' +
        '<div class="rp-bar"><div class="rp-pop"><button type="button" class="rp-mode" id="modeBtn" title="How your message is sent">' +
          '<span id="modeIc">' + ic(modes[mode][0]) + '</span><span id="modeLbl">' + modes[mode][1] + "</span>" + ic("down") + "</button>" +
          '<div class="rp-menu up" id="modeMenu" hidden>' + Object.keys(modes).map(function (k) {
            return '<button type="button" data-mode="' + k + '">' + ic(modes[k][0]) + " " + modes[k][1] +
              '<small>' + { free: "words, *actions* and `thoughts` as you write them", say: "sent as \"spoken words\"", act: "sent as an *action*", think: "sent as a `private thought`" }[k] + "</small></button>";
          }).join("") + "</div></div>" +
        '<button type="button" class="round light" id="picBtn" title="Picture this moment">' + ic("image") + "</button>" +
        '<span class="grow"></span>' +
        '<button type="button" class="icon-btn" id="wandBtn" title="Write my reply, or polish what I typed">' + ic("wand") + "</button>" +
        (Rec ? '<button type="button" class="icon-btn" id="micBtn" title="Speak your message">' + ic("mic") + "</button>" : "") +
        '<button class="round send" id="sendBtn" title="Send">' + ic("send") + "</button></div></form>" +
        '<p class="notice rp-legend"><span class="rp-speech">"Words"</span><em class="rp-action">actions</em><span class="rp-thought">thoughts</span>' +
        "<span>" + esc(c.name) + " is an AI character. Every character is a fictional adult.</span></p>" +
        "</section>" +
        '<aside class="rp-panel" id="rpPanel"><div class="rp-gallery"><div class="rp-photo" id="rpPhoto"></div>' +
          '<button type="button" class="icon-btn rp-close" id="panelClose" title="Close">' + ic("x") + "</button>" +
          '<button type="button" class="round dim rp-prev" id="phPrev" title="Previous">' + ic("left") + "</button>" +
          '<button type="button" class="round dim rp-next" id="phNext" title="Next">' + ic("right") + "</button>" +
          '<span class="rp-count" id="phCount"></span></div>' +
          '<div class="rp-thumbs" id="phThumbs"></div>' +
          '<div class="rp-who"><h3>' + esc(c.name) + " <small>" + esc(c.age) + "</small></h3>" +
          '<button type="button" class="round light" id="callBtn" title="Call ' + esc(c.name) + ': talk out loud, hear the replies">' + ic("phone") + "</button>" +
          '<div class="rp-pop"><button type="button" class="icon-btn" id="whoMore" title="More">' + ic("more") + "</button>" +
          '<div class="rp-menu right" id="whoMenu" hidden><a href="#/character/' + c.id + '">' + ic("edit") + " Edit " + esc(c.name) + "</a>" +
          '<button type="button" data-act="memory">' + ic("book") + " Memory</button>" +
          '<button type="button" data-act="book">' + ic("edit") + " Turn into a manuscript</button>" +
          '<button type="button" data-act="delete">' + ic("trash") + " Delete this scene</button></div></div></div>" +
          '<p class="rp-blurb" id="blurb"></p>' +
          '<div class="rp-actions"><button type="button" class="btn" id="newScene" title="A new scene, starting at this storyline\'s current time">' + ic("chatplus") + " New session</button>" +
          '<button type="button" class="btn light" id="mediaBtn">' + ic("media") + " View media</button>" +
          '<button type="button" class="btn ghost wide" id="bookBtn">' + ic("book") + " Turn into a manuscript</button></div></aside></div>";
      wireSidebar();
      var layout = view.querySelector(".chat-layout");
      document.getElementById("sideBtn").onclick = function () { layout.classList.toggle("show-side"); };
      function setPanel(open) {
        panelOpen = open; layout.classList.toggle("panel-open", open);
        if (window.matchMedia("(min-width: 1280px)").matches) setPref("rpPanel", open ? "1" : "0");
      }
      document.getElementById("panelBtn").onclick = function () { setPanel(!panelOpen); };
      document.getElementById("panelClose").onclick = function () { setPanel(false); };

      var msgs = document.getElementById("msgs");
      var input = document.getElementById("input");
      var sendBtn = document.getElementById("sendBtn");
      var list = conv.messages.slice();
      var voice = null;  // the voice note being played: { i, state: "loading" | "playing" | "paused" | "done" }
      var photoAt = 0;

      // Right panel: the character's portrait and this scene's pictures, a short blurb, and actions.
      function photos() {
        return (c.avatar ? [c.avatar] : []).concat(list.filter(function (m) { return m.image; }).map(function (m) { return m.image; }));
      }
      function drawPanel() {
        var ph = photos();
        if (photoAt >= ph.length) photoAt = Math.max(0, ph.length - 1);
        var box = document.getElementById("rpPhoto");
        box.style.backgroundImage = ph.length ? "url('" + ph[photoAt].replace(/'/g, "%27") + "')" : "";
        box.classList.toggle("none", !ph.length);
        box.innerHTML = ph.length ? "" : '<span class="rp-initial">' + esc(c.name.charAt(0)) + '</span><a class="btn small light" href="#/character/' + c.id + '">Make a portrait</a>';
        document.getElementById("phCount").textContent = ph.length > 1 ? (photoAt + 1) + " / " + ph.length : "";
        document.getElementById("phPrev").hidden = document.getElementById("phNext").hidden = ph.length < 2;
        document.getElementById("phThumbs").innerHTML = ph.length > 1 ? ph.map(function (u, i) {
          return '<button type="button" data-ph="' + i + '" class="' + (i === photoAt ? "on" : "") + '" style="background-image:url(\'' + esc(u) + '\')" title="Picture ' + (i + 1) + '"></button>';
        }).join("") : "";
        var text = conv.situation || (conv.location ? "Starts at " + conv.location + "." : "") ||
          (conv.storyline && conv.storyline.premise) || c.tagline || "";
        var blurb = document.getElementById("blurb");
        blurb.innerHTML = esc(text) + (text.length > 140 ? ' <button type="button" class="link" id="seeMore">See more</button>' : "");
        blurb.classList.toggle("clamp", text.length > 140);
      }
      document.getElementById("phPrev").onclick = function () { var n = photos().length; photoAt = (photoAt - 1 + n) % n; drawPanel(); };
      document.getElementById("phNext").onclick = function () { photoAt = (photoAt + 1) % photos().length; drawPanel(); };
      document.getElementById("phThumbs").onclick = function (e) { var b = e.target.closest("[data-ph]"); if (b) { photoAt = +b.dataset.ph; drawPanel(); } };
      document.getElementById("blurb").onclick = function (e) {
        if (e.target.id !== "seeMore") return;
        var open = this.classList.toggle("clamp");
        e.target.textContent = open ? "See more" : "See less";
      };
      document.getElementById("rpPhoto").onclick = function (e) { if (!e.target.closest("a") && photos().length) showMedia(photoAt); };
      document.getElementById("mediaBtn").onclick = function () { showMedia(-1); };
      function showMedia(at) {
        var ph = photos();
        if (!ph.length) { toast("No pictures yet. Use the picture button under a message to make one."); return; }
        var dlg = document.createElement("dialog");
        dlg.className = "start-dlg media-dlg";
        dlg.innerHTML = '<div class="start-head"><h2>' + esc(c.name) + '</h2><button type="button" class="x" title="Close">×</button></div>' +
          (at >= 0 ? '<img class="media-big" src="' + esc(ph[at]) + '" alt="">' : "") +
          '<div class="media-grid">' + ph.map(function (u) { return '<a href="' + esc(u) + '" target="_blank" rel="noopener"><img src="' + esc(u) + '" alt="" loading="lazy"></a>'; }).join("") + "</div>";
        document.body.appendChild(dlg);
        dlg.querySelector(".x").onclick = function () { dlg.close(); };
        dlg.onclose = function () { dlg.remove(); };
        dlg.onclick = function (e) { if (e.target === dlg) dlg.close(); };
        dlg.showModal();
      }
      var whoMenu = document.getElementById("whoMenu");
      document.getElementById("whoMore").onclick = function (e) { e.stopPropagation(); whoMenu.hidden = !whoMenu.hidden; };
      whoMenu.onclick = function (e) {
        var b = e.target.closest("[data-act]");
        if (!b) return;
        whoMenu.hidden = true;
        if (b.dataset.act === "memory") document.getElementById("memBtn").click();
        if (b.dataset.act === "delete") document.getElementById("delConv").click();
        if (b.dataset.act === "book") toManuscript();
      };
      document.getElementById("bookBtn").onclick = function () { toManuscript(); };
      // Roleplay to book: the scene (or its storyline) is rewritten as chapters of a novel.
      function toManuscript() {
        var dlg = document.createElement("dialog");
        dlg.className = "start-dlg book-dlg";
        dlg.innerHTML = '<form method="dialog" class="start-form form">' +
          '<div class="start-head"><h2><img class="brand-mark" src="/static/dreamnet/brand/mark-dark.svg" alt=""> Turn into a manuscript</h2><button type="button" class="x" data-close title="Close">×</button></div>' +
          '<p class="hint">The AI rewrites the roleplay as novel chapters: every event and key line kept, in book prose with no asterisks or tags. You can edit every chapter after, and export it from the novel.</p>' +
          (conv.storyline ? '<div class="start-first"><span class="lbl-strong">Write</span><div class="seg" role="radiogroup" aria-label="What to write">' +
            '<label><input type="radio" name="scope" value="scene" checked> This scene</label>' +
            '<label><input type="radio" name="scope" value="storyline"> The whole storyline</label></div></div>' : "") +
          '<label>Into<select name="novel"><option value="">A new novel</option></select></label>' +
          '<label class="new-only">Title<input name="title" maxlength="120" placeholder="' + esc(conv.storyline ? conv.storyline.title : c.name + " and " + state.me.username) + '"></label>' +
          '<div class="two"><label>Your character\'s name in the book<input name="you" maxlength="60" value="' + esc(state.me.username) + '"></label>' +
          '<label class="new-only">Point of view<select name="pov"><option>first person, present tense</option><option>first person, past tense</option>' +
            "<option>third person, past tense</option><option>third person, present tense</option></select></label></div>" +
          '<div class="book-progress" hidden><div class="bar"><span></span></div><p class="hint" id="bookNote"></p></div>' +
          '<div class="row start-actions"><a class="btn ghost" id="bookOpen" hidden>Open the manuscript</a><button type="submit" class="btn primary" data-go>Write the manuscript</button></div></form>';
        document.body.appendChild(dlg);
        var f = dlg.querySelector("form"), timer = null;
        api("/app/api/novels").then(function (ns) {
          f.novel.innerHTML += ns.map(function (n) { return '<option value="' + n.id + '">Add chapters to “' + esc(n.title) + "”</option>"; }).join("");
          if (conv.novel) f.novel.value = conv.novel.id;
          f.novel.onchange();
        }).catch(function () {});
        f.novel.onchange = function () { dlg.querySelectorAll(".new-only").forEach(function (x) { x.hidden = !!f.novel.value; }); };
        dlg.querySelector("[data-close]").onclick = function () { dlg.close(); };
        dlg.onclose = function () { clearInterval(timer); dlg.remove(); };
        f.onsubmit = function (e) {
          e.preventDefault();
          var go = f.querySelector("[data-go]"); go.disabled = true;
          api("/app/api/conversations/" + convId + "/manuscript", { method: "POST", body: {
            scope: f.scope ? f.scope.value : "scene", novel_id: f.novel.value ? +f.novel.value : null,
            title: f.title.value.trim(), you: f.you.value.trim(), pov: f.pov.value } })
            .then(function (r) {
              var prog = dlg.querySelector(".book-progress"), bar = prog.querySelector(".bar span"), note = dlg.querySelector("#bookNote");
              var open = dlg.querySelector("#bookOpen");
              prog.hidden = false; open.hidden = false; open.href = "#/novel/" + r.novel_id; go.hidden = true;
              open.onclick = function () { dlg.close(); };
              function poll() {
                api("/app/api/novels/" + r.novel_id + "/manuscript").then(function (j) {
                  var total = j.total || r.chapters;
                  bar.style.width = Math.round(100 * (j.done || 0) / total) + "%";
                  note.textContent = j.error ? j.error : j.running ? "Writing chapter " + Math.min(j.done + 1, total) + " of " + total + "… You can open it now; chapters appear as they're written."
                    : "Done: " + total + " chapter" + (total === 1 ? "" : "s") + " written.";
                  if (!j.running) { clearInterval(timer); open.classList.remove("ghost"); open.classList.add("primary"); }
                });
              }
              poll(); timer = setInterval(poll, 2500);
            })
            .catch(function (err) { go.disabled = false; toast(err.message, true); });
        };
        dlg.showModal();
      }

      function currentClock() {
        for (var j = list.length - 1; j >= 0; j--) if (list[j].story_at) return list[j].story_at;
        return conv.clock;
      }
      function voicePill(i) {
        var st = voice && voice.i === i ? voice : null;
        if (!st) return "";
        var bars = "";
        for (var k = 0; k < 22; k++) bars += '<i style="--h:' + (30 + Math.round(70 * Math.abs(Math.sin(k * 1.7 + i)))) + '%"></i>';
        return '<div class="rp-voice ' + st.state + '"><button type="button" class="round" data-vplay="' + i + '" title="' +
          (st.state === "playing" ? "Pause" : "Play") + '">' + ic(st.state === "playing" ? "pause" : st.state === "loading" ? "dots" : "play") + "</button>" +
          '<span class="bars">' + bars + '</span><time id="vTime">' + (st.state === "loading" ? "…" : fmtSecs(st.left)) + "</time></div>";
      }
      function render(keepScroll) {
        var lastDay = "", top = msgs.scrollTop;
        var start = conv.start || currentClock();
        var sk = sky(currentClock());
        var lastReal = -1;
        list.forEach(function (m, j) { if (m.role !== "mark") lastReal = j; });
        document.getElementById("clockNow").textContent = storyClock(currentClock(), start, true) + " · " + sk.part + " · " + sk.icon;
        document.getElementById("clockBtn").title = "In-story time. " + sk.part + ", " + sk.moon.toLowerCase() + ". Set the clock or skip ahead.";
        msgs.innerHTML = list.map(function (m, i) {
          var isLast = i === lastReal;
          var ts = m.created_at || Date.now() / 1000;
          var day = dayLabel(ts), head = "";
          if (day !== lastDay) { head = '<div class="day-mark"><span>' + esc(day) + "</span></div>"; lastDay = day; }
          if (m.role === "mark") {
            return head + '<div class="story-mark">' + ic(/^Starts at|^Scenario/.test(m.content) ? "pin" : "clock") + "<span>" + esc(m.content) + (m.story_at ? " · " + esc(storyClock(m.story_at, start, true)) : "") +
              '</span><time title="Written ' + esc(stamp(ts)) + '">' + esc(clock(ts)) + '</time><button data-delmark="' + m.id + '" title="Remove this mark">×</button></div>';
          }
          var me = m.role === "assistant";
          var body = m.editing
            ? '<div class="rp-edit"><textarea data-editbox="' + i + '" rows="6">' + esc(m.content) + "</textarea>" +
              '<div class="row"><button type="button" class="btn small primary" data-editsave="' + i + '">Save</button>' +
              '<button type="button" class="btn small ghost" data-editcancel="' + i + '">Cancel</button></div></div>'
            : '<div class="bubble' + (m.typing ? " typing" : "") + '">' + rp(stripTimeTags(m.content, m.typing)) +
              (me && m.story_at && !m.typing ? '<button type="button" class="now" data-settime="' + m.id + '" title="Change this time">' + esc(nowLine(m.story_at, start)) + "</button>" : "") + "</div>";
          var more = m.id && !m.typing ? '<div class="rp-pop"><button type="button" data-more="' + i + '" title="More">' + ic("more") + "</button>" +
            '<div class="rp-menu" data-menu="' + i + '" hidden>' +
            (!m.image && !m.picturing ? '<button type="button" data-picture="' + i + '">' + ic("image") + " Picture this moment</button>" : "") +
            (me && m.story_at ? '<button type="button" data-settime="' + m.id + '">' + ic("clock") + " Change the time</button>" : "") +
            (conv.novel && me ? '<button type="button" data-tochapter="' + i + '">' + ic("plus") + " Add to chapter</button>" : "") +
            (i > 0 ? '<button type="button" data-rewind="' + m.id + '">' + ic("rewind") + " Rewind to here</button>" : "") + "</div></div>" : "";
          return head + '<div class="msg ' + m.role + '">' +
            '<div class="bubble-wrap"><div class="rp-line">' + body +
            (me && m.id && !m.typing && !m.editing ? '<button type="button" class="round dim rp-speak" data-listen="' + i + '" title="Read aloud">' + ic("speaker") + "</button>" : "") + "</div>" +
            (m.picturing ? '<div class="scene-pic busy"><span><img class="brand-mark" src="/static/dreamnet/brand/mark-dark.svg" alt=""> Picturing this moment…</span></div>' :
              m.image ? '<figure class="scene-pic"><a href="' + esc(m.image) + '" target="_blank" rel="noopener" title="Open full size"><img src="' + esc(m.image) + '" alt="Picture of this moment" loading="lazy"></a>' +
                '<figcaption><button data-picture="' + i + '">' + ic("refresh") + ' New picture</button><button data-picprompt="' + i + '">' + ic("edit") + ' Edit prompt</button><button data-picdel="' + i + '">' + ic("x") + " Remove</button></figcaption></figure>" : "") +
            (m.id && !m.typing && !m.editing ? '<div class="msg-tools">' +
              '<button type="button" data-copy="' + i + '" title="Copy">' + ic("copy") + "</button>" +
              '<button type="button" data-edit="' + i + '" title="Edit">' + ic("edit") + "</button>" +
              (me && isLast && i > 0 ? '<button type="button" data-regen="' + m.id + '" title="Regenerate">' + ic("refresh") + "</button>" +
                '<button type="button" data-continue="' + i + '" title="Continue this reply">' + ic("skip") + "</button>" : "") +
              more +
              (m.role === "user" && m.story_at ? '<time class="story" title="In-story time">' + esc(storyClock(m.story_at, start)) + "</time>" : "") +
              '<time title="Written ' + esc(stamp(ts)) + '">' + esc(clock(ts)) + "</time></div>" : "") +
            voicePill(i) +
            "</div></div>";
        }).join("");
        msgs.scrollTop = keepScroll ? top : msgs.scrollHeight;
        drawPanel();
      }
      render();
      // The user opened the scene from the place picker: get the character's reply to it now.
      var auto = null;
      try { auto = sessionStorage.getItem("dn.autoreply"); sessionStorage.removeItem("dn.autoreply"); } catch (e) { /* none */ }
      if (auto === String(convId) && list.length && list[list.length - 1].role === "user") setTimeout(function () { send(""); }, 0);

      // A picture of one moment: that message drives the action, the server adds what's known about everyone in it.
      function makePicture(m, promptText) {
        m.picturing = true; render(true);
        api("/app/api/messages/" + m.id + "/picture", { method: "POST", body: promptText ? { prompt: promptText } : {} })
          .then(function (r) { m.image = r.image; m.image_prompt = r.prompt; photoAt = photos().indexOf(r.image); })
          .catch(function (err) { toast(err.message, true); })
          .finally(function () { m.picturing = false; render(true); });
      }

      // Voice notes: a reply read aloud plays in a small player under it.
      var vTick = null;
      function voiceTick() {
        clearInterval(vTick);
        vTick = setInterval(function () {
          if (!voice || !document.body.contains(msgs)) { clearInterval(vTick); return; }
          var t = document.getElementById("vTime");
          if (player && voice.audio === player && t && isFinite(player.duration)) {
            voice.left = Math.max(0, player.duration - player.currentTime);
            t.textContent = fmtSecs(voice.left);
          }
        }, 250);
      }
      function playVoice(i, onEnd) {
        stopSpeaking();
        voice = { i: i, state: "loading", left: 0 };
        var mine = voice;
        render(true);
        speak(list[i].content, voiceFor(c), function () {
          if (voice === mine) { mine.state = "done"; mine.left = mine.audio && isFinite(mine.audio.duration) ? mine.audio.duration : 0; render(true); }
          if (onEnd) onEnd();
        }).then(function () {
          if (voice !== mine || mine.state !== "loading") return;
          mine.audio = player; mine.state = "playing";
          mine.left = player && isFinite(player.duration) ? player.duration : 0;
          render(true); voiceTick();
        });
      }
      voiceHook = function () { if (voice && voice.state !== "done" && voice.state !== "loading") { voice.state = "done"; if (document.body.contains(msgs)) render(true); } };

      function send(text, opts) {
        opts = opts || {};
        sendBtn.disabled = true;
        if (text) list.push({ role: "user", content: text, created_at: Date.now() / 1000 });
        var target = opts.cont ? list[opts.cont] : { role: "assistant", content: "", typing: true };
        var base = opts.cont ? target.content.replace(/\s+$/, "") + "\n\n" : "";
        if (opts.cont) target.typing = true; else list.push(target);
        render();
        ctrl = new AbortController();
        stream("/app/api/conversations/" + convId + "/messages", opts.cont ? { content: "", continue: true } : { content: text }, function (t) {
          target.content = base + t; render();
        }, ctrl.signal).then(function () {
          return api("/app/api/conversations/" + convId);
        }).then(function (fresh) {
          list = fresh.messages; render();
          var last = list.length - 1;
          if (list[last] && list[last].role === "assistant" && (calling || pref("autoRead", "0") === "1"))
            playVoice(last, function () { if (calling) listen(true); });
        }).catch(function (e) {
          if (e.name === "AbortError") return;
          if (opts.cont) { target.typing = false; target.content = base.replace(/\s+$/, ""); } else list.pop();
          render(); toast(e.message, true);
        }).finally(function () { sendBtn.disabled = false; ctrl = null; });
      }
      function wrap(text) {
        if (mode === "say" && !/^["“]/.test(text)) return '"' + text + '"';
        if (mode === "act" && !/^\*/.test(text)) return "*" + text + "*";
        if (mode === "think" && !/^`/.test(text)) return "`" + text + "`";
        return text;
      }

      document.getElementById("composer").onsubmit = function (e) {
        e.preventDefault();
        var text = input.value.trim();
        if (!text || sendBtn.disabled) return;
        input.value = ""; input.style.height = "";
        send(wrap(text));
      };
      input.onkeydown = function (e) {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); document.getElementById("composer").requestSubmit(); }
      };
      input.oninput = function () { input.style.height = ""; input.style.height = Math.min(input.scrollHeight, 200) + "px"; };
      var modeMenu = document.getElementById("modeMenu");
      document.getElementById("modeBtn").onclick = function (e) { e.stopPropagation(); modeMenu.hidden = !modeMenu.hidden; };
      modeMenu.onclick = function (e) {
        var b = e.target.closest("[data-mode]");
        if (!b) return;
        mode = b.dataset.mode; setPref("rpMode", mode); modeMenu.hidden = true;
        document.getElementById("modeIc").innerHTML = ic(modes[mode][0]);
        document.getElementById("modeLbl").textContent = modes[mode][1];
        input.focus();
      };
      document.getElementById("picBtn").onclick = function () {
        for (var j = list.length - 1; j >= 0; j--) if (list[j].role !== "mark" && list[j].id) { makePicture(list[j]); return; }
        toast("Say something first, then picture the moment.", true);
      };
      var wand = document.getElementById("wandBtn");
      wand.onclick = function () {
        if (wand.classList.contains("busy")) return;
        wand.classList.add("busy");
        api("/app/api/conversations/" + convId + "/suggest", { method: "POST", body: { draft: input.value.trim() } })
          .then(function (r) { input.value = r.text; input.oninput(); input.focus(); })
          .catch(function (err) { toast(err.message, true); })
          .finally(function () { wand.classList.remove("busy"); });
      };
      // Speaking your message (where the browser can listen), and calls: talk, hear the reply, talk again.
      var rec = null, calling = false, micBtn = document.getElementById("micBtn"), callBtn = document.getElementById("callBtn");
      function listen(forCall) {
        if (!Rec) return;
        if (rec) { rec.abort(); rec = null; }
        stopSpeaking();
        var r = new Rec(), before = forCall ? "" : input.value.replace(/\s*$/, " ").replace(/^ $/, ""), said = "";
        r.lang = "en-US"; r.interimResults = true; r.continuous = false;
        r.onresult = function (e) {
          said = [].map.call(e.results, function (x) { return x[0].transcript; }).join(" ");
          input.value = (before + said).trim(); input.oninput();
        };
        r.onend = function () {
          if (rec === r) rec = null;
          if (micBtn) micBtn.classList.remove("on");
          if (forCall && calling) {
            if (said.trim()) document.getElementById("composer").requestSubmit();
            else setTimeout(function () { if (calling && !rec) listen(true); }, 300);
          }
        };
        r.onerror = function (e) {
          if (e.error === "not-allowed") { toast("Allow the microphone to talk to " + c.name + ".", true); endCall(); }
        };
        rec = r;
        if (micBtn) micBtn.classList.add("on");
        try { r.start(); } catch (err) { rec = null; }
      }
      function endCall() {
        calling = false; callBtn.classList.remove("on"); callBtn.title = "Call " + c.name + ": talk out loud, hear the replies";
        if (rec) { rec.abort(); rec = null; }
      }
      if (micBtn) micBtn.onclick = function () { if (rec) { rec.stop(); } else listen(false); };
      callBtn.onclick = function () {
        if (calling) { endCall(); stopSpeaking(); toast("Call ended"); return; }
        if (!Rec) {
          var on = pref("autoRead", "0") !== "1";
          setPref("autoRead", on ? "1" : "0"); autoRead.checked = on;
          document.getElementById("voiceBtn").classList.toggle("on", on);
          toast(on ? "This browser can't listen, so type as usual: " + c.name + "'s replies will be read aloud." : "Replies won't be read aloud.");
          return;
        }
        calling = true; callBtn.classList.add("on"); callBtn.title = "End the call";
        toast("On a call with " + c.name + ". Talk, then pause; they answer out loud.");
        listen(true);
      };
      var oldCleanup = cleanup;
      cleanup = function () { oldCleanup(); endCall(); voiceHook = null; clearInterval(vTick); };
      document.addEventListener("click", function closeMenus(e) {
        if (!document.body.contains(msgs)) { document.removeEventListener("click", closeMenus); return; }
        if (!e.target.closest(".rp-pop")) view.querySelectorAll(".rp-menu").forEach(function (m) { m.hidden = true; });
      });

      msgs.onclick = function (e) {
        var b = e.target.closest("button");
        if (!b) return;
        if (b.dataset.more) {
          e.stopPropagation();
          var mn = msgs.querySelector('[data-menu="' + b.dataset.more + '"]'), was = mn.hidden;
          view.querySelectorAll(".rp-menu").forEach(function (x) { x.hidden = true; });
          mn.hidden = !was;
          return;
        }
        var menu = b.closest(".rp-menu");
        if (menu) menu.hidden = true;
        if (b.dataset.regen) {
          api("/app/api/messages/" + b.dataset.regen, { method: "DELETE" }).then(function () {
            list = list.filter(function (m) { return m.id < +b.dataset.regen; }); render(); send("");
          });
        } else if (b.dataset.continue) {
          if (!sendBtn.disabled) send("", { cont: +b.dataset.continue });
        } else if (b.dataset.copy) {
          var txt = stripTimeTags(list[+b.dataset.copy].content);
          (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject()).then(function () { toast("Copied"); },
            function () { toast("Couldn't copy here. Select the text instead.", true); });
        } else if (b.dataset.edit) {
          list.forEach(function (m) { m.editing = false; });
          list[+b.dataset.edit].editing = true; render(true);
          var box = msgs.querySelector("[data-editbox]"); if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
        } else if (b.dataset.editcancel) {
          list[+b.dataset.editcancel].editing = false; render(true);
        } else if (b.dataset.editsave) {
          var em = list[+b.dataset.editsave], val = msgs.querySelector("[data-editbox]").value.trim();
          api("/app/api/messages/" + em.id, { method: "PUT", body: { content: val } })
            .then(function (r) { em.content = r.content; em.editing = false; render(true); })
            .catch(function (err) { toast(err.message, true); });
        } else if (b.dataset.rewind) {
          if (!confirm("Delete this message and everything after it?")) return;
          api("/app/api/messages/" + b.dataset.rewind, { method: "DELETE" }).then(function () {
            list = list.filter(function (m) { return m.id < +b.dataset.rewind; }); render();
          });
        } else if (b.dataset.listen || b.dataset.vplay) {
          var idx = +(b.dataset.listen || b.dataset.vplay);
          if (voice && voice.i === idx && voice.state === "playing" && player) { player.pause(); if (player.echo) player.echo.pause(); voice.state = "paused"; render(true); }
          else if (voice && voice.i === idx && voice.state === "paused" && player) { player.play(); voice.state = "playing"; render(true); voiceTick(); }
          else if (voice && voice.i === idx && voice.state === "playing") { stopSpeaking(); }
          else if (!(voice && voice.i === idx && voice.state === "loading")) playVoice(idx);
        } else if (b.dataset.settime) {
          var v = prompt("When does this reply end? Type a time (03:15 AM) or how long it took (45 min, 2 hours). Later messages move with it.");
          if (!v || !v.trim()) return;
          api("/app/api/messages/" + b.dataset.settime + "/time", { method: "PUT", body: { value: v.trim() } })
            .then(function () { return api("/app/api/conversations/" + convId); })
            .then(function (fresh) { list = fresh.messages; render(); toast("Time updated"); })
            .catch(function (err) { toast(err.message, true); });
        } else if (b.dataset.delmark) {
          api("/app/api/marks/" + b.dataset.delmark, { method: "DELETE" }).then(function () {
            list = list.filter(function (m) { return m.id !== +b.dataset.delmark; }); render();
          });
        } else if (b.dataset.picture) {
          makePicture(list[+b.dataset.picture]);
        } else if (b.dataset.picprompt) {
          var pm = list[+b.dataset.picprompt];
          var edited = prompt("Picture prompt. Change it and press OK to make a new picture.", pm.image_prompt || "");
          if (edited && edited.trim()) makePicture(pm, edited.trim());
        } else if (b.dataset.picdel) {
          var dm = list[+b.dataset.picdel];
          api("/app/api/messages/" + dm.id + "/picture", { method: "DELETE" }).then(function () { dm.image = ""; dm.image_prompt = ""; render(true); });
        } else if (b.dataset.tochapter) {
          addToChapter(conv.novel.id, list[+b.dataset.tochapter].content);
        }
      };
      var voicePanel = document.getElementById("voicePanel");
      document.getElementById("voiceBtn").onclick = function () { voicePanel.hidden = !voicePanel.hidden; };
      var autoRead = document.getElementById("autoRead"), readMode = document.getElementById("readMode");
      autoRead.checked = pref("autoRead", "0") === "1";
      readMode.value = pref("readMode", "all");
      document.getElementById("voiceBtn").classList.toggle("on", autoRead.checked);
      autoRead.onchange = function () {
        setPref("autoRead", autoRead.checked ? "1" : "0");
        document.getElementById("voiceBtn").classList.toggle("on", autoRead.checked);
        if (!autoRead.checked) stopSpeaking();
      };
      readMode.onchange = function () { setPref("readMode", readMode.value); };
      var clockPanel = document.getElementById("clockPanel");
      var sceneRules = (conv.time_rules || []).slice();
      function drawSceneRules() {
        document.getElementById("sceneRules").innerHTML = sceneRules.map(function (a, i) {
          return '<div class="sr-row" data-i="' + i + '"><input data-k="label" placeholder="Action, e.g. Shapeshifting" value="' + esc(a.label) + '">' +
            '<input data-k="min" type="number" min="0" step="any" value="' + a.min + '" aria-label="Minimum minutes"> to ' +
            '<input data-k="max" type="number" min="0" step="any" value="' + a.max + '" aria-label="Maximum minutes"> min ' +
            '<input data-k="per" placeholder="per…" value="' + esc(a.per || "") + '" class="sr-per">' +
            '<button type="button" data-delrule="' + i + '" title="Remove">×</button></div>';
        }).join("") || '<p class="muted">None yet.</p>';
      }
      drawSceneRules();
      document.getElementById("sceneRules").oninput = function (e) {
        var row = e.target.closest(".sr-row"), k = e.target.dataset.k;
        if (!row || !k) return;
        sceneRules[+row.dataset.i][k] = k === "min" || k === "max" ? parseFloat(e.target.value) || 0 : e.target.value;
      };
      document.getElementById("sceneRules").onclick = function (e) {
        var b = e.target.closest("[data-delrule]");
        if (b) { sceneRules.splice(+b.dataset.delrule, 1); drawSceneRules(); }
      };
      document.getElementById("srAdd").onclick = function () { sceneRules.push({ label: "", min: 1, max: 5, per: "" }); drawSceneRules(); };
      document.getElementById("srSave").onclick = function () {
        api("/app/api/conversations/" + convId + "/time-rules", { method: "PUT", body: { actions: sceneRules } })
          .then(function (r) { sceneRules = r.actions; drawSceneRules(); toast("Scene rules saved"); })
          .catch(function (err) { toast(err.message, true); });
      };
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
        Promise.all([api("/app/api/characters/" + c.id + "/memories?storyline=" + (conv.storyline ? conv.storyline.id : "")), api("/app/api/conversations/" + convId)]).then(function (r) {
          var mems = r[0], fresh = r[1];
          memPanel.innerHTML =
            '<div class="mem-col"><h4>This scene so far' + (fresh.summary_upto ? ' <span class="hint">up to ' + esc(stamp(fresh.summary_upto)) + "</span>" : "") + "</h4>" +
            '<textarea id="sumText" rows="5" placeholder="Filled in automatically once the scene gets long. You can edit it.">' + esc(fresh.summary) + "</textarea>" +
            '<button class="btn small" id="sumSave">Save summary</button></div>' +
            '<div class="mem-col"><h4>' + esc(c.name) + " remembers <span class=\"hint\">in every scene of this storyline</span></h4>" +
            '<form class="row" id="memAdd"><input id="memText" placeholder="Add something they should never forget"><button class="btn small">Add</button></form>' +
            '<label class="check-line"><input type="checkbox" id="memAll"> Remember in every storyline</label>' +
            '<ul class="mem-list">' + (mems.map(function (m) {
              var when = stamp(m.said_at || m.created_at) + (m.story_time ? " · story time: " + m.story_time : "");
              return '<li><div class="grow"><span>' + esc(m.content) + "</span>" +
                (m.quote ? '<q>' + esc(m.quote) + "</q>" : "") +
                '<small>' + esc(when) + " · " + (m.source === "user" ? "added by you" : "from a scene") + (m.storyline_id ? "" : " · every storyline") + "</small></div>" +
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
            api("/app/api/characters/" + c.id + "/memories", { method: "POST", body: { content: t, storyline_id: conv.storyline ? conv.storyline.id : null, everywhere: document.getElementById("memAll").checked } }).then(loadMemory)
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
        newScene({ character_id: c.id, novel_id: conv.novel ? conv.novel.id : null, storyline_from: convId }, c.name);
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
        var content = (full.content.trim() ? full.content.replace(/\s*$/, "\n\n") : "") + text.replace(/\*([^*\n]+)\*/g, "$1").replace(/`([^`\n]+)`|\(\(([^\n]+?)\)\)/g, function (m, a, b) { return "*" + (a || b) + "*"; });
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
