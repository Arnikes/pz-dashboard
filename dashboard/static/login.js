"use strict";

const form = document.getElementById("loginForm");
const button = document.getElementById("loginSubmit");
const error = document.getElementById("loginError");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = I18n.t("Вход…");
  error.hidden = true;
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-PZ-Request": "1", "X-PZ-Language": I18n.language },
      body: JSON.stringify({
        login: document.getElementById("login").value,
        password: document.getElementById("password").value,
        remember: document.getElementById("remember").checked,
      }),
      signal: AbortSignal.timeout(10000),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || I18n.t("Не удалось войти"));
    const next = new URLSearchParams(location.search).get("next") || location.hash;
    location.replace("/" + (/^#\/[a-z-]+$/.test(next) ? next : "#/overview"));
  } catch (failure) {
    const transportFailure = failure instanceof TypeError || failure.name === "TimeoutError";
    error.textContent = transportFailure
      ? I18n.t("Нет связи с пультом. Повторите попытку.")
      : failure.message;
    error.hidden = false;
    if (!transportFailure) document.getElementById("password").value = "";
    document.getElementById("password").focus();
  } finally {
    button.disabled = false;
    button.textContent = I18n.t("Войти");
  }
});
