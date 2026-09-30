(function () {
  "use strict";
  var UTM_KEYS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"];

  function store(key, val) { try { if (val === undefined) return localStorage.getItem(key); localStorage.setItem(key, val); } catch (e) { return null; } }

  var visitorId = store("vr_vid");
  if (!visitorId) {
    visitorId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
    store("vr_vid", visitorId);
  }

  var params = new URLSearchParams(location.search);
  var utm = {};
  try { utm = JSON.parse(sessionStorage.getItem("vr_utm") || "{}"); } catch (e) { utm = {}; }
  if (UTM_KEYS.some(function (k) { return params.get(k); })) {
    utm = {};
    UTM_KEYS.forEach(function (k) { if (params.get(k)) utm[k] = params.get(k); });
    try { sessionStorage.setItem("vr_utm", JSON.stringify(utm)); } catch (e) {}
  }
  if (!sessionStorage.getItem("vr_ref") && document.referrer && document.referrer.indexOf(location.host) === -1) {
    try { sessionStorage.setItem("vr_ref", document.referrer); } catch (e) {}
  }

  function track(event) {
    var body = JSON.stringify({ event: event, visitor_id: visitorId, page: location.pathname, utm: utm });
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/events", new Blob([body], { type: "application/json" }));
    } else {
      fetch("/api/events", { method: "POST", headers: { "Content-Type": "application/json" }, body: body, keepalive: true });
    }
  }
  window.vrTrack = track;

  if (document.body.classList.contains("page-landing")) track("landing_page_view");
  if (/\/pricing/.test(location.pathname)) track("pricing_page_viewed");

  document.addEventListener("click", function (e) {
    var el = e.target.closest("a, button");
    if (!el) return;
    if (el.classList.contains("js-checklist-cta")) {
      track("checklist_cta_clicked");
      var target = document.getElementById("get-checklist");
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
        history.replaceState(null, "", "#get-checklist");
        setTimeout(function () { var f = document.getElementById("first_name"); if (f) f.focus({ preventScroll: true }); }, 450);
      }
    }
    if (el.dataset.track) track(el.dataset.track);
    if (el.getAttribute("href") === "/resources/care-recruitment-checklist/download") { /* tracked server-side */ }
  });

  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { track(en.target.dataset.trackView); io.unobserve(en.target); }
      });
    }, { threshold: 0.35 });
    document.querySelectorAll("[data-track-view]").forEach(function (el) { io.observe(el); });

    var hero = document.querySelector(".hero__cta"), formSec = document.getElementById("get-checklist"), bar = document.querySelector(".mobile-cta");
    if (hero && bar) {
      var heroVisible = true, formVisible = false;
      var upd = function () { bar.classList.toggle("is-visible", !heroVisible && !formVisible); };
      new IntersectionObserver(function (e) { heroVisible = e[0].isIntersecting; upd(); }).observe(hero);
      if (formSec) new IntersectionObserver(function (e) { formVisible = e[0].isIntersecting; upd(); }).observe(formSec);
    }
  }

  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/;
  var MESSAGES = {
    first_name: "Please enter your first name.",
    last_name: "Please enter your last name.",
    email: "Please enter your work email.",
    organisation: "Please enter your organisation name.",
    job_role: "Please select your job role.",
    consent: "Please confirm you agree to receive the requested resource."
  };

  function setError(form, name, msg) {
    var input = form.elements[name];
    var err = form.querySelector("#" + name + "-error");
    if (!input) return;
    if (msg) { input.setAttribute("aria-invalid", "true"); } else { input.removeAttribute("aria-invalid"); }
    if (err) err.textContent = msg ? "⚠ " + msg : "";
  }

  function validate(form) {
    var errors = {};
    Object.keys(MESSAGES).forEach(function (name) {
      var input = form.elements[name];
      if (!input || !input.required) return;
      var ok = input.type === "checkbox" ? input.checked : input.value.trim() !== "";
      if (!ok) errors[name] = MESSAGES[name];
    });
    var email = form.elements.email;
    if (email && email.value.trim() && !EMAIL_RE.test(email.value.trim())) errors.email = "Please enter a valid email address, e.g. name@organisation.co.uk.";
    return errors;
  }

  function showErrors(form, errors) {
    var alert = form.querySelector(".form-alert");
    Array.prototype.forEach.call(form.elements, function (el) { if (el.name) setError(form, el.name, errors[el.name]); });
    var keys = Object.keys(errors).filter(function (k) { return k !== "form"; });
    if (alert) {
      if (errors.form || keys.length) {
        alert.hidden = false;
        alert.textContent = errors.form || ("Please correct " + keys.length + (keys.length === 1 ? " field" : " fields") + " highlighted below.");
      } else { alert.hidden = true; alert.textContent = ""; }
    }
    if (keys.length && form.elements[keys[0]]) form.elements[keys[0]].focus();
  }

  function wireForm(form, endpoint, onSuccess, startedEvent) {
    if (!form) return;
    var started = false;
    form.addEventListener("focusin", function () { if (!started && startedEvent) { started = true; track(startedEvent); } });
    form.addEventListener("change", function (e) {
      if (e.target.name && e.target.getAttribute("aria-invalid")) {
        var errs = validate(form); setError(form, e.target.name, errs[e.target.name]);
      }
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var errors = validate(form);
      if (Object.keys(errors).length) { showErrors(form, errors); return; }
      var data = {};
      Array.prototype.forEach.call(form.elements, function (el) {
        if (!el.name) return;
        data[el.name] = el.type === "checkbox" ? el.checked : el.value;
      });
      Object.assign(data, utm, { visitor_id: visitorId, landing_page: location.pathname, referrer: sessionStorage.getItem("vr_ref") || "" });
      var btn = form.querySelector("button[type=submit]");
      var label = btn.textContent;
      btn.disabled = true; btn.setAttribute("aria-busy", "true"); btn.textContent = "Sending…";
      fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data), credentials: "same-origin" })
        .then(function (r) { return r.json().catch(function () { return { ok: false, errors: { form: "Something went wrong. Please try again." } }; }); })
        .then(function (res) {
          if (res.ok) { onSuccess(res); return; }
          showErrors(form, res.errors || { form: "Something went wrong. Please try again." });
          btn.disabled = false; btn.removeAttribute("aria-busy"); btn.textContent = label;
        })
        .catch(function () {
          showErrors(form, { form: "We couldn't reach the server. Please check your connection and try again." });
          btn.disabled = false; btn.removeAttribute("aria-busy"); btn.textContent = label;
        });
    });
  }

  wireForm(document.getElementById("lead-form"), "/api/leads", function (res) { location.href = res.redirect; }, "lead_form_started");
  wireForm(document.getElementById("demo-form"), "/api/demo-requests", function () {
    var form = document.getElementById("demo-form"), ok = document.getElementById("demo-success");
    form.hidden = true; ok.hidden = false; ok.focus();
  }, null);
})();
